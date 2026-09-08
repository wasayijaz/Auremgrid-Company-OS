from __future__ import annotations

import unittest

from auremgrid.services.job_types import (
    PUBLIC_JOB_TYPES,
    REGISTERED_JOB_TYPES,
    has_registered_job_handler,
)
from auremgrid.services.worker import run_one_job
from auremgrid.services.agent_execution import (
    AGENT_THINK_JOB_TYPE,
    ModelEntry,
    ModelRegistry,
    ProviderRegistry,
)
from auremgrid.services.brain import CompanyOS


class ThinkProvider:
    name = "test-provider"
    model = "test-model"
    version = "1"

    def deliberate(self, context):
        return {
            "plan": {"summary": context["question"]},
            "usage": {"input_tokens": 3, "output_tokens": 5},
        }


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

    def test_agent_think_is_advertised_and_registered(self) -> None:
        self.assertIn(AGENT_THINK_JOB_TYPE, PUBLIC_JOB_TYPES)
        self.assertIn(AGENT_THINK_JOB_TYPE, REGISTERED_JOB_TYPES)

    def test_worker_dispatches_agent_think_job(self) -> None:
        os = CompanyOS()
        providers = ProviderRegistry()
        providers.register("test-provider", ThinkProvider())
        models = ModelRegistry()
        models.register(ModelEntry("test-model", "test-provider", 1, 0.01, 100))
        models.set_fallback_chain(1, ["test-model"])
        os.agent_think_providers = providers
        os.agent_think_models = models
        try:
            org = os.create_organization("Auremgrid")
            ws = os.create_organization_workspace(org.id, "Client", "client")
            person = os.create_person(org.id, "Owner", "owner@think.test", role="owner")
            os.add_person_to_workspace(org.id, ws.id, person.id, "admin")
            principal = os.auth.create_principal(org.id, person.id, "owner@think.test")
            job = os.jobs.enqueue_job(
                org.id,
                ws.id,
                principal["id"],
                AGENT_THINK_JOB_TYPE,
                {
                    "agent_id": "agent-1",
                    "run_id": "run-1",
                    "person_id": person.id,
                    "context": {"organization_id": org.id, "workspace_id": ws.id, "person_id": person.id, "question": "status"},
                },
            )

            result = run_one_job(os, org.id, ws.id, "worker-1")

            self.assertEqual(result["id"], job["id"])
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["result"]["status"], "succeeded")
            self.assertEqual(result["result"]["thinking"]["input_tokens"], 3)
        finally:
            os.close()


if __name__ == "__main__":
    unittest.main()
