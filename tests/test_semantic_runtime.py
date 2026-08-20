from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auremgrid import cli
from auremgrid.adapters.semantic import (
    DeterministicFallbackEmbeddingProvider,
    EmbeddingProviderError,
    SentenceTransformerEmbeddingProvider,
    embedding_provider_from_config,
)
from auremgrid.services.brain import CompanyOS


class _LocalModel:
    def __init__(self, dimensions: int = 3) -> None:
        self.dimensions = dimensions

    def get_sentence_embedding_dimension(self) -> int:
        return self.dimensions

    def encode(self, texts, *, normalize_embeddings):
        assert normalize_embeddings is True
        return [[1.0] + [0.0] * (self.dimensions - 1) for _ in texts]


class _BrokenLocalModel(_LocalModel):
    def encode(self, texts, *, normalize_embeddings):
        raise RuntimeError("corrupt local weights")


class SemanticRuntimeTests(unittest.TestCase):
    def test_offline_provider_is_default_and_reports_fallback_truthfully(self) -> None:
        provider = embedding_provider_from_config()
        self.assertIsInstance(provider, DeterministicFallbackEmbeddingProvider)
        health = provider.health().to_dict()
        self.assertEqual(health["status"], "healthy")
        self.assertTrue(health["fallback_used"])
        self.assertEqual(health["dimensions"], 64)

    def test_local_provider_loads_injected_model_lazily_and_freezes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def loader(path: Path):
                calls.append(path)
                return _LocalModel(3)

            provider = embedding_provider_from_config(
                model_path=tmp,
                model="acme/local-mini",
                version="weights-2026-08-01",
                loader=loader,
            )
            self.assertEqual(calls, [])
            self.assertEqual(provider.health().status, "configured")
            self.assertFalse(provider.health().fallback_used)
            self.assertEqual(provider.embed(["first"]), [(1.0, 0.0, 0.0)])
            self.assertEqual(calls, [Path(tmp)])
            self.assertEqual(provider.health().to_dict(), {
                "provider": "sentence_transformers_local",
                "model": "acme/local-mini",
                "version": "weights-2026-08-01",
                "dimensions": 3,
                "status": "healthy",
                "detail": None,
                "fallback_used": False,
            })

    def test_missing_model_path_degrades_semantic_without_blocking_fts_or_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "not-installed"
            provider = SentenceTransformerEmbeddingProvider(
                missing, model="missing", version="1",
            )
            os = CompanyOS(embedding_provider=provider)
            self.assertEqual(os.embedding_health["status"], "degraded")
            ws = os.create_workspace("Client", "ws_client")
            actor = os.create_actor(ws.id, "Admin", "admin", "act_admin")
            result = os.ingest_text(ws.id, actor.id, "brief", "launch readiness", "memory://brief")
            bundle = os.search(ws.id, actor.id, "launch")
            self.assertEqual(bundle.retrieval["fts"], "healthy")
            self.assertEqual(bundle.retrieval["semantic"], "degraded")
            self.assertFalse(bundle.retrieval["fallback_used"])
            self.assertIn(result.document_id, {item.payload.get("document_id") for item in bundle.items})
            self.assertEqual(provider.health().status, "degraded")
            os.close()

    def test_missing_optional_dependency_isolated_until_embedding_is_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = SentenceTransformerEmbeddingProvider(
                tmp,
                model="local-model",
                version="1",
                loader=lambda _path: (_ for _ in ()).throw(ModuleNotFoundError("sentence_transformers")),
            )
            self.assertEqual(provider.embed([]), [])
            self.assertEqual(provider.health().status, "configured")
            with self.assertRaises(EmbeddingProviderError):
                provider.embed(["load now"])
            health = provider.health()
            self.assertEqual(health.status, "degraded")
            self.assertFalse(health.fallback_used)

    def test_model_execution_failure_is_reflected_in_provider_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = SentenceTransformerEmbeddingProvider(
                tmp, model="broken", version="1", loader=lambda _path: _BrokenLocalModel(),
            )
            with self.assertRaises(EmbeddingProviderError):
                provider.embed(["load"])
            self.assertEqual(provider.health().status, "degraded")
            self.assertIn("corrupt local weights", provider.health().detail or "")

    def test_restart_rebuilds_projection_under_explicit_new_version_and_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_path = root / "model"
            model_path.mkdir()
            db_path = root / "semantic.sqlite"
            first_provider = SentenceTransformerEmbeddingProvider(
                model_path, model="local-mini", version="v1", loader=lambda _path: _LocalModel(2),
            )
            first = CompanyOS(db_path, embedding_provider=first_provider)
            ws = first.create_workspace("Client", "ws_client")
            actor = first.create_actor(ws.id, "Admin", "admin", "act_admin")
            first.ingest_text(ws.id, actor.id, "brief", "launch readiness", "memory://brief")
            first.close()

            second_provider = SentenceTransformerEmbeddingProvider(
                model_path, model="local-mini", version="v2", loader=lambda _path: _LocalModel(4),
            )
            second = CompanyOS(db_path, embedding_provider=second_provider)
            row = second.store.conn.execute(
                "SELECT provider,model,provider_version,dimensions FROM document_embedding_projection"
            ).fetchone()
            self.assertEqual(
                tuple(row),
                ("sentence_transformers_local", "local-mini", "v2", 4),
            )
            self.assertEqual(second.embedding_health["status"], "healthy")
            self.assertEqual(second.embedding_health["dimensions"], 4)
            second.close()

    def test_cli_requires_complete_identity_and_passes_local_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                cli.main(["demo", "--semantic-model-path", tmp])

            captured = []

            class FakeResult:
                def to_dict(self):
                    return {}

            class FakeOS:
                def __init__(self, db, *, embedding_provider, graph_projection):
                    captured.append((db, embedding_provider, graph_projection))

                def seed_demo(self):
                    return None

                def search(self, *args):
                    return FakeResult()

                def close(self):
                    return None

            with patch.object(cli, "CompanyOS", FakeOS):
                self.assertEqual(cli.main([
                    "demo",
                    "--semantic-model-path", tmp,
                    "--semantic-model", "local-mini",
                    "--semantic-version", "v7",
                ]), 0)
            provider = captured[0][1]
            self.assertEqual(provider.model, "local-mini")
            self.assertEqual(provider.version, "v7")
            self.assertEqual(provider.model_path, Path(tmp))
            self.assertIsNone(captured[0][2])

    def test_cli_local_environment_config_is_opt_in_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = type("Args", (), {
                "semantic_model_path": None,
                "semantic_model": None,
                "semantic_version": None,
            })()
            with patch.dict("auremgrid.cli.environ", {
                "AUREMGRID_SEMANTIC_MODEL_PATH": tmp,
                "AUREMGRID_SEMANTIC_MODEL": "env-local-mini",
                "AUREMGRID_SEMANTIC_VERSION": "env-v1",
            }, clear=True):
                provider = cli._embedding_provider(args)
            self.assertEqual(provider.model_path, Path(tmp))
            self.assertEqual(provider.model, "env-local-mini")
            self.assertEqual(provider.version, "env-v1")


if __name__ == "__main__":
    unittest.main()
