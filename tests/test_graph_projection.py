from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from auremgrid.adapters.graphiti_local import LocalTemporalGraph
from auremgrid.services.brain import CompanyOS


class FakeGraph:
    name = "fake-graph"
    requires_full_workspace_access = False

    def __init__(self, *, fail_upsert=False, fail_rebuild=False):
        self.fail_upsert = fail_upsert
        self.fail_rebuild = fail_rebuild
        self.hits = []
        self.generations = []
        self.search_generations = []
        self.search_allowed_source_ids = []
        self.upserts = []
        self.rebuild_episodes = []
        self.restored = []
        self.activated = []
        self.closed = False
        self.on_rebuild = None

    def health(self):
        return {"status": "healthy", "generation": self.generations[-1] if self.generations else None, "detail": None}

    def upsert_episode(
        self, workspace_id, source_id, content, observed_at, generation=None,
        *, document_id=None, recorded_at=None,
    ):
        if self.fail_upsert:
            raise RuntimeError("network should be hidden")
        self.upserts.append({
            "workspace_id": workspace_id, "source_id": source_id, "content": content,
            "observed_at": observed_at, "generation": generation,
            "document_id": document_id, "recorded_at": recorded_at,
        })

    def rebuild_workspace(self, generation, episodes):
        if self.fail_rebuild:
            raise RuntimeError("upstream outage")
        self.rebuild_episodes.append(list(episodes))
        self.generations.append(generation)
        if self.on_rebuild is not None:
            self.on_rebuild()

    def activate_generation(self, workspace_id, generation):
        self.activated.append((workspace_id, generation))

    def restore_generation(self, workspace_id, generation, episodes):
        self.restored.append((workspace_id, generation, list(episodes)))

    def close(self):
        self.closed = True

    def search(self, workspace_id, query, allowed_source_ids, as_of=None, limit=8, generation=None):
        self.search_generations.append(generation)
        self.search_allowed_source_ids.append(list(allowed_source_ids))
        return self.hits[:limit]


