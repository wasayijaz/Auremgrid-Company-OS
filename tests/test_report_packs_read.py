from __future__ import annotations

import sqlite3
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.report_packs_read import ReportPacksReadService


@dataclass(frozen=True)
class FakeWorkItem:
    id: str
    title: str
    status: str
    priority: str = "normal"
    assignee_person_id: str | None = None
    deadline: str | None = None
    blocking_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "assignee_person_id": self.assignee_person_id,
            "deadline": self.deadline,
            "blocking_reason": self.blocking_reason,
        }


class FakeStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.work_items = {
            "client-a": [
                FakeWorkItem("work-1", "Landing page", "in_progress", "high", "person-2", "2026-09-10"),
                FakeWorkItem("work-2", "Creative review", "review", "normal", None, "2026-08-25", "Waiting on assets"),
                FakeWorkItem("work-3", "Launch QA", "shipped", "normal", "person-3", "2026-09-01"),
            ],
            "client-b": [],
        }

    def list_work_items(self, workspace_id: str, open_only: bool = False) -> list[FakeWorkItem]:
        items = list(self.work_items.get(workspace_id, []))
        if open_only:
            return [item for item in items if item.status != "shipped"]
        return items


class FakeCompany:
    def __init__(self) -> None:
        self.scopes = {
            "agency": {"workspace_id": "agency", "organization_id": "org-1", "kind": "agency"},
            "client-a": {"workspace_id": "client-a", "organization_id": "org-1", "kind": "client"},
            "client-b": {"workspace_id": "client-b", "organization_id": "org-1", "kind": "client"},
            "other-client": {"workspace_id": "other-client", "organization_id": "org-2", "kind": "client"},
        }

    def workspace_scope(self, workspace_id: str) -> dict[str, str] | None:
        return self.scopes.get(workspace_id)

    def list_workspaces(self, organization_id: str) -> list[dict[str, str]]:
        return [
            {"id": "agency", "name": "Agency HQ", "kind": "agency"},
            {"id": "client-b", "name": "Beta Studio", "kind": "client"},
            {"id": "client-a", "name": "Acme Co", "kind": "client"},
        ] if organization_id == "org-1" else []


