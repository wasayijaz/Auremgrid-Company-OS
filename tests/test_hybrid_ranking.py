from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from auremgrid.adapters.hybrid import HybridRanker, RankedHit
from auremgrid.services.brain import CompanyOS


class HybridRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ranker = HybridRanker()
        self.as_of = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)

    def _hit(
        self,
        key: str,
        *,
        score: float = 0.8,
        trust_level: str = "internal",
        observed_at: datetime | None = None,
        recorded_at: datetime | None = None,
        channel: str = "keyword",
    ) -> RankedHit:
        timestamp = observed_at or self.as_of
        return RankedHit(
            "document",
            key,
            score,
            (channel,),
            trust_level,
            timestamp,
            recorded_at or timestamp,
        )

    def test_authority_and_recency_are_bounded_and_explainable(self) -> None:
        ranked = self.ranker.fuse(
            [
                self._hit("older-internal", observed_at=self.as_of - timedelta(days=365)),
                self._hit("fresh-external", trust_level="external"),
                self._hit("fresh-internal"),
            ],
            as_of=self.as_of,
        )

        self.assertEqual([hit.key for hit in ranked], ["fresh-internal", "fresh-external", "older-internal"])
        for hit in ranked:
            self.assertGreaterEqual(hit.score, 0.0)
            self.assertLessEqual(hit.score, 1.0)
            components = dict(hit.score_components)
            self.assertAlmostEqual(
                hit.score,
                components["relevance_contribution"]
                + components["authority_contribution"]
                + components["recency_contribution"],
                places=5,
            )
            self.assertEqual(
                set(components),
                {
                    "relevance", "authority", "recency",
                    "relevance_contribution", "authority_contribution", "recency_contribution",
                },
            )

    def test_channels_are_independent_then_fused_without_unbounded_score(self) -> None:
        ranked = self.ranker.fuse(
            [
                self._hit("corroborated", score=0.6, channel="keyword"),
                self._hit("corroborated", score=0.7, channel="vector"),
                self._hit("single", score=0.7, channel="graph"),
            ],
            as_of=self.as_of,
        )

        self.assertEqual(ranked[0].key, "corroborated")
        self.assertEqual(ranked[0].channels, ("keyword", "vector"))
        self.assertLessEqual(ranked[0].score, 1.0)

        duplicate = self.ranker.fuse(
            [
                self._hit("same", score=0.6, channel="keyword"),
                self._hit("same", score=0.6, channel="keyword"),
            ],
            as_of=self.as_of,
        )[0]
        self.assertEqual(dict(duplicate.score_components)["relevance"], 0.6)

    def test_fixed_as_of_and_tie_break_are_deterministic(self) -> None:
        hits = [self._hit("z"), self._hit("a")]
        first = self.ranker.fuse(hits, as_of=self.as_of)
        second = self.ranker.fuse(list(reversed(hits)), as_of=self.as_of)

        self.assertEqual([(hit.key, hit.score) for hit in first], [(hit.key, hit.score) for hit in second])
        self.assertEqual([hit.key for hit in first], ["a", "z"])

    def test_unknown_trust_is_neutral_and_not_inferred_from_locator_or_key(self) -> None:
        ranked = self.ranker.fuse(
            [self._hit("internal-looking-key", trust_level="not-configured")],
            as_of=self.as_of,
        )
        components = dict(ranked[0].score_components)
        self.assertEqual(components["authority"], 0.5)


class SearchRankingContractTests(unittest.TestCase):
    def test_search_exposes_features_only_for_temporally_and_acl_eligible_candidates(self) -> None:
        os = CompanyOS()
        workspace = os.create_workspace("Ranking", "ws_ranking")
        reader = os.create_actor(workspace.id, "Reader", "operator", "act_reader")
        admin = os.create_actor(workspace.id, "Admin", "admin", "act_admin")
        cutoff = datetime.now(timezone.utc).replace(microsecond=0)
        visible = os.ingest_text(
            workspace.id,
            admin.id,
            "visible",
            "launch readiness visible",
            "memory://visible",
            allowed_actor_ids=[reader.id],
            observed_at=cutoff - timedelta(days=1),
            trust_level="internal",
        )
        hidden = os.ingest_text(
            workspace.id,
            admin.id,
            "hidden",
            "launch readiness hidden",
            "memory://hidden",
            allowed_actor_ids=[admin.id],
            observed_at=cutoff - timedelta(days=1),
            trust_level="internal",
        )
        future = os.ingest_text(
            workspace.id,
            admin.id,
            "future",
            "launch readiness future",
            "memory://future",
            allowed_actor_ids=[reader.id],
            observed_at=cutoff + timedelta(days=1),
            trust_level="internal",
        )

        bundle = os.search(workspace.id, reader.id, "launch", as_of=cutoff, limit=20)
        document_ids = {item.payload.get("document_id") for item in bundle.items}
        self.assertIn(visible.document_id, document_ids)
        self.assertNotIn(hidden.document_id, document_ids)
        self.assertNotIn(future.document_id, document_ids)
        self.assertEqual(bundle.retrieval["semantic_hits"], 1)
        self.assertEqual(bundle.retrieval["ranking"]["contract"], "hybrid-authority-recency-v1")
        self.assertEqual(sum(bundle.retrieval["ranking"]["weights"].values()), 1.0)
        for item in bundle.items:
            self.assertEqual(
                set(item.payload["score_components"]),
                {
                    "relevance", "authority", "recency",
                    "relevance_contribution", "authority_contribution", "recency_contribution",
                },
            )
        os.close()


if __name__ == "__main__":
    unittest.main()