class GraphProjectionTests(unittest.TestCase):
    def _setup(self, graph):
        os = CompanyOS(graph_projection=graph)
        ws = os.create_workspace("Client", "ws_client")
        admin = os.create_actor(ws.id, "Admin", "admin", "act_admin")
        return os, ws, admin

    def test_forged_external_refs_are_rehydrated_and_acl_filtered(self):
        graph = FakeGraph()
        os, ws, actor = self._setup(graph)
        good = os.ingest_text(ws.id, actor.id, "good", "canonical launch evidence", "memory://good")
        os.rebuild_projections()
        graph.hits = [
            {"source_id": good.source.id, "document_id": "forged", "prose": "do not trust me"},
            {"source_id": "other-workspace", "document_id": "secret"},
            {"source_id": good.source.id, "document_id": good.document_id},
        ]
        result = os.search(ws.id, actor.id, "no fts overlap")
        ids = {item.payload.get("document_id") for item in result.items}
        self.assertIn(good.document_id, ids)
        self.assertNotIn("forged", ids)
        self.assertNotIn("secret", ids)
        self.assertNotIn("prose", repr(result.to_dict()))
        os.close()

    def test_graph_failure_does_not_fail_canonical_ingest(self):
        graph = FakeGraph(fail_upsert=True)
        os, ws, actor = self._setup(graph)
        result = os.ingest_text(ws.id, actor.id, "good", "canonical evidence", "memory://good")
        self.assertTrue(result.created)
        self.assertEqual(os.graph_health["status"], "degraded")
        self.assertIsNotNone(os.store.get_document(ws.id, result.document_id))
        os.close()

    def test_rebuild_and_live_upsert_carry_canonical_identity_and_generation(self):
        graph = FakeGraph()
        os, ws, actor = self._setup(graph)
        first = os.ingest_text(ws.id, actor.id, "one", "canonical one", "memory://one")
        os.rebuild_projections()
        active = os.store.graph_generation_state(ws.id)["active_generation"]
        rebuilt = graph.rebuild_episodes[-1][0]
        first_document = os.store.get_document(ws.id, first.document_id)
        self.assertEqual(rebuilt["document_id"], first.document_id)
        self.assertEqual(rebuilt["recorded_at"], first_document.recorded_at.isoformat())
        self.assertEqual(rebuilt["generation"], active)

        second = os.ingest_text(ws.id, actor.id, "two", "canonical two", "memory://two")
        live = graph.upserts[-1]
        second_document = os.store.get_document(ws.id, second.document_id)
        self.assertEqual(live["generation"], active)
        self.assertEqual(live["document_id"], second.document_id)
        self.assertEqual(live["recorded_at"], second_document.recorded_at.isoformat())
        os.close()

    def test_injected_persistent_graph_restores_sqlite_generation_without_rebuild(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "company.db"
            first = CompanyOS(database)
            ws = first.create_workspace("Client", "ws_client")
            actor = first.create_actor(ws.id, "Admin", "admin", "act_admin")
            first.ingest_text(ws.id, actor.id, "one", "canonical one", "memory://one")
            first.rebuild_projections()
            active = first.store.graph_generation_state(ws.id)["active_generation"]
            first.close()

            graph = FakeGraph()
            restarted = CompanyOS(database, graph_projection=graph)
            self.assertEqual(graph.generations, [])
            restored_workspace, restored_generation, restored_episodes = graph.restored[0]
            self.assertEqual((restored_workspace, restored_generation), (ws.id, active))
            self.assertEqual(restored_episodes[0]["generation"], active)
            self.assertIn("document_id", restored_episodes[0])
            self.assertIn("recorded_at", restored_episodes[0])
            self.assertEqual(graph.activated, [(ws.id, active)])
            restarted.close()

    def test_graph_health_starts_from_injected_provider(self):
        class DegradedGraph(FakeGraph):
            def health(self):
                return {"status": "degraded", "generation": "durable", "detail": "offline"}

        os, _, _ = self._setup(DegradedGraph())
        self.assertEqual(
            os.graph_health,
            {"status": "degraded", "generation": "durable", "detail": "offline"},
        )
        graph = os.graph
        os.close()
        self.assertTrue(graph.closed)

    def test_incomplete_graph_generation_does_not_replace_active(self):
        graph = FakeGraph()
        os, ws, actor = self._setup(graph)
        os.ingest_text(ws.id, actor.id, "good", "canonical evidence", "memory://good")
        os.rebuild_projections()
        active = os.store.graph_generation_state(ws.id)["active_generation"]
        graph.fail_rebuild = True
        os.rebuild_projections()
        state = os.store.graph_generation_state(ws.id)
        self.assertEqual(state["active_generation"], active)
        self.assertEqual(state["active_status"], "active")
        os.close()

    def test_local_incomplete_generation_stays_inactive(self):
        graph = LocalTemporalGraph()
        graph.rebuild_workspace("g1", [{"workspace_id": "ws", "source_id": "src", "content": "launch", "observed_at": "2026-01-01T00:00:00+00:00"}])
        graph.activate_generation("ws", "g1")
        with self.assertRaises(ValueError):
            graph.rebuild_workspace("g2", [
                {"workspace_id": "ws", "source_id": "src", "content": "new", "observed_at": "2026-01-02T00:00:00+00:00"},
                {"workspace_id": "ws", "source_id": "src", "content": "bad", "observed_at": "not-a-date"},
            ])
        self.assertEqual(graph.search("ws", "launch", ["src"])[0]["observed_at"], "2026-01-01T00:00:00+00:00")

    def test_provider_finalized_before_db_switch_keeps_old_generation_queryable(self):
        graph = FakeGraph()
        os, ws, actor = self._setup(graph)
        result = os.ingest_text(ws.id, actor.id, "good", "canonical launch evidence", "memory://good")
        os.rebuild_projections()
        old_generation = os.store.graph_generation_state(ws.id)["active_generation"]
        graph.hits = [{"source_id": result.source.id, "document_id": result.document_id, "prose": "ignored"}]
        original_activate = os.store.activate_graph_generation
        os.store.activate_graph_generation = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("switch crash"))
        try:
            os.rebuild_projections()
        finally:
            os.store.activate_graph_generation = original_activate
        bundle = os.search(ws.id, actor.id, "launch")
        self.assertFalse(bundle.unknown)
        self.assertEqual(os.store.graph_generation_state(ws.id)["active_generation"], old_generation)
        self.assertEqual(graph.search_generations[-1], old_generation)
        os.close()

    def test_provider_rebuild_finishes_before_sqlite_activation(self):
        graph = FakeGraph()
        os, ws, actor = self._setup(graph)
        os.ingest_text(ws.id, actor.id, "good", "canonical evidence", "memory://good")
        original_activate = os.store.activate_graph_generation

        def checked_activate(workspace_id, generation):
            self.assertEqual(graph.generations[-1], generation)
            original_activate(workspace_id, generation)

        os.store.activate_graph_generation = checked_activate
        try:
            os.rebuild_projections()
        finally:
            os.store.activate_graph_generation = original_activate
        os.close()

    def test_search_passes_active_generation_and_authorized_source_ids(self):
        graph = FakeGraph()
        os, ws, admin = self._setup(graph)
        actor = os.create_actor(ws.id, "Operator", "operator", "act_operator")
        allowed = os.ingest_text(ws.id, admin.id, "allowed", "visible launch", "memory://allowed")
        os.ingest_text(
            ws.id, admin.id, "restricted", "private launch", "memory://restricted",
            allowed_actor_ids=["someone_else"],
        )
        os.rebuild_projections()
        active = os.store.graph_generation_state(ws.id)["active_generation"]
        os.search(ws.id, actor.id, "launch")
        self.assertEqual(graph.search_generations[-1], active)
        self.assertEqual(graph.search_allowed_source_ids[-1], [allowed.source.id])
        os.close()

    def test_workspace_wide_provider_is_skipped_for_partial_acl(self):
        graph = FakeGraph()
        graph.requires_full_workspace_access = True
        os, ws, admin = self._setup(graph)
        actor = os.create_actor(ws.id, "Operator", "operator", "act_operator")
        os.ingest_text(ws.id, admin.id, "allowed", "visible launch", "memory://allowed")
        os.ingest_text(
            ws.id, admin.id, "restricted", "private launch", "memory://restricted",
            allowed_actor_ids=["someone_else"],
        )
        os.rebuild_projections()
        result = os.search(ws.id, actor.id, "launch")
        self.assertEqual(graph.search_generations, [])
        self.assertEqual(result.retrieval["graph"], "restricted")
        self.assertEqual(result.retrieval["graph_detail"], "partial_acl")
        os.close()

    def test_current_time_provider_is_not_called_for_explicit_historical_search(self):
        graph = FakeGraph()
        graph.requires_full_workspace_access = True
        graph.uses_current_time_search = True
        os, ws, actor = self._setup(graph)
        ingested = os.ingest_text(ws.id, actor.id, "historical", "launch evidence", "memory://history")
        os.rebuild_projections()
        document = os.store.get_document(ws.id, ingested.document_id)
        result = os.search(ws.id, actor.id, "launch", as_of=document.recorded_at)
        self.assertEqual(graph.search_generations, [])
        self.assertEqual(result.retrieval["graph"], "skipped")
        self.assertEqual(result.retrieval["graph_detail"], "historical_query")
        self.assertTrue(result.items)  # canonical FTS/semantic channels remain available
        os.close()

    def test_rebuild_snapshot_fence_rejects_ingest_during_provider_build(self):
        graph = FakeGraph()
        os, ws, actor = self._setup(graph)
        os.ingest_text(ws.id, actor.id, "one", "canonical one", "memory://one")
        os.rebuild_projections()
        old_generation = os.store.graph_generation_state(ws.id)["active_generation"]
        graph.on_rebuild = lambda: os.ingest_text(ws.id, actor.id, "two", "canonical two", "memory://two")
        os.rebuild_projections()
        state = os.store.graph_generation_state(ws.id)
        self.assertEqual(state["active_generation"], old_generation)
        self.assertEqual(state["active_status"], "active")
        self.assertEqual(os.graph_health["status"], "healthy")
        os.close()


if __name__ == "__main__":
    unittest.main()