class FakeAgencyOps:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def finance_status(self, organization_id: str, requester_person_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        self.calls.append((organization_id, requester_person_id, workspace_id or ""))
        return {
            "status": "connected",
            "recognized_revenue": 12000.0,
            "outstanding_revenue": 1500.0,
            "latest_economics": {"margin": 0.62},
        }


class FakeClientOps:
    def list_risks(self, organization_id: str, workspace_id: str, person_id: str, open_only: bool = True) -> list[dict[str, Any]]:
        return [
            {
                "id": "risk-1",
                "organization_id": organization_id,
                "workspace_id": workspace_id,
                "type": "delivery",
                "severity": "high",
                "status": "open",
                "impact": "Launch timeline may slip.",
            }
        ]


class FakeCompanyOS:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.store = FakeStore(conn)
        self.company = FakeCompany()
        self.agency_ops = FakeAgencyOps()
        self.client_ops = FakeClientOps()
        self.authorized = {"agency", "client-a"}
        self.access_checks: list[tuple[str, str, str, bool]] = []

    def _require_person_access(self, organization_id: str, workspace_id: str, person_id: str, write: bool = False) -> None:
        self.access_checks.append((organization_id, workspace_id, person_id, write))
        if workspace_id not in self.authorized:
            raise AuthorizationError("not allowed")


class ReportPacksReadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE campaigns (
                id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, project_id TEXT,
                name TEXT, objective TEXT, platform TEXT, budget REAL, currency TEXT,
                start_date TEXT, end_date TEXT, status TEXT, owner_person_id TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE campaign_metric_snapshots (
                id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, campaign_id TEXT,
                captured_at TEXT, spend REAL, revenue REAL, leads REAL, impressions REAL,
                clicks REAL, cpl REAL, cac REAL, ctr REAL, cvr REAL, roas REAL, source TEXT
            );
            """
        )
        self.conn.execute(
            "INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("campaign-1", "org-1", "client-a", None, "Launch", "Sales", "Meta", 1000.0, "USD", None, None, "active", "person-1", "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
        )
        self.conn.execute(
            "INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("campaign-2", "org-1", "client-a", None, "Retargeting", "Sales", "Google", 500.0, "USD", None, None, "draft", "person-1", "2026-08-02T00:00:00+00:00", "2026-08-02T00:00:00+00:00"),
        )
        self.conn.execute(
            "INSERT INTO campaign_metric_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("metric-1", "org-1", "client-a", "campaign-1", "2026-09-05T12:00:00+00:00", 100.0, 350.0, 10.0, 1000.0, 80.0, 10.0, None, 8.0, 12.5, 3.5, "ads"),
        )
        self.conn.execute(
            "INSERT INTO campaign_metric_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("metric-old", "org-1", "client-a", "campaign-1", "2026-08-05T12:00:00+00:00", 999.0, 999.0, 99.0, 999.0, 99.0, 10.0, None, 9.9, 99.0, 1.0, "ads"),
        )
        self.conn.commit()
        self.os = FakeCompanyOS(self.conn)
        self.service = ReportPacksReadService(self.os)
        self.scope = {"organization_id": "org-1", "workspace_id": "agency", "person_id": "person-1"}

    def tearDown(self) -> None:
        self.conn.close()

    def test_list_available_packs_returns_only_authorized_client_workspaces(self) -> None:
        packs = self.service.list_available_packs(self.scope)
        self.assertEqual([pack["client_id"] for pack in packs], ["client-a"])
        self.assertEqual(packs[0]["sections"], ["performance", "finance", "delivery", "risks"])
        self.assertTrue(all(check[3] is False for check in self.os.access_checks))

    def test_build_report_pack_aggregates_existing_read_surfaces(self) -> None:
        pack = self.service.build_report_pack(self.scope, "client-a", "2026-09")
        self.assertEqual(pack["client"], {"id": "client-a", "name": "Acme Co", "kind": "client"})
        self.assertEqual(pack["performance"]["campaign_count"], 2)
        self.assertEqual(pack["performance"]["active_campaign_count"], 1)
        self.assertEqual(pack["performance"]["campaigns_with_metrics"], 1)
        self.assertEqual(pack["performance"]["totals"]["spend"], 100.0)
        self.assertEqual(pack["performance"]["totals"]["revenue"], 350.0)
        self.assertEqual(pack["performance"]["totals"]["roas"], 3.5)
        self.assertEqual(pack["finance"]["recognized_revenue"], 12000.0)
        self.assertEqual(self.os.agency_ops.calls, [("org-1", "person-1", "client-a")])
        self.assertEqual(pack["delivery"]["work_item_count"], 3)
        self.assertEqual(pack["delivery"]["open_work_count"], 2)
        self.assertEqual(pack["delivery"]["blocked_work_count"], 1)
        self.assertEqual(pack["delivery"]["overdue_work_count"], 1)
        self.assertEqual(pack["risks"][0]["id"], "risk-1")

    def test_validation_and_scope_fail_closed(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.build_report_pack(self.scope, "agency", "2026-09")
        with self.assertRaises(AuthorizationError):
            self.service.build_report_pack(self.scope, "other-client", "2026-09")
        with self.assertRaises(ValidationError):
            self.service.build_report_pack({"organization_id": "org-1", "workspace_id": "", "person_id": "person-1"}, "client-a", "2026-09")
        with self.assertRaises(ValidationError):
            self.service.build_report_pack(self.scope, "client-a", {"start": "2026-10-01", "end": "2026-09-01"})

    def test_service_exposes_no_mutation_methods(self) -> None:
        public_methods = {name for name in dir(self.service) if not name.startswith("_") and callable(getattr(self.service, name))}
        self.assertEqual(public_methods, {"build_report_pack", "list_available_packs"})


if __name__ == "__main__":
    unittest.main()
