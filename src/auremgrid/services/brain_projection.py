from __future__ import annotations
from typing import Any
from auremgrid.adapters.semantic import EmbeddingProviderError
from auremgrid.adapters.stack import OpenSourceStack
from auremgrid.domain.errors import ValidationError
from auremgrid.domain.models import Document
from auremgrid.services.brain_shared import new_id, utcnow




class BrainProjectionMixin:
        def _evict_deleted_documents_from_live_projections(self, document_ids: set[str]) -> None:
            for document_id in document_ids:
                self._embeddings.pop(document_id, None)
    
        def _restore_durable_graph_generations(self) -> None:
            """Make an injected persistent provider serve SQLite's durable generation."""
    
            if self.graph.health().get("status") == "unavailable":
                self.graph_health = self.graph.health()
                return
            activate = getattr(self.graph, "activate_generation", None)
            if activate is None:
                return
            degraded_health: dict[str, Any] | None = None
            for row in self.store.conn.execute("SELECT id FROM workspaces").fetchall():
                workspace_id = row["id"]
                active_generation = self.store.graph_generation_state(workspace_id)["active_generation"]
                documents = [
                    self.store._document_from_row(document)
                    for document in self.store.conn.execute(
                        "SELECT * FROM documents WHERE workspace_id=? ORDER BY recorded_at, id",
                        (workspace_id,),
                    ).fetchall()
                ]
                episodes = [
                    self._graph_episode(document, str(active_generation))
                    for document in documents
                ] if active_generation else []
                try:
                    complete = getattr(self.graph, "generation_is_complete", None)
                    if active_generation and (complete is None or complete(
                        workspace_id, active_generation, episodes
                    )):
                        restore = getattr(self.graph, "restore_generation", None)
                        if restore is not None:
                            restore(workspace_id, active_generation, episodes)
                        activate(workspace_id, active_generation)
                        self.graph_health = self.graph.health()
                        continue
                    self._rebuild_graph_workspace(workspace_id, documents)
                    if self.graph_health.get("status") == "degraded" and degraded_health is None:
                        degraded_health = dict(self.graph_health)
                except Exception:
                    self.graph_health = {
                        "status": "degraded",
                        "generation": active_generation,
                        "detail": "generation_restore_failed",
                    }
                    if degraded_health is None:
                        degraded_health = dict(self.graph_health)
            if degraded_health is not None:
                mark_degraded = getattr(self.graph, "mark_degraded", None)
                if mark_degraded is not None:
                    mark_degraded(
                        degraded_health.get("generation"),
                        str(degraded_health.get("detail") or "provider_failed"),
                    )
                self.graph_health = degraded_health
    
        def _rebuild_graph_workspace(
            self, workspace_id: str, documents: list[Document]
        ) -> None:
            """Build and atomically switch one workspace's graph projection."""
            generation = new_id("graphgen")
            self.store.conn.commit()
            maximum = max(
                (f"{document.recorded_at.isoformat()}|{document.id}" for document in documents),
                default="",
            )
            self.store.start_graph_generation(
                workspace_id, generation, f"{len(documents)}|{maximum}"
            )
            self.store.conn.commit()
            try:
                self.graph.rebuild_workspace(
                    generation,
                    [self._graph_episode(document, generation) for document in documents],
                )
                self.store.activate_graph_generation(workspace_id, generation)
                activate = getattr(self.graph, "activate_generation", None)
                if activate is not None:
                    activate(workspace_id, generation)
                self.graph_health = self.graph.health()
            except ValidationError:
                self.store.fail_graph_generation(workspace_id, generation, "stale_snapshot")
                active_generation = self.store.graph_generation_state(workspace_id)["active_generation"]
                old_episodes = [
                    self._graph_episode(document, str(active_generation)) for document in documents
                ] if active_generation else []
                complete = getattr(self.graph, "generation_is_complete", None)
                old_complete = bool(active_generation) and (
                    complete is None or complete(workspace_id, active_generation, old_episodes)
                )
                if old_complete:
                    restore = getattr(self.graph, "restore_generation", None)
                    if restore is not None:
                        restore(workspace_id, active_generation, old_episodes)
                    activate = getattr(self.graph, "activate_generation", None)
                    if activate is not None:
                        activate(workspace_id, active_generation)
                    self.graph_health = {
                        "status": "healthy", "generation": active_generation,
                        "detail": "stale_generation",
                    }
                else:
                    mark_degraded = getattr(self.graph, "mark_degraded", None)
                    if mark_degraded is not None:
                        mark_degraded(active_generation, "stale_snapshot")
                    self.graph_health = {
                        "status": "degraded", "generation": active_generation,
                        "detail": "stale_snapshot",
                    }
            except Exception:
                self.store.fail_graph_generation(workspace_id, generation)
                active_generation = self.store.graph_generation_state(workspace_id)["active_generation"]
                activate = getattr(self.graph, "activate_generation", None)
                if activate is not None and active_generation:
                    restore = getattr(self.graph, "restore_generation", None)
                    if restore is not None:
                        restore(workspace_id, active_generation, [])
                    try:
                        activate(workspace_id, active_generation)
                    except Exception:
                        pass
                mark_degraded = getattr(self.graph, "mark_degraded", None)
                if mark_degraded is not None:
                    mark_degraded(active_generation, "provider_failed")
                self.graph_health = {
                    "status": "degraded", "generation": active_generation, "detail": "provider_failed"
                }
    
        @staticmethod
        def _graph_episode(document: Document, generation: str) -> dict[str, str]:
            return {
                "workspace_id": document.workspace_id,
                "source_id": document.source_id,
                "document_id": document.id,
                "content": document.content,
                "observed_at": document.observed_at.isoformat(),
                "recorded_at": document.recorded_at.isoformat(),
                "generation": generation,
            }
    
        def _upsert_graph_document(self, workspace_id: str, document: Document) -> None:
            """Project one canonical document into SQLite's current graph generation."""
    
            generation = self.store.graph_generation_state(workspace_id)["active_generation"]
            # Persistent upstream projections are generation-scoped. Until an
            # initial rebuild establishes that durable boundary, canonical ingest
            # succeeds without creating an untracked remote episode.
            if generation is None and getattr(self.graph, "uses_current_time_search", False):
                return
            self.graph.upsert_episode(
                workspace_id,
                document.source_id,
                document.content,
                document.observed_at.isoformat(),
                generation=generation,
                document_id=document.id,
                recorded_at=document.recorded_at.isoformat(),
            )
    
        def apply_provider_lifecycle_batch(self, batch_id: str) -> dict[str, Any]:
            """Apply staged provider memberships, then refresh disposable projections."""
    
            workspaces = {
                row["workspace_id"]
                for row in self.store.conn.execute(
                    """SELECT DISTINCT workspace_id FROM provider_route_mutation_staging
                       WHERE batch_id=? AND status='staged'""",
                    (batch_id,),
                ).fetchall()
            }
            applied = self.store.apply_staged_provider_route_mutations(batch_id)
            projection = self.rebuild_projections() if applied else None
            return {
                "batch_id": batch_id,
                "applied": len(applied),
                "workspace_ids": sorted(workspaces),
                "projection": projection,
            }
    
        def rebuild_projections(self, workspace_id: str | None = None, *, rebuild_graph: bool = True) -> dict[str, Any]:
            """Rebuild disposable projections from current canonical evidence.
    
            The in-memory adapters are shared across workspaces, so a requested
            workspace acts as the trigger rather than a destructive partial filter.
            Rebuilding the full active set preserves isolation for every other workspace.
            """
            self.stack = OpenSourceStack()
            workspaces = self.store.conn.execute("SELECT id FROM workspaces").fetchall()
            total_documents = total_facts = 0
            embedding_documents: list[tuple[str, Document]] = []
            graph_documents: list[tuple[str, Document]] = []
            for ws_row in workspaces:
                ws = ws_row["id"]
                documents = self.store.conn.execute(
                    """SELECT documents.* FROM documents
                       WHERE documents.workspace_id=?""",
                    (ws,),
                ).fetchall()
                source_ids = [
                    row["id"] for row in self.store.conn.execute(
                        """SELECT sources.id FROM sources
                           JOIN source_lifecycle_intervals lifecycle
                             ON lifecycle.workspace_id=sources.workspace_id AND lifecycle.source_id=sources.id
                           WHERE sources.workspace_id=? AND lifecycle.retired_at IS NULL""",
                        (ws,),
                    ).fetchall()
                ]
                current_documents: list[Document] = []
                for row in documents:
                    document = self.store._document_from_row(row)
                    embedding_documents.append((ws, document))
                    graph_documents.append((ws, document))
                    if document.source_id in source_ids:
                        current_documents.append(document)
                        self.stack.ingest_document(document, document.content, document.observed_at)
                facts = self.store.list_facts(ws, source_ids, include_superseded=True) if source_ids else []
                for fact in facts: self.stack.ingest_fact(fact)
                memories = self.store.conn.execute("SELECT * FROM memories WHERE workspace_id=?",(ws,)).fetchall()
                for memory in memories: self.stack.remember(ws,memory["actor_id"],memory["content"],memory["kind"])
                self.store.conn.execute("""INSERT INTO projection_state VALUES ('local_projections',?,?,?,?,?,?)
                    ON CONFLICT(name,workspace_id) DO UPDATE SET status=excluded.status,document_count=excluded.document_count,
                    fact_count=excluded.fact_count,last_rebuilt_at=excluded.last_rebuilt_at,last_error=NULL""",
                    (ws,"healthy",len(current_documents),len(facts),utcnow().isoformat(),None))
                total_documents += len(current_documents); total_facts += len(facts)
                if rebuild_graph:
                    snapshot_documents = [document for graph_ws, document in graph_documents if graph_ws == ws]
                    self._rebuild_graph_workspace(ws, snapshot_documents)
            # Projection-health rows above are independent of the vector generation;
            # close that write before the atomic vector replacement begins.
            self.store.conn.commit()
            try:
                vectors = self.embedding_provider.embed([document.content for _, document in embedding_documents])
                if len(vectors) != len(embedding_documents):
                    raise EmbeddingProviderError("embedding provider returned the wrong vector count")
                for vector in vectors:
                    if len(vector) != self.embedding_provider.dimensions:
                        raise EmbeddingProviderError("embedding provider returned the wrong vector dimensions")
                with self.store.atomic(immediate=True):
                    # A provider/model/version is one projection generation.  Never
                    # leave old-version rows available to a new index after rebuild.
                    self.store.conn.execute(
                        "DELETE FROM document_embedding_projection WHERE provider=?",
                        (self.embedding_provider.name,),
                    )
                    for (ws, document), vector in zip(embedding_documents, vectors):
                        self.vector_index.upsert(ws, document.id, tuple(float(value) for value in vector))
                self._embeddings = {
                    document.id: tuple(float(value) for value in vector)
                    for (_, document), vector in zip(embedding_documents, vectors)
                }
                self.embedding_health = self.embedding_provider.health().to_dict()
            except Exception as exc:
                # Keep any prior durable generation intact.  A provider outage is
                # visible as degraded semantic retrieval; it is never relabeled as
                # the deterministic fallback.
                health = self.embedding_provider.health()
                self.embedding_health = {
                    **health.to_dict(),
                    "status": "degraded",
                    "detail": str(exc),
                    "fallback_used": False,
                }
            self.store.conn.commit()
            return {"status":"healthy","workspaces":len(workspaces),"documents":total_documents,"facts":total_facts,
                "embedding_provider":self.embedding_provider.name,
                "embedding_status":self.embedding_health["status"]}
