from __future__ import annotations

import unittest
from unittest.mock import patch

from auremgrid.services.brain import CompanyOS


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Scheduler")
        self.ws_one = self.os.create_organization_workspace(self.org.id, "One", "client")
        self.ws_two = self.os.create_organization_workspace(self.org.id, "Two", "client")
        self.scheduler = self.os.scheduler(self.org.id, None, "worker-a", poll_seconds=0.01)

    def tearDown(self) -> None:
        self.os.close()

    def test_pause_heartbeat_and_idle_recovery(self) -> None:
        self.assertEqual(self.scheduler.run_once()["status"], "idle")
        self.assertEqual(self.scheduler.health()["status"], "idle")
        self.scheduler.set_paused(True)
        self.assertEqual(self.scheduler.run_once()["status"], "paused")
        self.assertTrue(self.scheduler.health()["paused"])
        self.scheduler.set_paused(False)
        self.assertEqual(self.scheduler.run_once()["status"], "idle")

    def test_pause_and_heartbeat_are_workspace_scoped(self) -> None:
        one = self.os.scheduler(self.org.id, self.ws_one.id, "worker-a", poll_seconds=0.01)
        two = self.os.scheduler(self.org.id, self.ws_two.id, "worker-a", poll_seconds=0.01)
        one.set_paused(True)
        self.assertTrue(one.health()["paused"])
        self.assertFalse(two.health()["paused"])
        self.assertEqual(one.run_once()["status"], "paused")
        self.assertEqual(two.run_once()["status"], "idle")
        self.assertEqual(one.health()["status"], "paused")
        self.assertEqual(two.health()["status"], "idle")

    def test_run_once_persists_degraded_heartbeat_and_reraises(self) -> None:
        scheduler = self.os.scheduler(self.org.id, self.ws_one.id, "worker-fail", poll_seconds=0.01)
        with patch("auremgrid.services.worker.run_one_job", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                scheduler.run_once()
        health = scheduler.health()
        self.assertEqual(health["status"], "degraded")
        self.assertTrue(health["degraded"])
        self.assertEqual(health["heartbeat"]["last_error"], "boom")


if __name__ == "__main__":
    unittest.main()
