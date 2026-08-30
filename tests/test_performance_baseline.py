from __future__ import annotations

import unittest

from scripts.performance_baseline import run_rehearsal


REQUIRED_MEASUREMENTS = {
    "dashboard_command",
    "brain_search",
    "intelligence_workspace",
    "intelligence_portfolio",
    "proactive_refresh_snapshot",
    "workflow_query",
    "large_work_list",
    "projection_rebuild",
    "backup_verify",
    "migration_open",
    "worker_queue_throughput",
}


class PerformanceBaselineTests(unittest.TestCase):
    def test_reports_required_single_host_surfaces(self) -> None:
        result = run_rehearsal(2, repeats=1)
        measurements = result["measurements"]

        self.assertEqual(result["clients"], 2)
        self.assertTrue(REQUIRED_MEASUREMENTS <= set(measurements))
        for name in REQUIRED_MEASUREMENTS - {"backup_verify", "worker_queue_throughput"}:
            measurement = measurements[name]
            self.assertEqual(measurement["status"], "measured", name)
            self.assertEqual(measurement["samples"], 1)
            self.assertGreaterEqual(measurement["median_ms"], 0)

        self.assertEqual(measurements["backup_verify"]["status"], "measured")
        self.assertGreaterEqual(measurements["backup_verify"]["schema_version"], 1)
        self.assertGreater(measurements["backup_verify"]["size_bytes"], 0)

        self.assertEqual(measurements["migration_open"]["status"], "measured")
        self.assertEqual(
            measurements["migration_open"]["schema_version"],
            measurements["backup_verify"]["schema_version"],
        )

        throughput = measurements["worker_queue_throughput"]
        self.assertEqual(throughput["status"], "measured")
        self.assertEqual(throughput["jobs"], 2)
        self.assertEqual(throughput["succeeded"], 2)
        self.assertGreater(throughput["jobs_per_second"], 0)


if __name__ == "__main__":
    unittest.main()
