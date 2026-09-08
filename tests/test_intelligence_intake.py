from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.api.http import serve
from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.services.intelligence_intake import SUCCESS_QUESTIONS, IntelligenceIntakeStore
from tests.auth_support import issue_identity


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class IntelligenceIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.os.seed_demo(FIXTURES)
        self.token, self.identity = issue_identity(
            self.os, "org_demo", "person_demo_owner", "ws_alpha", "act_alpha_admin"
        )

    def tearDown(self) -> None:
        self.os.close()

    def _store(self) -> IntelligenceIntakeStore:
        return IntelligenceIntakeStore(self.os.store.conn, self.os.jobs.new_id)

    def test_catalog_is_fixed_and_keys_are_stable(self) -> None:
        self.assertEqual(
            [item["key"] for item in SUCCESS_QUESTIONS],
            [
                "success_outcome",
                "success_metrics",
                "hard_constraints",
                "communication_standard",
                "human_consult_triggers",
            ],
        )

    def test_missing_answers_stay_absent_from_context(self) -> None:
        workspace = self.os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin"
        )
        self.assertEqual(workspace["scope_contract"]["success_criteria"], [])

    def test_answered_questions_surface_as_explicit_success_criteria(self) -> None:
        store = self._store()
        store.answer_question(
            "org_demo", "success_metrics",
            "Net revenue retention, utilization, on-time delivery", "person_demo_owner",
        )
        store.answer_question(
            "org_demo", "hard_constraints",
            "Never ship unreviewed client deliverables", "person_demo_owner",
        )
        workspace = self.os.intelligence.workspace(
            "org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin"
        )
        criteria = workspace["scope_contract"]["success_criteria"]
        self.assertEqual(
            [row["question_key"] for row in criteria],
            ["hard_constraints", "success_metrics"],
        )
        self.assertTrue(all(row["answer"] and row["answered_by"] for row in criteria))
        self.assertTrue(all(row["question"] for row in criteria))

    def test_reanswer_updates_one_row_per_question_key(self) -> None:
        store = self._store()
        first = store.answer_question(
            "org_demo", "success_outcome", "Client renews the retainer", "person_demo_owner"
        )
        second = store.answer_question(
            "org_demo", "success_outcome",
            "Client renews and expands into a second retainer", "person_demo_owner",
        )
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertGreaterEqual(second["updated_at"], second["created_at"])
        self.assertEqual(second["answer"], "Client renews and expands into a second retainer")
        count = self.os.store.conn.execute(
            "SELECT COUNT(*) FROM intelligence_success_definitions WHERE organization_id=? AND question_key='success_outcome'",
            ("org_demo",),
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_invalid_question_key_and_empty_answer_are_rejected(self) -> None:
        store = self._store()
        with self.assertRaises(ValidationError):
            store.answer_question("org_demo", "made_up_key", "text", "person_demo_owner")
        with self.assertRaises(ValidationError):
            store.answer_question("org_demo", "success_outcome", "   ", "person_demo_owner")

    def test_questions_catalog_lists_answers_without_inventing_them(self) -> None:
        payload = self._store().questions("org_demo")
        self.assertEqual(len(payload["questions"]), len(SUCCESS_QUESTIONS))
        self.assertTrue(all(item["answer"] is None for item in payload["questions"]))

    def test_executive_brief_contract_matches_the_render_path(self) -> None:
        brief = self.os.intelligence.executive_brief("org_demo", "person_demo_owner")
        self.assertEqual(brief["type"], "executive_brief")
        # Exactly the sections the executive render path reads or the engine
        # tests pin; nothing rendered-only-by-nobody remains.
        self.assertEqual(
            set(brief["sections"].keys()),
            {"attention", "top_three", "narrative", "constraints"},
        )
        self.assertNotIn("conclusions", brief)
        portfolio = brief["portfolio"]
        for field in ("client_count", "open_work", "open_risks", "stalled_reviews", "capacity_overloaded", "finance"):
            self.assertIn(field, portfolio)
        self.assertIn("status", portfolio["finance"])
        self.assertIn("recognized_revenue", portfolio["finance"])
        for item in brief["sections"]["attention"]:
            for field in ("workspace_id", "workspace_name", "title", "confidence", "evidence"):
                self.assertIn(field, item)
        self.assertIn("what_happens_if_do_nothing", brief)
        self.assertIn(brief["what_happens_if_do_nothing"]["status"], {"evidence_backed", "unknown"})

    def test_http_success_definition_roundtrip_is_authenticated_and_scoped(self) -> None:
        server = serve(self.os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address

        def request(method: str, path: str, token: str | None = self.token, payload: dict | None = None) -> tuple[int, dict]:
            connection = HTTPConnection(host, port, timeout=5)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            body = None
            if payload is not None:
                body = json.dumps(payload)
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = json.loads(response.read())
            connection.close()
            return response.status, data

        try:
            status, _ = request(
                "GET",
                "/dashboard/intelligence/success-definition?organization_id=org_demo&person_id=person_demo_owner",
                None,
            )
            self.assertEqual(status, 401)
            status, body = request(
                "GET",
                "/dashboard/intelligence/success-definition?organization_id=org_demo&person_id=person_demo_owner",
            )
            self.assertEqual(status, 200)
            self.assertEqual(len(body["questions"]), len(SUCCESS_QUESTIONS))
            status, body = request(
                "POST",
                "/dashboard/intelligence/success-definition",
                payload={
                    "organization_id": "org_demo",
                    "person_id": "person_demo_owner",
                    "question_key": "communication_standard",
                    "answer": "Weekly written summary with cited evidence",
                },
            )
            self.assertEqual(status, 201)
            self.assertEqual(body["answer"]["question_key"], "communication_standard")
            status, body = request(
                "GET",
                "/dashboard/intelligence/success-definition?organization_id=org_demo&person_id=person_demo_owner",
            )
            answered = next(item for item in body["questions"] if item["key"] == "communication_standard")
            self.assertEqual(answered["answer"], "Weekly written summary with cited evidence")
            status, _ = request(
                "GET",
                "/dashboard/intelligence/success-definition?organization_id=org_demo&person_id=someone_else",
            )
            self.assertEqual(status, 403)
            status, _ = request(
                "POST",
                "/dashboard/intelligence/success-definition",
                payload={
                    "organization_id": "org_demo",
                    "person_id": "person_demo_owner",
                    "question_key": "made_up_key",
                    "answer": "text",
                },
            )
            self.assertEqual(status, 400)
            status, _ = request(
                "GET",
                "/dashboard/intelligence/success-definition-unmatched?organization_id=org_demo&person_id=person_demo_owner",
            )
            self.assertEqual(status, 404)
            status, _ = request(
                "POST",
                "/dashboard/intelligence/success-definition-unmatched",
                payload={"organization_id": "org_demo"},
            )
            self.assertEqual(status, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
