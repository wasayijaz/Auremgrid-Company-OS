from __future__ import annotations

import unittest
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auremgrid.domain.errors import NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS, new_id
from auremgrid.services.job_ops import JobOperations
from tests.auth_support import LATEST_SCHEMA_VERSION


class JobOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.other_org = self.os.create_organization("Other")
        self.ws = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.other_ws = self.os.create_organization_workspace(self.other_org.id, "Other Prime", "client")
        self.person = self.os.create_person(self.org.id, "Owner", "owner@jobs.test", role="owner")
        self.other_person = self.os.create_person(self.other_org.id, "Other Owner", "other@jobs.test", role="owner")
        self.principal = self.os.auth.create_principal(self.org.id, self.person.id, "owner@jobs.test")
        self.other_principal = self.os.auth.create_principal(self.other_org.id, self.other_person.id, "other@jobs.test")
        self.ops = JobOperations(self.os.store.conn, new_id)

    def tearDown(self) -> None:
        self.os.close()

    def test_schema_v11_auth_control_tables_exist_without_secret_values(self) -> None:
        self.assertEqual(self.os.store.schema_version, LATEST_SCHEMA_VERSION)
        secret_columns = [
            row["name"]
            for row in self.os.store.conn.execute("PRAGMA table_info(secret_bindings)").fetchall()
        ]
        self.assertEqual(
            [
                row["name"]
                for row in self.os.store.conn.execute("PRAGMA table_info(auth_principals)").fetchall()
            ],
            ["id", "organization_id", "person_id", "email", "status", "created_at", "updated_at"],
        )
        self.assertIn("system_state", self._tables())
        self.assertIn("principal_actor_bindings", self._tables())
        self.assertNotIn("value", secret_columns)
        self.assertNotIn("secret", secret_columns)

    def test_schema_10_workflow_runs_migrate_to_v11_without_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema10.sqlite"
            original = CompanyOS(path)
            org = original.create_organization("Migration Agency")
            ws = original.create_organization_workspace(org.id, "Client", "client")
            person = original.create_person(org.id, "Owner", role="owner")
            backup = original.create_person(org.id, "Backup", role="member")
            original.add_person_to_workspace(org.id, ws.id, person.id, "admin")
            original.add_person_to_workspace(org.id, ws.id, backup.id, "operator")
            original.client_ops.create_client_roster(
                org.id,
                ws.id,
                person.id,
                [
                    {"role_key": "client_success_dri", "person_id": person.id},
                    {"role_key": "client_success_backup", "person_id": backup.id},
                    {"role_key": "account_executive", "person_id": person.id},
                    {"role_key": "wing_lead", "wing": "Client Strategy/Marketing", "person_id": person.id},
                    {"role_key": "wing_executive", "wing": "Client Strategy/Marketing", "person_id": person.id},
                    {"role_key": "wing_executive", "wing": "Operations", "person_id": person.id},
                    {"role_key": "wing_lead", "wing": "Product & Engineering", "person_id": person.id},
                ],
            )
            run = original.workflow_ops.create_run(
                org.id, ws.id, person.id, original.workflow_catalog.get("client_request")
            )
            original.close()
            connection = sqlite3.connect(path)
            for table in (
                "outbox_events", "job_events", "jobs", "secret_bindings", "system_state",
                "principal_actor_bindings", "api_tokens", "auth_sessions", "auth_principals",
            ):
                connection.execute(f"DROP TABLE {table}")
            for column in (
                "expected_account_id", "provider_account_id", "provider_account_name",
                "granted_permissions", "credential_verified_at",
            ):
                connection.execute(f"ALTER TABLE integrations DROP COLUMN {column}")
            connection.execute("DROP INDEX IF EXISTS idx_provider_mutation_exact_event")
            for trigger in (
                "provider_mutation_event_key_required", "provider_mutation_identity_no_update",
                "provider_mutation_source_bind_once", "provider_mutation_apply_once",
            ):
                connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            connection.execute("ALTER TABLE provider_route_mutation_staging DROP COLUMN event_dedupe_key")
            connection.execute("ALTER TABLE provider_sync_generations DROP COLUMN cancelled_at")
            connection.execute("DELETE FROM schema_migrations WHERE version>=11")
            connection.commit()
            connection.close()

            upgraded = CompanyOS(path)
            self.assertEqual(upgraded.store.schema_version, LATEST_SCHEMA_VERSION)
            restored = upgraded.workflow_ops.summary(org.id, ws.id, person.id, run["id"])
            self.assertEqual(restored["run"]["definition_key"], "client_request")
            upgraded.close()

    def test_enqueue_is_idempotent_and_conflicts_on_payload_mismatch(self) -> None:
        first = self.ops.enqueue_job(self.org.id, self.ws.id, self.principal["id"], "sync", {"page": 1}, idempotency_key="sync-1")
        second = self.ops.enqueue_job(self.org.id, self.ws.id, self.principal["id"], "sync", {"page": 1}, idempotency_key="sync-1")
        self.assertEqual(first["id"], second["id"])
        with self.assertRaises(ValidationError):
            self.ops.enqueue_job(self.org.id, self.ws.id, self.principal["id"], "sync", {"page": 2}, idempotency_key="sync-1")

    def test_jobs_and_outbox_reject_credential_material_and_redact_errors(self) -> None:
        with self.assertRaises(ValidationError):
            self.ops.enqueue_job(
                self.org.id, self.ws.id, self.principal["id"], "sync", {"api_key": "sentinel"}
            )
        with self.assertRaises(ValidationError):
            self.ops.add_outbox_event(
                self.org.id, self.ws.id, "integration", "i1", "send", {"Authorization": "Bearer sentinel"}
            )
        job = self.ops.enqueue_job(self.org.id, self.ws.id, self.principal["id"], "sync", {"page": 1})
        claimed = self.ops.claim_job(self.org.id, self.ws.id, "worker")
        failed = self.ops.fail_job(
            self.org.id, self.ws.id, job["id"], "worker", claimed["lease_token"],
            {"Authorization": "Bearer sentinel"}, retry=False,
        )
        self.assertEqual(failed["error"]["Authorization"], "[REDACTED]")

    def test_two_workers_cannot_claim_same_unexpired_lease(self) -> None:
        self.ops.enqueue_job(self.org.id, self.ws.id, self.principal["id"], "sync", {"page": 1})
        first = self.ops.claim_job(self.org.id, self.ws.id, "worker-a")
        second = self.ops.claim_job(self.org.id, self.ws.id, "worker-b")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIsNotNone(first["lease_token"])

    def test_expired_lease_recovery_fences_stale_worker(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.ops.enqueue_job(self.org.id, self.ws.id, self.principal["id"], "sync", {"page": 1})
        first = self.ops.claim_job(self.org.id, self.ws.id, "worker-a", lease_seconds=10, now=now)
        recovered = self.ops.claim_job(
            self.org.id, self.ws.id, "worker-b", lease_seconds=10, now=now + timedelta(seconds=11)
        )
        self.assertEqual(first["id"], recovered["id"])
        self.assertEqual(recovered["attempts"], 2)
        with self.assertRaises(ValidationError):
            self.ops.succeed_job(
                self.org.id,
                self.ws.id,
                first["id"],
                "worker-a",
                first["lease_token"],
                {"ok": True},
                now=now + timedelta(seconds=12),
            )

    def test_retry_wait_then_dead_letter_uses_bounded_backoff(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        job = self.ops.enqueue_job(self.org.id, self.ws.id, self.principal["id"], "sync", {"page": 1}, max_attempts=2)
        first = self.ops.claim_job(self.org.id, self.ws.id, "worker", now=now)
        retry = self.ops.fail_job(
            self.org.id, self.ws.id, job["id"], "worker", first["lease_token"], {"message": "rate limited"}, now=now
        )
        self.assertEqual(retry["status"], "retry_wait")
        self.assertEqual(retry["available_at"], (now + timedelta(seconds=60)).isoformat())
        self.assertIsNone(self.ops.claim_job(self.org.id, self.ws.id, "worker", now=now + timedelta(seconds=59)))
        second = self.ops.claim_job(self.org.id, self.ws.id, "worker", now=now + timedelta(seconds=61))
        dead = self.ops.fail_job(
            self.org.id,
            self.ws.id,
            job["id"],
            "worker",
            second["lease_token"],
            {"message": "still limited"},
            now=now + timedelta(seconds=61),
        )
        self.assertEqual(dead["status"], "dead_letter")

    def test_heartbeat_progress_and_success(self) -> None:
        job = self.ops.enqueue_job(self.org.id, self.ws.id, self.principal["id"], "render", {"id": 1})
        claimed = self.ops.claim_job(self.org.id, self.ws.id, "worker")
        running = self.ops.heartbeat_job(
            self.org.id, self.ws.id, job["id"], "worker", claimed["lease_token"], progress=0.4
        )
        done = self.ops.succeed_job(
            self.org.id, self.ws.id, job["id"], "worker", running["lease_token"], {"url": "file://result"}
        )
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["progress"], 0.4)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["progress"], 1)

    def test_cancel_only_queued_or_retry_wait_jobs(self) -> None:
        queued = self.ops.enqueue_job(self.org.id, self.ws.id, self.principal["id"], "sync", {"page": 1})
        cancelled = self.ops.cancel_job(self.org.id, self.ws.id, queued["id"], "not needed")
        self.assertEqual(cancelled["status"], "cancelled")
        running = self.ops.enqueue_job(self.org.id, self.ws.id, self.principal["id"], "sync", {"page": 2})
        self.ops.claim_job(self.org.id, self.ws.id, "worker")
        with self.assertRaises(ValidationError):
            self.ops.cancel_job(self.org.id, self.ws.id, running["id"], "too late")

    def test_tenant_scoping_hides_foreign_jobs(self) -> None:
        job = self.ops.enqueue_job(self.org.id, self.ws.id, self.principal["id"], "sync", {"page": 1})
        self.ops.enqueue_job(self.other_org.id, self.other_ws.id, self.other_principal["id"], "sync", {"page": 2})
        with self.assertRaises(NotFoundError):
            self.ops.get_job(self.other_org.id, self.other_ws.id, job["id"])
        self.assertEqual(len(self.ops.list_jobs(self.org.id, self.ws.id)), 1)

    def test_job_events_are_append_only(self) -> None:
        job = self.ops.enqueue_job(self.org.id, self.ws.id, self.principal["id"], "sync", {"page": 1})
        claimed = self.ops.claim_job(self.org.id, self.ws.id, "worker")
        self.ops.heartbeat_job(self.org.id, self.ws.id, job["id"], "worker", claimed["lease_token"], progress=0.2)
        events = self.ops.job_events(self.org.id, self.ws.id, job["id"])
        self.assertEqual([event["event_type"] for event in events], ["enqueue", "claim", "heartbeat"])
        with self.assertRaises(Exception):
            self.os.store.conn.execute("UPDATE job_events SET event_type='tampered'")

    def test_outbox_idempotency_retry_and_fencing(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        first = self.ops.add_outbox_event(
            self.org.id, self.ws.id, "job", "job_1", "job.completed", {"ok": True}, idempotency_key="event-1"
        )
        second = self.ops.add_outbox_event(
            self.org.id, self.ws.id, "job", "job_1", "job.completed", {"ok": True}, idempotency_key="event-1"
        )
        self.assertEqual(first["id"], second["id"])
        with self.assertRaises(ValidationError):
            self.ops.add_outbox_event(
                self.org.id, self.ws.id, "job", "job_1", "job.completed", {"ok": False}, idempotency_key="event-1"
            )
        claimed = self.ops.claim_outbox_events(self.org.id, self.ws.id, "publisher-a", now=now)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(self.ops.claim_outbox_events(self.org.id, self.ws.id, "publisher-b", now=now), [])
        failed = self.ops.fail_outbox_event(
            self.org.id, self.ws.id, first["id"], "publisher-a", claimed[0]["lease_token"], "network down", now=now
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.ops.claim_outbox_events(self.org.id, self.ws.id, "publisher-b", now=now + timedelta(seconds=59)), [])
        reclaimed = self.ops.claim_outbox_events(self.org.id, self.ws.id, "publisher-b", now=now + timedelta(seconds=61))
        with self.assertRaises(ValidationError):
            self.ops.publish_outbox_event(
                self.org.id, self.ws.id, first["id"], "publisher-a", claimed[0]["lease_token"], now=now + timedelta(seconds=62)
            )
        published = self.ops.publish_outbox_event(
            self.org.id, self.ws.id, first["id"], "publisher-b", reclaimed[0]["lease_token"], now=now + timedelta(seconds=62)
        )
        self.assertEqual(published["status"], "published")

    def test_recovery_mode_fences_outbound_dispatch(self) -> None:
        self.ops.add_outbox_event(
            self.org.id, self.ws.id, "report", "r1", "report.ready", {"report_id": "r1"}
        )
        self.os.store.conn.execute(
            "INSERT INTO system_state(key,value,updated_at) VALUES ('recovery_mode','1','2026-08-19T00:00:00+00:00')"
        )
        self.os.store.conn.commit()
        self.assertEqual(self.ops.claim_outbox_events(self.org.id, self.ws.id, "publisher"), [])

    def _tables(self) -> set[str]:
        return {
            row["name"]
            for row in self.os.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }


if __name__ == "__main__":
    unittest.main()
