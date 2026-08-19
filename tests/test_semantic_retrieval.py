from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auremgrid.adapters.semantic import EmbeddingHealth, EmbeddingProviderError
from auremgrid.services.brain import CompanyOS


class TinySemanticProvider:
    name = "test-semantic"
    model = "tiny"
    version = "1"
    dimensions = 2

    def health(self) -> EmbeddingHealth:
        return EmbeddingHealth(self.name, self.model, self.version, self.dimensions)

    def embed(self, texts):
        vectors = []
        for text in texts:
            vectors.append((1.0, 0.0) if "launch" in text.lower() or "release" in text.lower() else (0.0, 1.0))
        return vectors


class FailingProvider(TinySemanticProvider):
    name = "failing-semantic"

    def embed(self, texts):
        raise EmbeddingProviderError("provider unavailable")


class RecordingIndex:
    def __init__(self):
        self.vectors = {}
        self.allowed = []

    def upsert(self, workspace_id, key, vector):
        self.vectors[(workspace_id, key)] = vector

    def search(self, workspace_id, vector, allowed_document_ids, limit=8):
        self.allowed.append((workspace_id, tuple(allowed_document_ids)))
        allowed = set(allowed_document_ids)
        return [(key, 1.0) for (ws, key), candidate in self.vectors.items() if ws == workspace_id and key in allowed][:limit]


class SemanticRetrievalTests(unittest.TestCase):
    def _workspace(self, os, name="Client"):
        ws = os.create_workspace(name, f"ws_{name.lower()}")
        actor = os.create_actor(ws.id, "Admin", "admin", f"act_{name.lower()}")
        return ws, actor

    def test_synonym_semantic_hit_is_independent_of_fts(self):
        os = CompanyOS(embedding_provider=TinySemanticProvider())
        ws, actor = self._workspace(os)
        result = os.ingest_text(ws.id, actor.id, "brief", "release readiness", "memory://brief")
        bundle = os.search(ws.id, actor.id, "launch")
        self.assertFalse(bundle.unknown)
        self.assertEqual(bundle.retrieval["semantic"], "healthy")
        self.assertEqual(bundle.retrieval["fts"], "healthy")
        self.assertIn(result.document_id, {item.payload["document_id"] for item in bundle.items})
        self.assertTrue(any("vector" in item.payload["channels"] for item in bundle.items))
        os.close()

    def test_allowed_document_ids_are_enforced_before_vector_search(self):
        index = RecordingIndex()
        os = CompanyOS(embedding_provider=TinySemanticProvider(), vector_index=index)
        ws1, actor1 = self._workspace(os, "One")
        ws2, actor2 = self._workspace(os, "Two")
        first = os.ingest_text(ws1.id, actor1.id, "one", "launch plan", "memory://one")
        second = os.ingest_text(ws2.id, actor2.id, "two", "launch plan", "memory://two")
        bundle = os.search(ws1.id, actor1.id, "launch")
        self.assertIn((ws1.id, (first.document_id,)), index.allowed)
        self.assertNotIn(second.document_id, {item.payload.get("document_id") for item in bundle.items})
        os.close()

    def test_future_and_retired_documents_are_excluded_from_all_channels(self):
        os = CompanyOS(embedding_provider=TinySemanticProvider())
        ws, actor = self._workspace(os)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        future = os.ingest_text(ws.id, actor.id, "future", "launch future", "memory://future", observed_at=now + timedelta(days=1))
        old = os.ingest_text(ws.id, actor.id, "old", "launch old", "memory://old", observed_at=now - timedelta(days=2))
        os.store.retire_source(ws.id, old.source.id, now)
        bundle = os.search(ws.id, actor.id, "launch", as_of=now)
        ids = {item.payload.get("document_id") for item in bundle.items}
        self.assertNotIn(future.document_id, ids)
        self.assertNotIn(old.document_id, ids)
        os.close()

    def test_provider_outage_keeps_fts_evidence_and_exposes_degraded_semantic(self):
        os = CompanyOS(embedding_provider=FailingProvider())
        ws, actor = self._workspace(os)
        result = os.ingest_text(ws.id, actor.id, "brief", "launch readiness", "memory://brief")
        bundle = os.search(ws.id, actor.id, "launch")
        self.assertFalse(bundle.unknown)
        self.assertEqual(bundle.retrieval["semantic"], "degraded")
        self.assertFalse(bundle.retrieval["fallback_used"])
        self.assertTrue(any(item.payload.get("document_id") == result.document_id for item in bundle.items))
        os.close()

    def test_deterministic_hash_collisions_do_not_create_unknown_evidence(self):
        os = CompanyOS()
        ws, actor = self._workspace(os)
        os.ingest_text(ws.id, actor.id, "brief", "approved launch readiness", "memory://brief")
        bundle = os.search(ws.id, actor.id, "unrelated phrase absent from evidence")
        self.assertTrue(bundle.unknown)
        self.assertEqual(bundle.retrieval["fallback_used"], True)
        os.close()

    def test_vectors_are_float32_durable_and_restart_rebuildable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "semantic.sqlite"
            first = CompanyOS(path)
            ws, actor = self._workspace(first)
            result = first.ingest_text(ws.id, actor.id, "brief", "launch readiness", "memory://brief")
            row = first.store.conn.execute(
                "SELECT provider,model,provider_version,dimensions,vector,health FROM document_embedding_projection"
            ).fetchone()
            self.assertEqual(row["dimensions"], 64)
            self.assertEqual(len(row["vector"]), 64 * 4)
            first.close()
            second = CompanyOS(path)
            self.assertEqual(second.store.schema_version, 16)
            bundle = second.search(ws.id, actor.id, "launch")
            self.assertIn(result.document_id, {item.payload.get("document_id") for item in bundle.items})
            second.close()

    def test_historical_semantic_hit_survives_restart_after_source_retirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "historical.sqlite"
            base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=2)
            first = CompanyOS(path, embedding_provider=TinySemanticProvider())
            ws, actor = self._workspace(first)
            result = first.ingest_text(
                ws.id, actor.id, "brief", "release readiness", "memory://brief", observed_at=base
            )
            first.store.retire_source(ws.id, result.source.id, base + timedelta(days=2))
            as_of = base + timedelta(days=1)
            before = first.search(ws.id, actor.id, "launch", as_of=as_of)
            self.assertIn(result.document_id, {item.payload.get("document_id") for item in before.items})
            first.close()
            second = CompanyOS(path, embedding_provider=TinySemanticProvider())
            after = second.search(ws.id, actor.id, "launch", as_of=as_of)
            self.assertIn(result.document_id, {item.payload.get("document_id") for item in after.items})
            current = second.search(ws.id, actor.id, "launch")
            self.assertNotIn(result.document_id, {item.payload.get("document_id") for item in current.items})
            status_text = repr(second.engine_status(ws.id, actor.id, "launch"))
            self.assertNotIn("release readiness", status_text)
            self.assertNotIn(result.source.id, status_text)
            second.close()


if __name__ == "__main__":
    unittest.main()
