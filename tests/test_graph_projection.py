from __future__ import annotations

import unittest

from auremgrid.adapters.graphiti_local import LocalTemporalGraph
from auremgrid.services.brain import CompanyOS


class FakeGraph:
    name = "fake-graph"

    def __init__(self, *, fail_upsert=False, fail_rebuild=False):
        self.fail_upsert = fail_upsert
        self.fail_rebuild = fail_rebuild
        self.hits = []
        self.generations = []
        self.search_generations = []
        self.on_rebuild = None

    def health(self):
        return {"status": "healthy", "generation": self.generations[-1] if self.generations else None, "detail": None}

    def upsert_episode(self, workspace_id, source_id, content, observed_at, generation=None):
        if self.fail_upsert:
            raise RuntimeError("network should be hidden")

    def rebuild_workspace(self, generation, episodes):
        if self.fail_rebuild:
            raise RuntimeError("upstream outage")
        self.generations.append(generation)
        if self.on_rebuild is not None:
            self.on_rebuild()

    def search(self, workspace_id, query, allowed_source_ids, as_of=None, limit=8, generation=None):
        self.search_generations.append(generation)
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
