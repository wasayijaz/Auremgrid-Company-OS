from __future__ import annotations
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from auremgrid.adapters.hybrid import RankedHit
from auremgrid.adapters.semantic import EmbeddingProviderError
from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.domain.models import Actor, AuditEvent, Citation, Document, EvidenceBundle, EvidenceItem, Fact, IngestResult, Memory, Relation, SourceArtifact
from auremgrid.extract.deterministic import extract_claims
from auremgrid.services.brain_shared import HIGH_CONFIDENCE_THRESHOLD, MAX_SEARCH_LIMIT, MAX_SEARCH_QUERY_CHARS, _best_span, _freshness_descriptor, _temporal_read_moment, _token_overlap, content_hash, new_id, normalize_text, utcnow




class BrainIngestionMixin:
        def ingest_text(
            self,
            workspace_id: str,
            actor_id: str,
            source_key: str,
            content: str,
            locator: str,
            allowed_actor_ids: list[str] | None = None,
            observed_at: datetime | None = None,
            media_type: str = "text/markdown",
            trust_level: str = "internal",
        ) -> IngestResult:
            actor = self._require_actor(workspace_id, actor_id)
            if not actor.can_write:
                self._audit(workspace_id, actor_id, "ingest", source_key, "denied", "read-only actor")
                raise AuthorizationError("actor cannot ingest sources")
            if not source_key.strip():
                raise ValidationError("source_key is required")
            observed = observed_at or utcnow()
            digest = content_hash(content)
            existing = self.store.find_source(workspace_id, source_key, digest)
            if existing:
                if self.graph_health.get("status") == "degraded":
                    row = self.store.conn.execute(
                        "SELECT * FROM documents WHERE workspace_id=? AND source_id=? ORDER BY recorded_at DESC LIMIT 1",
                        (workspace_id, existing.id),
                    ).fetchone()
                    if row is not None:
                        document = self.store._document_from_row(row)
                        try:
                            self._upsert_graph_document(workspace_id, document)
                            self.graph_health = self.graph.health()
                        except Exception:
                            self.graph_health = {"status": "degraded", "generation": None, "detail": "provider_failed"}
                if not self.store.source_is_active(workspace_id, existing.id):
                    self.store.activate_source(workspace_id, existing.id, reason="identical_source_reactivated")
                    self.rebuild_projections(workspace_id)
                self._audit(workspace_id, actor_id, "ingest", source_key, "noop", "identical content hash")
                return IngestResult(
                    created=False,
                    source=existing,
                    document_id=None,
                    message="idempotent no-op",
                )
            latest = self.store.latest_source(workspace_id, source_key)
            version = 1 if latest is None else latest.version + 1
            extraction = extract_claims(content, observed)
            lifecycle_candidates = [item.valid_from for item in (*extraction.facts, *extraction.relations)]
            lifecycle_at = min(lifecycle_candidates, default=observed)
            source = SourceArtifact(
                id=new_id("src"),
                workspace_id=workspace_id,
                source_key=source_key,
                locator=locator,
                content_hash=digest,
                media_type=media_type,
                trust_level=trust_level,
                allowed_actor_ids=tuple(allowed_actor_ids or ()),
                observed_at=observed,
                recorded_at=utcnow(),
                version=version,
            )
            document = Document(
                id=new_id("doc"),
                workspace_id=workspace_id,
                source_id=source.id,
                content=content,
                content_hash=digest,
                observed_at=observed,
                recorded_at=utcnow(),
            )
            facts, relations = self._persist_ingestion(
                actor, source, document, extraction, lifecycle_at
            )
            try:
                self._upsert_graph_document(workspace_id, document)
                self.graph_health = self.graph.health()
            except Exception:
                # Canonical ingestion has already committed; graph projection repair
                # is retried by the next explicit rebuild and cannot roll back truth.
                self.graph_health = {"status": "degraded", "generation": None, "detail": "provider_failed"}
            try:
                vector = tuple(self.embedding_provider.embed([content])[0])
                if len(vector) != self.embedding_provider.dimensions:
                    raise EmbeddingProviderError("embedding provider returned the wrong vector dimensions")
                self._embeddings[document.id] = vector
                with self.store.atomic(immediate=True):
                    self.vector_index.upsert(workspace_id, document.id, vector)
                self.embedding_health = self.embedding_provider.health().to_dict()
            except Exception as exc:
                self.embedding_health = {
                    **self.embedding_provider.health().to_dict(),
                    "status": "degraded",
                    "detail": str(exc),
                    "fallback_used": False,
                }
            self.stack.ingest_document(document, content, observed)
            for fact in facts:
                self.stack.ingest_fact(fact)
            fact_ids = [fact.id for fact in facts]
            relation_ids = [relation.id for relation in relations]
            self._audit(
                workspace_id,
                actor_id,
                "ingest",
                source_key,
                "created",
                f"facts={len(fact_ids)} relations={len(relation_ids)}",
            )
            return IngestResult(
                created=True,
                source=source,
                document_id=document.id,
                fact_ids=tuple(fact_ids),
                relation_ids=tuple(relation_ids),
                message="ingested",
            )
    
        def _persist_ingestion(
            self,
            actor: Actor,
            source: SourceArtifact,
            document: Document,
            extraction: Any,
            lifecycle_at: datetime,
        ) -> tuple[list[Fact], list[Relation]]:
            """Persist a complete version before atomically making it current."""
    
            facts: list[Fact] = []
            relations: list[Relation] = []
            with self.store.atomic(immediate=True):
                self.store.create_source(source, lifecycle_at=lifecycle_at, activate=False)
                self.store.create_document(document)
                for extracted in extraction.facts:
                    fact = Fact(
                        id=new_id("fact"), workspace_id=source.workspace_id,
                        source_id=source.id, document_id=document.id,
                        subject=extracted.subject, predicate=extracted.predicate, object=extracted.object,
                        valid_from=extracted.valid_from, valid_until=extracted.valid_until,
                        observed_at=source.observed_at, recorded_at=utcnow(),
                        confidence=extracted.confidence, superseded_by=None,
                        conflict_group=extracted.conflict_group,
                        citation=Citation(
                            source_id=source.id, source_key=source.source_key,
                            locator=source.locator, content_hash=source.content_hash,
                            evidence_span=extracted.evidence_span, observed_at=source.observed_at,
                            valid_from=extracted.valid_from, valid_until=extracted.valid_until,
                            confidence=extracted.confidence,
                        ),
                    )
                    group = self._conflict_group_for(actor, fact)
                    if group and fact.conflict_group is None:
                        fact = replace(fact, conflict_group=group)
                    # The old source remains current until every downstream row exists,
                    # so supersession still sees and updates the prior current facts.
                    self._supersede_matching(actor, fact)
                    self.store.create_fact(fact)
                    scope = self.company.workspace_scope(source.workspace_id)
                    if scope is not None:
                        initial_state = "conflicted" if fact.conflict_group else (
                            "high_confidence" if fact.confidence >= HIGH_CONFIDENCE_THRESHOLD else "inferred"
                        )
                        self.brain_ops._state_event(
                            scope["organization_id"], source.workspace_id, "fact", fact.id,
                            initial_state,
                            "incompatible current observation" if fact.conflict_group else "deterministic extraction",
                            source.id, actor.id, fact.valid_from, fact.valid_until,
                        )
                    facts.append(fact)
                for extracted in extraction.relations:
                    relation = Relation(
                        id=new_id("rel"), workspace_id=source.workspace_id,
                        source_id=source.id, document_id=document.id,
                        from_entity=extracted.from_entity, relation=extracted.relation,
                        to_entity=extracted.to_entity, valid_from=extracted.valid_from,
                        valid_until=extracted.valid_until, observed_at=source.observed_at,
                        recorded_at=utcnow(), confidence=extracted.confidence,
                        citation=Citation(
                            source_id=source.id, source_key=source.source_key,
                            locator=source.locator, content_hash=source.content_hash,
                            evidence_span=extracted.evidence_span, observed_at=source.observed_at,
                            valid_from=extracted.valid_from, valid_until=extracted.valid_until,
                            confidence=extracted.confidence,
                        ),
                    )
                    self.store.create_relation(relation)
                    relations.append(relation)
                self.store.activate_source(
                    source.workspace_id,
                    source.id,
                    activated_at=source.recorded_at,
                    reason="source_ingest_committed",
                    effective_from=lifecycle_at,
                )
            return facts, relations
    
        def ingest_path(
            self,
            workspace_id: str,
            actor_id: str,
            path: str | Path,
            source_key: str | None = None,
            allowed_actor_ids: list[str] | None = None,
            observed_at: datetime | None = None,
        ) -> IngestResult:
            file_path = Path(path)
            content = file_path.read_text(encoding="utf-8")
            return self.ingest_text(
                workspace_id=workspace_id,
                actor_id=actor_id,
                source_key=source_key or file_path.name,
                content=content,
                locator=str(file_path),
                allowed_actor_ids=allowed_actor_ids,
                observed_at=observed_at,
            )
    
        def search(
            self,
            workspace_id: str,
            actor_id: str,
            query: str,
            as_of: datetime | None = None,
            limit: int = 8,
        ) -> EvidenceBundle:
            actor = self._require_actor(workspace_id, actor_id)
            if not isinstance(limit, int) or limit < 1:
                raise ValidationError("limit must be a positive integer")
            limit = min(limit, MAX_SEARCH_LIMIT)
            if not isinstance(query, str) or not query.strip():
                raise ValidationError("query is required")
            if len(query) > MAX_SEARCH_QUERY_CHARS:
                raise ValidationError(f"query must be at most {MAX_SEARCH_QUERY_CHARS} characters")
            requested_as_of = _temporal_read_moment(as_of)
            # Knowledge-state events retain sub-second ordering.  Do not round the
            # live read watermark or a just-recorded transition can disappear until
            # the next wall-clock second.
            as_of = requested_as_of or datetime.now(timezone.utc)
            sources = self.store.allowed_sources(workspace_id, actor, as_of=requested_as_of)
            source_ids = [source.id for source in sources]
            sources_by_id = {source.id: source for source in sources}
            query_norm = normalize_text(query)
            fused_hits: list[RankedHit] = []
            documents_by_id: dict[str, tuple[Document, SourceArtifact]] = {}
            facts_by_id: dict[str, Fact] = {}
            fts_hits = self.store.search_documents(workspace_id, source_ids, query, limit, as_of=as_of)
            for document, source, score in fts_hits:
                documents_by_id[document.id] = (document, source)
                fused_hits.append(RankedHit(
                    "document", document.id, score, ("keyword",), source.trust_level,
                    source.observed_at, source.recorded_at,
                ))
            allowed_document_ids = self.store.allowed_document_ids(workspace_id, source_ids, as_of)
            allowed_document_id_set = set(allowed_document_ids)
            graph_status = "healthy"
            graph_detail: str | None = None
            graph_hits = 0
            try:
                graph_state = self.store.graph_generation_state(workspace_id)
                active_graph_generation = graph_state["active_generation"]
                if not active_graph_generation:
                    raise RuntimeError("graph projection is not active")
                all_source_ids = {
                    source.id
                    for source in self.store.allowed_sources(
                        workspace_id, replace(actor, role="admin"), as_of=requested_as_of
                    )
                }
                if requested_as_of is not None and getattr(
                    self.graph, "uses_current_time_search", False
                ):
                    graph_status = "skipped"
                    graph_detail = "historical_query"
                    external_hits = []
                elif (
                    getattr(self.graph, "requires_full_workspace_access", False)
                    and set(source_ids) != all_source_ids
                ):
                    graph_status = "restricted"
                    graph_detail = "partial_acl"
                    external_hits: list[dict[str, Any]] = []
                else:
                    external_hits = self.graph.search(
                        workspace_id, query, source_ids, as_of=requested_as_of, limit=limit,
                        generation=active_graph_generation,
                    )
                for external_hit in external_hits:
                    source_id = external_hit.get("source_id")
                    if not isinstance(source_id, str) or source_id not in source_ids:
                        continue
                    document_ref = external_hit.get("document_id")
                    if document_ref is not None:
                        document = self.store.get_document(workspace_id, document_ref) if isinstance(document_ref, str) else None
                        if document is None or document.source_id != source_id or document.id not in self.store.allowed_document_ids(workspace_id, [source_id], as_of):
                            continue
                    else:
                        document = next(
                            (candidate for candidate in (self.store.get_document(workspace_id, doc_id) for doc_id in
                             self.store.allowed_document_ids(workspace_id, [source_id], as_of)) if candidate is not None),
                            None,
                        )
                    if document is None or document.id not in allowed_document_id_set:
                        continue
                    source = sources_by_id.get(source_id)
                    if source is None:
                        continue
                    documents_by_id[document.id] = (document, source)
                    fused_hits.append(RankedHit(
                        "document", document.id, 0.2, ("graph",), source.trust_level,
                        source.observed_at, source.recorded_at,
                    ))
                    graph_hits += 1
            except Exception:
                graph_status = "degraded"
                self.graph_health = {"status": "degraded", "generation": None, "detail": "provider_failed"}
            semantic_status = "healthy"
            semantic_detail: str | None = None
            semantic_hits = 0
            try:
                query_embedding = self.embedding_provider.embed([query])[0]
                for document_id, vector_score in self.vector_index.search(
                    workspace_id,
                    tuple(query_embedding),
                    allowed_document_ids,
                    max(limit, len(allowed_document_ids)),
                ):
                    document = self.store.get_document(workspace_id, document_id)
                    if document is None or document.source_id not in source_ids:
                        continue
                    source = sources_by_id.get(document.source_id)
                    if source is None:
                        continue
                    # The offline hash provider is a lexical safety net, not a
                    # semantic model. Reject hash-bucket collisions using a local
                    # candidate-quality check, while still searching the complete
                    # authorized vector candidate set independently of FTS.
                    if (
                        self.embedding_provider.name == "deterministic_lexical_fallback"
                        and not _token_overlap(query_norm, normalize_text(document.content))
                    ):
                        continue
                    documents_by_id[document.id] = (document, source)
                    fused_hits.append(RankedHit(
                        "document", document.id, vector_score, ("vector",), source.trust_level,
                        source.observed_at, source.recorded_at,
                    ))
                    semantic_hits += 1
            except Exception:
                # Do not substitute the deterministic provider here.  Operators and
                # callers must be able to distinguish an outage from a real fallback.
                semantic_status = "degraded"
                # Public retrieval metadata uses a stable code. Provider exceptions
                # can contain local paths, model internals, or credential-like text.
                semantic_detail = "provider_failed"
            for fact in self.store.list_facts(workspace_id, source_ids, as_of=as_of, include_superseded=True):
                latest_state = self.brain_ops._knowledge_state_row(workspace_id, "fact", fact.id, as_of)
                if latest_state is not None and latest_state["state"] == "stale":
                    continue
                haystack = normalize_text(f"{fact.subject} {fact.predicate} {fact.object}")
                keyword_hit = _token_overlap(query_norm, haystack)
                graph_boost = self.graph.related_fact_boost(fact, query) if hasattr(self.graph, "related_fact_boost") else 0.0
                if not keyword_hit and graph_boost <= 0:
                    continue
                facts_by_id[fact.id] = fact
                source = sources_by_id.get(fact.source_id)
                if source is None:
                    continue
                score = (0.7 + (0.3 * fact.confidence)) if keyword_hit else 0.0
                if fact.superseded_by:
                    score -= 0.4
                if keyword_hit:
                    fused_hits.append(RankedHit(
                        "fact", fact.id, score, ("keyword",), source.trust_level,
                        source.observed_at, source.recorded_at,
                    ))
                if graph_boost:
                    fused_hits.append(RankedHit(
                        "fact", fact.id, graph_boost, ("graph",), source.trust_level,
                        source.observed_at, source.recorded_at,
                    ))
            items: list[EvidenceItem] = []
            for hit in self.ranker.fuse(fused_hits, limit=limit, as_of=as_of):
                score_components = {name: value for name, value in hit.score_components}
                if hit.kind == "document":
                    document, source = documents_by_id[hit.key]
                    items.append(
                        EvidenceItem(
                            kind="document",
                            score=round(hit.score, 4),
                            payload={
                                "document_id": document.id,
                                "source_key": source.source_key,
                             "channels": list(hit.channels),
                                "citation_ref": f"document:{document.id}",
                                "score_components": score_components,
                                "freshness": _freshness_descriptor(document.observed_at, document.recorded_at, as_of),
                            },
                            citation=Citation(
                                source_id=source.id,
                                source_key=source.source_key,
                                locator=source.locator,
                                content_hash=source.content_hash,
                                evidence_span=_best_span(document.content, query),
                                observed_at=document.observed_at,
                            ),
                        )
                    )
                else:
                    fact = facts_by_id[hit.key]
                    payload = fact.to_dict()
                    state = self.brain_ops._knowledge_state_row(workspace_id, "fact", fact.id, as_of)
                    payload["effective_state"] = str(state["state"]) if state is not None else "inferred"
                    payload["channels"] = list(hit.channels)
                    payload["citation_ref"] = f"fact:{fact.id}"
                    payload["score_components"] = score_components
                    payload["freshness"] = _freshness_descriptor(fact.observed_at, fact.recorded_at, as_of)
                    items.append(
                        EvidenceItem(
                            kind="fact",
                            score=round(hit.score, 4),
                            payload=payload,
                            citation=fact.citation,
                        )
                    )
            bounded = tuple(items[:limit])
            unknown = len(bounded) == 0
            message = "insufficient evidence" if unknown else "evidence retrieved"
            retrieval = {
                "fts": "healthy",
                "semantic": semantic_status,
                "semantic_hits": semantic_hits,
                "graph": graph_status,
                "graph_hits": graph_hits,
                "fallback_used": self.embedding_provider.name == "deterministic_lexical_fallback",
                "provider": self.embedding_provider.name,
                "model": self.embedding_provider.model,
                "version": self.embedding_provider.version,
                "authorized_source_count": len(source_ids),
                "authorized_document_count": len(allowed_document_ids),
                "requested_limit": limit,
                "effective_limit": limit,
                "citation_contract": "source_id+content_hash+evidence_span+observed_at",
                "freshness_contract": "observed_at+recorded_at+as_of; observed 70% / recorded 30%, 180-day half-life",
                "ranking": {
                    "contract": "hybrid-authority-recency-v1",
                    "weights": {
                        "relevance": self.ranker.RELEVANCE_WEIGHT,
                        "authority": self.ranker.AUTHORITY_WEIGHT,
                        "recency": self.ranker.RECENCY_WEIGHT,
                    },
                    "recency_half_life_days": self.ranker.RECENCY_HALF_LIFE_DAYS,
                    "tie_break": ["score_desc", "kind_asc", "key_asc"],
                },
            }
            if graph_detail:
                retrieval["graph_detail"] = graph_detail
            if semantic_detail:
                retrieval["semantic_detail"] = semantic_detail
            if semantic_status == "degraded":
                message = f"{message} (semantic channel degraded)"
            self._audit(workspace_id, actor_id, "search", query, "ok" if not unknown else "unknown", message)
            return EvidenceBundle(
                workspace_id=workspace_id,
                query=query,
                as_of=as_of,
                unknown=unknown,
                message=message,
                items=bounded,
                retrieval=retrieval,
            )
    
        def entity(
            self,
            workspace_id: str,
            actor_id: str,
            name: str,
            as_of: datetime | None = None,
        ) -> dict[str, Any]:
            actor = self._require_actor(workspace_id, actor_id)
            requested_as_of = _temporal_read_moment(as_of)
            as_of = requested_as_of or utcnow()
            source_ids = [
                source.id for source in self.store.allowed_sources(workspace_id, actor, as_of=requested_as_of)
            ]
            target = normalize_text(name)
            facts = [
                fact
                for fact in self.store.list_facts(workspace_id, source_ids, as_of=as_of)
                if target in {normalize_text(fact.subject), normalize_text(fact.object)}
            ]
            relations = [
                relation
                for relation in self.store.list_relations(workspace_id, source_ids, as_of=as_of)
                if target in {normalize_text(relation.from_entity), normalize_text(relation.to_entity)}
            ]
            self._audit(workspace_id, actor_id, "entity", name, "ok", f"facts={len(facts)}")
            fact_rows = []
            for fact in facts:
                item = fact.to_dict()
                state = self.brain_ops._knowledge_state_row(workspace_id, "fact", fact.id, as_of)
                item["effective_state"] = str(state["state"]) if state is not None else "inferred"
                fact_rows.append(item)
            return {
                "entity": name,
                "as_of": as_of.isoformat(),
                "facts": fact_rows,
                "relations": [relation.to_dict() for relation in relations],
            }
    
        def history(
            self,
            workspace_id: str,
            actor_id: str,
            subject: str,
            predicate: str | None = None,
            as_of: datetime | None = None,
        ) -> dict[str, Any]:
            actor = self._require_actor(workspace_id, actor_id)
            requested_as_of = _temporal_read_moment(as_of)
            moment = requested_as_of or utcnow()
            source_ids = [source.id for source in self.store.allowed_sources(
                workspace_id, actor, as_of=requested_as_of, include_retired=requested_as_of is None,
            )]
            subject_norm = normalize_text(subject)
            facts = []
            for fact in self.store.list_facts(workspace_id, source_ids, include_superseded=True):
                if requested_as_of is not None and fact.recorded_at > moment:
                    continue
                if normalize_text(fact.subject) != subject_norm:
                    continue
                if predicate and normalize_text(fact.predicate) != normalize_text(predicate):
                    continue
                facts.append(fact)
            self._audit(workspace_id, actor_id, "history", subject, "ok", f"versions={len(facts)}")
            fact_rows = []
            for fact in facts:
                item = fact.to_dict()
                state = self.brain_ops._knowledge_state_row(workspace_id, "fact", fact.id, moment)
                item["effective_state"] = str(state["state"]) if state is not None else "inferred"
                fact_rows.append(item)
            return {
                "subject": subject,
                "predicate": predicate,
                "as_of": moment.isoformat(),
                "facts": fact_rows,
            }
    
        def neighbors(
            self,
            workspace_id: str,
            actor_id: str,
            entity: str,
            as_of: datetime | None = None,
        ) -> dict[str, Any]:
            actor = self._require_actor(workspace_id, actor_id)
            requested_as_of = _temporal_read_moment(as_of)
            as_of = requested_as_of or utcnow()
            source_ids = [
                source.id for source in self.store.allowed_sources(workspace_id, actor, as_of=requested_as_of)
            ]
            target = normalize_text(entity)
            relations = [
                relation
                for relation in self.store.list_relations(workspace_id, source_ids, as_of=as_of)
                if target in {normalize_text(relation.from_entity), normalize_text(relation.to_entity)}
            ]
            self._audit(workspace_id, actor_id, "neighbors", entity, "ok", f"edges={len(relations)}")
            return {"entity": entity, "as_of": as_of.isoformat(), "relations": [relation.to_dict() for relation in relations]}
    
        def sources(self, workspace_id: str, actor_id: str) -> dict[str, Any]:
            actor = self._require_actor(workspace_id, actor_id)
            sources = self.store.allowed_sources(workspace_id, actor)
            self._audit(workspace_id, actor_id, "sources", workspace_id, "ok", f"count={len(sources)}")
            return {"sources": [source.to_dict() for source in sources]}
    
        def recent(self, workspace_id: str, actor_id: str, limit: int = 5) -> dict[str, Any]:
            actor = self._require_actor(workspace_id, actor_id)
            source_ids = [source.id for source in self.store.allowed_sources(workspace_id, actor)]
            rows = self.store.list_recent_documents(workspace_id, source_ids, limit)
            self._audit(workspace_id, actor_id, "recent", workspace_id, "ok", f"count={len(rows)}")
            return {
                "documents": [
                    {
                        "document": document.to_dict(),
                        "source": source.to_dict(),
                    }
                    for document, source in rows
                ]
            }
    
        def remember(
            self,
            workspace_id: str,
            actor_id: str,
            content: str,
            kind: str = "preference",
        ) -> Memory:
            actor = self._require_actor(workspace_id, actor_id)
            if not actor.can_write:
                self._audit(workspace_id, actor_id, "remember", kind, "denied", "read-only actor")
                raise AuthorizationError("actor cannot write memory")
            memory = Memory(
                id=new_id("mem"),
                workspace_id=workspace_id,
                actor_id=actor.id,
                kind=kind,
                content=content,
                observed_at=utcnow(),
                recorded_at=utcnow(),
            )
            self.store.create_memory(memory)
            self.stack.remember(workspace_id, actor.id, content, kind)
            self._audit(workspace_id, actor_id, "remember", kind, "created", content[:120])
            return memory
    
        def memories(self, workspace_id: str, actor_id: str) -> list[Memory]:
            actor = self._require_actor(workspace_id, actor_id)
            return self.store.list_memories(workspace_id, actor.id)
    
        def audit_log(self, workspace_id: str, actor_id: str) -> list[AuditEvent]:
            actor = self._require_actor(workspace_id, actor_id)
            if not actor.is_admin:
                raise AuthorizationError("only admins can read the audit log")
            return self.store.list_audit(workspace_id)
