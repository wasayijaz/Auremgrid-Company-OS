from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from auremgrid.services.brain import CompanyOS
from auremgrid.services.worker import run_one_job


class DurableWorkerTests(unittest.TestCase):
    def test_worker_reauthorizes_principal_and_executes_report_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.sqlite"
            os = CompanyOS(path)
            org = os.create_organization("Auremgrid")
            ws = os.create_organization_workspace(org.id, "Client", "client")
            person = os.create_person(org.id, "Owner", "owner@worker.test", role="owner")
            os.add_person_to_workspace(org.id, ws.id, person.id, "admin")
            principal = os.auth.create_principal(org.id, person.id, "owner@worker.test")
            job = os.jobs.enqueue_job(
                org.id, ws.id, principal["id"], "report.generate", {"report_type": "client_weekly_report"}
            )
            os.close()

            worker_os = CompanyOS(path)
            result = run_one_job(worker_os, org.id, ws.id, "worker-1")
            self.assertEqual(result["id"], job["id"])
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["progress"], 1)
            worker_os.close()

    def test_worker_fails_unregistered_job_without_retrying_external_action(self) -> None:
        os = CompanyOS()
        org = os.create_organization("Auremgrid")
        person = os.create_person(org.id, "Owner", "owner@worker.test", role="owner")
        principal = os.auth.create_principal(org.id, person.id, "owner@worker.test")
        job = os.jobs.enqueue_job(org.id, None, principal["id"], "external.unknown", {})
        result = run_one_job(os, org.id, None, "worker-1")
        self.assertEqual(result["id"], job["id"])
        self.assertEqual(result["status"], "failed")
        os.close()


if __name__ == "__main__":
    unittest.main()
