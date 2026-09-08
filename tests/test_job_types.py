from __future__ import annotations

import unittest

from auremgrid.services.job_types import (
    PUBLIC_JOB_TYPES,
    REGISTERED_JOB_TYPES,
    has_registered_job_handler,
)


class DurableJobTypeRegistryTests(unittest.TestCase):
    def test_public_job_types_have_registered_handlers(self) -> None:
        missing = sorted(
            job_type
            for job_type in PUBLIC_JOB_TYPES
            if not has_registered_job_handler(job_type)
        )
        self.assertEqual(missing, [])

    def test_public_job_types_exclude_internal_and_retired_jobs(self) -> None:
        self.assertNotIn("connector.sync", PUBLIC_JOB_TYPES)
        self.assertNotIn("outbox.dispatch", PUBLIC_JOB_TYPES)
        self.assertNotIn("backup.create", PUBLIC_JOB_TYPES)

    def test_registry_includes_internal_worker_jobs(self) -> None:
        self.assertIn("connector.sync", REGISTERED_JOB_TYPES)


if __name__ == "__main__":
    unittest.main()
