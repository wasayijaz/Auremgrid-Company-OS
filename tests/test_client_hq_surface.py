from __future__ import annotations

import unittest
from pathlib import Path

from tests.dashboard_bundle import read_dashboard_bundle


class ClientHQSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = read_dashboard_bundle(Path(__file__).parents[1])

    def test_overview_renders_accountability_and_operational_contract(self) -> None:
        for marker in (
            "renderClientOverview(data)",
            "data.account_team",
            "data.current_roster",
            "data.meeting_responsibilities",
            "data.workload_by_person",
            "data.workflow_board",
            "data.readiness",
            "Client success DRI",
            "Meeting responsibilities",
            "Workflow board",
        ):
            self.assertIn(marker, self.html)

    def test_client_hq_has_explicit_loading_empty_and_error_states(self) -> None:
        for marker in (
            "Loading client accountability and readiness",
            "No current roster. Roles remain explicitly unassigned.",
            "No meetings or responsibility assignments yet.",
            "No workflow runs for this client.",
            "Client HQ unavailable:",
            ">Retry</button>",
        ):
            self.assertIn(marker, self.html)

    def test_client_actions_come_only_from_server_allowed_actions(self) -> None:
        overview = self.html[
            self.html.index("function renderClientOverview") : self.html.index("async function openClient")
        ]
        self.assertIn("renderActionDescriptors(run)", overview)
        self.assertIn("renderActionDescriptors(stage)", overview)
        self.assertNotIn(".title===", overview)
        self.assertNotIn(".role===", overview)
        self.assertIn("Array.isArray(row.allowed_actions)", self.html)

    def test_people_view_uses_the_derived_current_week_capacity_contract(self) -> None:
        self.assertIn("week_start:week", self.html)
        self.assertIn("capacity.people", self.html)
        self.assertNotIn("capacity.capacity||[]", self.html)
        self.assertNotIn("No capacity snapshots yet", self.html)


if __name__ == "__main__":
    unittest.main()
