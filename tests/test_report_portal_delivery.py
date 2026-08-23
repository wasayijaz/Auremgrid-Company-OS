from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class ReportPortalDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.other_ws = self.os.create_organization_workspace(self.org.id, "Hidden", "client")
        self.staff = self.os.create_person(self.org.id, "Owner", role="owner")
        self.client = self.os.create_person(self.org.id, "Client Rep", role="client")
        self.other_client = self.os.create_person(self.org.id, "Other Client", role="client")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.staff.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.other_ws.id, self.staff.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.client.id, "client")
        self.os.add_person_to_workspace(self.org.id, self.other_ws.id, self.other_client.id, "client")
        _, self.staff_identity = issue_identity(self.os, self.org.id, self.staff.id, self.ws.id)
        _, self.client_identity = issue_identity(self.os, self.org.id, self.client.id, self.ws.id)
        _, self.other_client_identity = issue_identity(self.os, self.org.id, self.other_client.id, self.other_ws.id)

    def tearDown(self) -> None:
        self.os.close()

    def _report(self, suffix: str = "") -> dict:
        work = self.os.work_ops.create(
            self.org.id, self.ws.id, self.staff.id, f"Launch plan {suffix}", "Ship the launch", "Client",
        )
        self.os.client_ops.create_risk(
            self.org.id, self.ws.id, self.staff.id, "delivery", "high", 0.8,
            f"Schedule pressure {suffix}", "Internal evidence must not leak", "Replan",
        )
        report = self.os.agent_ops.generate_report(self.org.id, self.staff.id, "client_weekly_report", self.ws.id)
        self.assertIn("citations", report)
        self.assertIn("evidence", repr(report["payload"]))
        return report

    def _approval(self, report_id: str, approved: bool = True, policy: str = "human") -> dict:
        approval = self.os.agency_ops.request_approval(
            self.org.id, "person", self.staff.id, f"portal report {report_id}",
            "report.portal_publish", {"report_run_id": report_id},
            "Client-facing portal publication", policy, self.ws.id, self.staff.id if policy != "auto" else None,
        )
        if policy != "auto":
            approval = self.os.agency_ops.decide_approval(self.org.id, self.staff.id, approval["id"], approved, "ok")
        return approval

    def test_publish_requires_human_approval_and_sanitizes_client_snapshot(self) -> None:
        report = self._report()
        pending = self.os.agency_ops.request_approval(
            self.org.id, "person", self.staff.id, "portal report",
            "report.portal_publish", {"report_run_id": report["id"]},
            "Client-facing portal publication", "human", self.ws.id, self.staff.id,
        )
        with self.assertRaises(ValidationError):
            self.os.report_delivery.publish(
                self.staff_identity, self.org.id, self.ws.id, report["id"], pending["id"], "Weekly report",
            )
        auto = self._approval(report["id"], policy="auto")
        with self.assertRaises(ValidationError):
            self.os.report_delivery.publish(
                self.staff_identity, self.org.id, self.ws.id, report["id"], auto["id"], "Weekly report",
            )

        approval = self.os.agency_ops.decide_approval(self.org.id, self.staff.id, pending["id"], True, "approved")
        published = self.os.report_delivery.publish(
            self.staff_identity, self.org.id, self.ws.id, report["id"], approval["id"], "Weekly report",
        )
        self.assertEqual(published["status"], "published")
        self.assertNotIn("citations", repr(published["snapshot"]))
        self.assertNotIn("Internal evidence must not leak", repr(published["snapshot"]))

        visible = self.os.report_delivery.portal_list(self.client_identity, self.org.id, self.ws.id)
        self.assertEqual([item["id"] for item in visible], [published["id"]])
        viewed = self.os.report_delivery.portal_view(self.client_identity, self.org.id, self.ws.id, published["id"])
        self.assertEqual(viewed["snapshot"]["title"], "Weekly report")
        events = [row["action"] for row in self.os.store.conn.execute(
            "SELECT action FROM portal_report_events WHERE portal_report_version_id=? ORDER BY rowid",
            (published["id"],),
        ).fetchall()]
        self.assertEqual(events, ["published", "viewed"])

    def test_supersession_revocation_scope_and_append_only_history(self) -> None:
        first_report = self._report("one")
        first = self.os.report_delivery.publish(
            self.staff_identity, self.org.id, self.ws.id, first_report["id"],
            self._approval(first_report["id"])["id"], "Weekly report v1",
        )
        second_report = self._report("two")
        second = self.os.report_delivery.publish(
            self.staff_identity, self.org.id, self.ws.id, second_report["id"],
            self._approval(second_report["id"])["id"], "Weekly report v2",
        )
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["supersedes_version_id"], first["id"])
        self.assertEqual([item["id"] for item in self.os.report_delivery.portal_list(self.client_identity, self.org.id, self.ws.id)], [second["id"]])
        with self.assertRaises(NotFoundError):
            self.os.report_delivery.portal_view(self.client_identity, self.org.id, self.ws.id, first["id"])
        with self.assertRaises(AuthorizationError):
            self.os.report_delivery.portal_list(self.other_client_identity, self.org.id, self.ws.id)

        self.os.report_delivery.revoke(self.staff_identity, self.org.id, self.ws.id, second["id"], "client asked to retract")
        self.assertEqual(self.os.report_delivery.portal_list(self.client_identity, self.org.id, self.ws.id), [])
        actions = [row["action"] for row in self.os.store.conn.execute(
            "SELECT action FROM portal_report_events ORDER BY rowid"
        ).fetchall()]
        self.assertEqual(actions, ["published", "superseded", "published", "revoked"])
        with self.assertRaises(Exception):
            self.os.store.conn.execute("UPDATE portal_report_versions SET title='rewrite'")

    def test_staff_list_accepts_authorized_dashboard_identity_without_persisted_principal(self) -> None:
        report = self._report()
        published = self.os.report_delivery.publish(
            self.staff_identity, self.org.id, self.ws.id, report["id"],
            self._approval(report["id"])["id"], "Weekly report",
        )
        dashboard_identity = AuthenticatedIdentity(
            "service-dashboard", self.org.id, self.staff.id, "service",
            frozenset({"workspace_read"}), workspace_id=self.ws.id,
        )
        self.assertEqual(
            [item["id"] for item in self.os.report_delivery.staff_list(dashboard_identity, self.org.id, self.ws.id)],
            [published["id"]],
        )
        cross_scope = AuthenticatedIdentity(
            "service-dashboard", self.org.id, self.staff.id, "service",
            frozenset({"workspace_read"}), workspace_id=self.other_ws.id,
        )
        with self.assertRaises(AuthorizationError):
            self.os.report_delivery.staff_list(cross_scope, self.org.id, self.ws.id)

    def test_http_portal_routes_are_authorized(self) -> None:
        report = self._report()
        approval = self._approval(report["id"])
        server = serve(self.os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address

        def request(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
            connection = HTTPConnection(host, port, timeout=5)
            headers = {"Authorization": f"Bearer {token}"}
            body = json.dumps(payload) if payload is not None else None
            if payload is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            connection.close()
            return response.status, result

        try:
            staff_token, _ = issue_identity(self.os, self.org.id, self.staff.id, self.ws.id)
            client_token, _ = issue_identity(self.os, self.org.id, self.client.id, self.ws.id)
            status, published = request(
                "POST", "/reports/portal-publish", staff_token,
                {"workspace_id": self.ws.id, "report_run_id": report["id"], "approval_request_id": approval["id"], "title": "Portal report"},
            )
            self.assertEqual(status, 201)

            status, listing = request(
                "GET", f"/client-portal/reports?workspace_id={self.ws.id}", client_token,
            )
            self.assertEqual(status, 200)
            self.assertEqual([item["id"] for item in listing["reports"]], [published["id"]])

            status, detail = request(
                "GET", f"/client-portal/reports/download?workspace_id={self.ws.id}&portal_report_version_id={published['id']}",
                client_token,
            )
            self.assertEqual(status, 200)
            self.assertEqual(detail["mode"], "download")
            self.assertNotIn("citations", repr(detail))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
