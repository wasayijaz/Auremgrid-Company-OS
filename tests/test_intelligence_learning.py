from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
import tempfile
import unittest
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class IntelligenceLearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.os.seed_demo(FIXTURES)
        self.org = "org_demo"
        self.ws = "ws_alpha"
        self.person = "person_demo_owner"
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def tearDown(self) -> None:
        self.os.close()

    def _source_ref(self) -> dict[str, str]:
        row = self.os.store.conn.execute(
            "SELECT id FROM sources WHERE workspace_id=? ORDER BY recorded_at,id LIMIT 1",
            (self.ws,),
        ).fetchone()
        self.assertIsNotNone(row)
        return {"type": "source", "id": row["id"]}

    def _recommendation(self, *, idempotency_key: str | None = None) -> dict:
        start = self.now.isoformat()
        end = (self.now + timedelta(days=7)).isoformat()
        return self.os.intelligence_learning.record_recommendation(
            self.org,
            self.ws,
            self.person,
            "Use cited delivery evidence before changing the plan.",
            runbook_id="client_growth_diagnosis",
            runbook_version=1,
            profile_contributors=[{"profile_id": "cosmo_strategy_architect", "version": 1, "role": "lead"}],
            confidence=0.72,
            options=[{"id": "review", "label": "Review evidence"}, {"id": "defer", "label": "Defer"}],
            recommended_option_id="review",
            evidence_refs=[self._source_ref()],
            evaluation_window_start=start,
            evaluation_window_end=end,
            generated_by={"type": "runbook", "id": "client_growth_diagnosis"},
            idempotency_key=idempotency_key,
        )

    def test_migration_43_is_append_only_and_keeps_contracts(self) -> None:
        self.assertGreaterEqual(self.os.store.schema_version, 43)
        applied = self.os.store.conn.execute(
            "SELECT name FROM schema_migrations WHERE version=43"
        ).fetchone()
        self.assertIsNotNone(applied)
        self.assertEqual(applied["name"], "intelligence_learning_persistence")
        hypothesis = self.os.intelligence_learning.record_hypothesis(
            self.org,
            self.ws,
            self.person,
            "Interpretation: delivery delay may be caused by approval ambiguity.",
            evidence_for_refs=[self._source_ref()],
            evidence_against_refs=[],
            confidence=0.61,
            assumptions=["approval queue is current"],
            generated_by={"type": "expert_profile", "id": "cosmo_delivery_lead"},
        )
        recommendation = self._recommendation()

        with self.assertRaises(sqlite3.IntegrityError):
            self.os.store.conn.execute(
                "UPDATE intelligence_hypotheses SET text='fact' WHERE id=?",
                (hypothesis["id"],),
            )
        self.os.store.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.os.store.conn.execute(
                "DELETE FROM intelligence_recommendations WHERE id=?",
                (recommendation["id"],),
            )
        self.os.store.conn.rollback()

        audit_count = self.os.store.conn.execute(
            """SELECT COUNT(*) FROM ledger_audit
               WHERE organization_id=? AND workspace_id=?
                 AND entity_type IN ('intelligence_hypothesis','intelligence_recommendation')""",
            (self.org, self.ws),
        ).fetchone()[0]
        self.assertEqual(audit_count, 2)

    def test_model_and_expert_results_do_not_promote_facts_or_decisions(self) -> None:
        before_facts = self.os.store.conn.execute("SELECT COUNT(*) FROM facts WHERE workspace_id=?", (self.ws,)).fetchone()[0]
        before_decisions = self.os.store.conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE organization_id=? AND workspace_id=?",
            (self.org, self.ws),
        ).fetchone()[0]
        self.os.intelligence_learning.record_hypothesis(
            self.org,
            self.ws,
            self.person,
            "Interpretation only: source language may indicate stakeholder concern.",
            evidence_for_refs=[self._source_ref()],
            confidence=0.55,
            generated_by={"type": "model", "id": "deterministic-test-provider"},
        )
        self._recommendation()
        after_facts = self.os.store.conn.execute("SELECT COUNT(*) FROM facts WHERE workspace_id=?", (self.ws,)).fetchone()[0]
        after_decisions = self.os.store.conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE organization_id=? AND workspace_id=?",
            (self.org, self.ws),
        ).fetchone()[0]
        self.assertEqual(after_facts, before_facts)
        self.assertEqual(after_decisions, before_decisions)

    def test_acl_isolation_and_evidence_refs_are_strictly_scoped(self) -> None:
        viewer = self.os.create_person("org_demo", "Learning Viewer", "learning-viewer@test.invalid", person_id="person_learning_viewer")
        self.os.add_person_to_workspace("org_demo", self.ws, viewer.id, "viewer")
        with self.assertRaises(AuthorizationError):
            self.os.intelligence_learning.record_hypothesis(
                self.org,
                self.ws,
                viewer.id,
                "Viewer cannot write.",
                evidence_for_refs=[self._source_ref()],
            )

        outsider = self.os.create_person("org_demo", "Learning Outsider", "learning-outsider@test.invalid", person_id="person_learning_outsider")
        with self.assertRaises(AuthorizationError):
            self.os.intelligence_learning.workspace_learning(self.org, self.ws, outsider.id)

        beta_source = self.os.store.conn.execute(
            "SELECT id FROM sources WHERE workspace_id='ws_beta' ORDER BY recorded_at,id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(beta_source)
        with self.assertRaises(NotFoundError):
            self.os.intelligence_learning.record_hypothesis(
                self.org,
                self.ws,
                self.person,
                "Cross-workspace evidence should fail.",
                evidence_for_refs=[{"type": "source", "id": beta_source["id"]}],
            )

    def test_idempotent_lifecycle_and_payload_conflicts(self) -> None:
        first = self._recommendation(idempotency_key="rec-key")
        second = self._recommendation(idempotency_key="rec-key")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM intelligence_recommendations WHERE id=?", (first["id"],)).fetchone()[0],
            1,
        )
        with self.assertRaises(ValidationError):
            self.os.intelligence_learning.record_recommendation(
                self.org,
                self.ws,
                self.person,
                "Different payload.",
                runbook_id="client_growth_diagnosis",
                runbook_version=1,
                profile_contributors=[{"profile_id": "cosmo_strategy_architect", "version": 1}],
                confidence=0.72,
                options=[{"id": "review"}],
                recommended_option_id="review",
                evidence_refs=[self._source_ref()],
                evaluation_window_start=self.now.isoformat(),
                evaluation_window_end=(self.now + timedelta(days=7)).isoformat(),
                idempotency_key="rec-key",
            )

        event1 = self.os.intelligence_learning.append_recommendation_event(
            self.org,
            self.ws,
            self.person,
            first["id"],
            "chosen",
            chosen_option_id="review",
            idempotency_key="life-key",
        )
        event2 = self.os.intelligence_learning.append_recommendation_event(
            self.org,
            self.ws,
            self.person,
            first["id"],
            "chosen",
            chosen_option_id="review",
            idempotency_key="life-key",
        )
        self.assertEqual(event1["id"], event2["id"])
        with self.assertRaises(ValidationError):
            self.os.intelligence_learning.append_recommendation_event(
                self.org,
                self.ws,
                self.person,
                first["id"],
                "chosen",
                chosen_option_id="unknown",
            )

    def test_evaluation_outcome_attribution_requires_scope_time_and_matching_evidence(self) -> None:
        recommendation = self._recommendation()
        work = self.os.work_ops.create(
            self.org,
            self.ws,
            self.person,
            "Measure recommendation outcome",
            "Confirm the recommended review happened.",
            self.person,
        )
        outcome = {
            "type": "work_item",
            "id": work.id,
            "occurred_at": (self.now + timedelta(days=1)).isoformat(),
            "metric": "completed_review",
            "value": 1,
        }
        with self.assertRaises(ValidationError):
            self.os.intelligence_learning.append_recommendation_event(
                self.org,
                self.ws,
                self.person,
                recommendation["id"],
                "evaluated",
                measured_outcomes=[outcome],
                evidence_refs=[self._source_ref()],
                score=0.8,
                lessons="The review reduced ambiguity.",
            )

        event = self.os.intelligence_learning.append_recommendation_event(
            self.org,
            self.ws,
            self.person,
            recommendation["id"],
            "evaluated",
            measured_outcomes=[outcome],
            evidence_refs=[{"type": "work_item", "id": work.id}],
            score=0.8,
            lessons="The review reduced ambiguity.",
        )
        self.assertEqual(event["score"], 0.8)

        late = {**outcome, "occurred_at": (self.now + timedelta(days=30)).isoformat()}
        with self.assertRaises(ValidationError):
            self.os.intelligence_learning.append_recommendation_event(
                self.org,
                self.ws,
                self.person,
                recommendation["id"],
                "evaluated",
                measured_outcomes=[late],
                evidence_refs=[{"type": "work_item", "id": work.id}],
                score=0.4,
            )

    def test_learning_persists_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.sqlite"
            first = CompanyOS(path)
            try:
                first.seed_demo(FIXTURES)
                recommendation = first.intelligence_learning.record_recommendation(
                    self.org,
                    self.ws,
                    self.person,
                    "Persistent recommendation.",
                    runbook_id="client_growth_diagnosis",
                    runbook_version=1,
                    profile_contributors=[{"profile_id": "cosmo_strategy_architect", "version": 1}],
                    confidence=0.7,
                    options=[{"id": "review"}],
                    recommended_option_id="review",
                    evidence_refs=[{"type": "source", "id": first.store.conn.execute("SELECT id FROM sources WHERE workspace_id=? LIMIT 1", (self.ws,)).fetchone()["id"]}],
                    evaluation_window_start=self.now.isoformat(),
                    evaluation_window_end=(self.now + timedelta(days=7)).isoformat(),
                )
            finally:
                first.close()

            second = CompanyOS(path)
            try:
                learning = second.intelligence_learning.workspace_learning(self.org, self.ws, self.person)
                self.assertEqual([item["id"] for item in learning["recommendations"]], [recommendation["id"]])
                self.assertGreaterEqual(second.store.schema_version, 43)
                applied = second.store.conn.execute(
                    "SELECT name FROM schema_migrations WHERE version=43"
                ).fetchone()
                self.assertIsNotNone(applied)
            finally:
                second.close()


if __name__ == "__main__":
    unittest.main()
