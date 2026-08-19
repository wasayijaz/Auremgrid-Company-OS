from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from auremgrid.connectors.google_auth import (
    ConnectorInboxRepository,
    ConnectorSourceEvent,
    RouteLifecycleMutation,
)
from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS, new_id
from auremgrid.storage.sqlite import ProviderSyncFence


class ProviderSyncAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Atomic Sync")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client")
        self.actor = self.os.create_actor(self.ws.id, "Operator", "admin")
        person = self.os.create_person(self.org.id, "Owner", "owner@example.test", role="admin")
        self.principal = self.os.auth.create_principal(self.org.id, person.id, "owner@example.test")
        self.repo = ConnectorInboxRepository(self.os.store.conn, new_id)
        self.account_key = "integration:mapping"
        job = self.os.jobs.enqueue_job(
            self.org.id, self.ws.id, self.principal["id"], "connector.sync", {"stream": "google"}
        )
        lock = self.repo.reserve_stream(
            self.org.id, self.ws.id, "google_drive", self.account_key, "managed:google",
            job["id"], "mapping-hash", "worker", lease_seconds=600,
        )
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.os.store.conn.execute(
            """INSERT INTO secret_bindings(
                   id,organization_id,workspace_id,integration_id,name,provider,reference,scopes,
                   fingerprint,status,last_verified_at,created_at,updated_at,revoked_at,generation
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("binding-1", self.org.id, self.ws.id, None, "Google", "google_drive", "env:GOOGLE",
             "[]", "fingerprint", "active", now, now, now, None, 1),
        )
        self.os.store.conn.commit()
        self.fence = ProviderSyncFence(lock["id"], lock["reservation_token"], "binding-1", 1)

    def tearDown(self) -> None:
        self.os.close()

    def test_provider_operation_key_allows_later_reconciliation_wave(self) -> None:
        first = self.os.store.enqueue_provider_sync_task(
            self.ws.id, "google_drive", self.account_key, "managed:google",
            "reconcile", external_id="folder-1", route_key="folder:root",
            operation_key="wave-1",
        )
        second = self.os.store.enqueue_provider_sync_task(
            self.ws.id, "google_drive", self.account_key, "managed:google",
            "reconcile", external_id="folder-1", route_key="folder:root",
            operation_key="wave-2",
        )
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(
            self.os.store.conn.execute(
                "SELECT COUNT(*) FROM provider_sync_tasks WHERE operation_key IN ('wave-1','wave-2')"
            ).fetchone()[0],
            2,
        )

    def test_provider_task_worker_reclaims_expired_lease_and_old_worker_cannot_complete(self) -> None:
        task = self.os.store.enqueue_provider_sync_task(
            self.ws.id, "google_drive", self.account_key, "managed:google",
            "descendants", external_id="folder-1", route_key="folder:root",
            operation_key="lease-wave",
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        worker_a = self.os.store.claim_provider_sync_task(
            self.ws.id, "google_drive", self.account_key, "managed:google",
            "worker-a", lease_seconds=30, now=now, fence=self.fence,
        )
        self.assertEqual(worker_a["lease_owner"], "worker-a")
        worker_b = self.os.store.claim_provider_sync_task(
            self.ws.id, "google_drive", self.account_key, "managed:google",
            "worker-b", lease_seconds=30, now=now + timedelta(seconds=31), fence=self.fence,
        )
        self.assertEqual(worker_b["lease_owner"], "worker-b")
        self.assertFalse(self.os.store.complete_provider_sync_task(
            task["id"], worker_a["lease_token"], fence=self.fence,
        ))
        self.assertTrue(self.os.store.complete_provider_sync_task(
            task["id"], worker_b["lease_token"], fence=self.fence,
        ))

    def test_mapping_quarantine_is_redacted_and_durable(self) -> None:
        self.os.store.conn.execute(
            """INSERT INTO integrations(
                id,organization_id,source,status,workspace_mappings,permissions,sync_cursor,
                last_sync_at,last_error,object_count,health,created_at,expected_account_id,
                provider_account_id,provider_account_name,granted_permissions,credential_verified_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("integration-1", self.org.id, "google_drive", "authorized", "{}", "[]", None,
             None, None, 0, "never_synced", datetime.now(timezone.utc).isoformat(),
             "account", None, None, "[]", None),
        )
        self.os.store.conn.commit()
        item = self.os.store.quarantine_provider_sync(
            self.org.id, "integration-1", "google_drive", "mapping_overlap", "digest-1"
        )
        self.assertEqual(item["status"], "open")
        columns = {
            row["name"] for row in self.os.store.conn.execute(
                "PRAGMA table_info(provider_sync_quarantines)"
            ).fetchall()
        }
        self.assertNotIn("workspace_id", columns)
        self.assertEqual(len(self.os.store.open_provider_sync_quarantines(self.org.id, "integration-1")), 1)

    def _event_and_mutation(self, suffix: str = "1") -> tuple[ConnectorSourceEvent, RouteLifecycleMutation]:
        dedupe = f"drive:f{suffix}:transition"
        event = ConnectorSourceEvent(
            dedupe, f"f{suffix}", "file_changed", f"google-drive/files/f{suffix}",
            f"https://drive.test/f{suffix}", f"Evidence {suffix}", {},
            "2026-08-19T12:00:00+00:00",
        )
        mutation = RouteLifecycleMutation(
            f"f{suffix}", "folder:root", self.ws.id, "upsert", f"v{suffix}", dedupe
        )
        return event, mutation

    def _record(self, suffix: str = "1", mutations: bool = True):
        event, mutation = self._event_and_mutation(suffix)
        return self.repo.record_pull(
            self.org.id, self.ws.id, "google_drive", self.account_key,
            None, json.dumps({"cursor": suffix}), [event],
            stream_lock_id=self.fence.stream_lock_id,
            reservation_token=self.fence.reservation_token,
            credential_binding_id=self.fence.credential_binding_id,
            credential_generation=self.fence.credential_generation,
            lifecycle_mutations=(mutation,) if mutations else (),
        )

    def test_record_pull_and_staging_rollback_with_caller_transaction(self) -> None:
        event, mutation = self._event_and_mutation()
        with self.assertRaises(RuntimeError):
            with self.os.store.atomic(immediate=True):
                self.repo.record_pull(
                    self.org.id, self.ws.id, "google_drive", self.account_key,
                    None, "cursor", [event], lifecycle_mutations=(mutation,),
                    stream_lock_id=self.fence.stream_lock_id,
                    reservation_token=self.fence.reservation_token,
                    credential_binding_id=self.fence.credential_binding_id,
                    credential_generation=self.fence.credential_generation,
                    manage_transaction=False,
                )
                raise RuntimeError("crash after stage")
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM connector_ingest_batches").fetchone()[0], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM provider_route_mutation_staging").fetchone()[0], 0)

    def test_exact_event_bind_apply_and_cursor_promotion_are_composable(self) -> None:
        batch = self._record()
        event = batch["events"][0]
        source = self.os.ingest_text(
            self.ws.id, self.actor.id, event["source_key"], event["content"], event["locator"]
        ).source
        self.repo.mark_event_ingested(event["id"])
        with self.os.store.atomic(immediate=True):
            self.assertEqual(
                self.os.store.bind_provider_event_source(
                    batch["id"], event["dedupe_key"], self.ws.id, source.id, self.fence
                ),
                1,
            )
            applied = self.os.store.apply_provider_event_mutations(
                batch["id"], event["dedupe_key"], self.fence
            )
            self.assertEqual(len(applied), 1)
            self.repo.complete_batch(
                batch["id"], self.fence.stream_lock_id, self.fence.reservation_token,
                self.fence.credential_binding_id, self.fence.credential_generation,
                manage_transaction=False,
            )
        self.assertEqual(self.repo.get_batch(batch["id"])["status"], "completed")
        mutation = self.os.store.conn.execute(
            "SELECT * FROM provider_route_mutation_staging WHERE batch_id=?", (batch["id"],)
        ).fetchone()
        self.assertEqual(mutation["source_id"], source.id)
        self.assertEqual(mutation["status"], "applied")
        with self.assertRaises(sqlite3.IntegrityError):
            self.os.store.conn.execute(
                "UPDATE provider_route_mutation_staging SET source_id='different' WHERE id=?",
                (mutation["id"],),
            )

    def test_quarantined_lifecycle_event_blocks_cursor_promotion(self) -> None:
        batch = self._record("q")
        event = self.repo.claim_event(
            self.org.id, self.ws.id, "google_drive", self.account_key, "worker"
        )
        self.repo.quarantine_event(event["id"], "worker", event["lease_token"], "bad payload")
        with self.assertRaises(ValidationError):
            self.repo.complete_batch(
                batch["id"], self.fence.stream_lock_id, self.fence.reservation_token,
                self.fence.credential_binding_id, self.fence.credential_generation,
            )
        self.assertIsNone(self.repo.get_cursor(
            self.org.id, self.ws.id, "google_drive", self.account_key
        ))

    def test_stale_fence_rolls_back_task_mutation(self) -> None:
        stale = ProviderSyncFence(
            self.fence.stream_lock_id, "wrong-token",
            self.fence.credential_binding_id, self.fence.credential_generation,
        )
        with self.assertRaises(ValidationError):
            self.os.store.enqueue_provider_sync_task(
                self.ws.id, "google_drive", self.account_key, "managed:google", "backfill",
                route_key="folder:root", fence=stale,
            )
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM provider_sync_tasks").fetchone()[0], 0)

    def test_task_heartbeat_lease_and_credential_fences(self) -> None:
        task = self.os.store.enqueue_provider_sync_task(
            self.ws.id, "google_drive", self.account_key, "managed:google", "reconcile",
            external_id="f1", route_key="folder:root", fence=self.fence,
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        claimed = self.os.store.claim_provider_sync_task(
            self.ws.id, "google_drive", self.account_key, "managed:google", "worker",
            lease_seconds=30, now=now, fence=self.fence,
        )
        self.assertEqual(claimed["id"], task["id"])
        with self.assertRaises(ValidationError):
            self.os.store.heartbeat_provider_sync_task(
                task["id"], "wrong", now=now + timedelta(seconds=1), fence=self.fence
            )
        heartbeat = self.os.store.heartbeat_provider_sync_task(
            task["id"], claimed["lease_token"], now=now + timedelta(seconds=1), fence=self.fence
        )
        self.assertGreater(heartbeat["lease_expires_at"], claimed["lease_expires_at"])
        self.os.store.conn.execute("UPDATE secret_bindings SET generation=2 WHERE id='binding-1'")
        self.os.store.conn.commit()
        with self.assertRaises(ValidationError):
            self.os.store.complete_provider_sync_task(task["id"], claimed["lease_token"], self.fence)

    def test_cancelled_generation_cancels_tasks_and_cannot_retire_unseen(self) -> None:
        generation = self.os.store.start_provider_sync_generation(
            self.ws.id, "google_drive", self.account_key, "managed:google", "folder:root",
            "baseline", fence=self.fence,
        )
        self.os.store.enqueue_provider_sync_task(
            self.ws.id, "google_drive", self.account_key, "managed:google", "backfill",
            generation_id=generation["id"], route_key="folder:root", fence=self.fence,
        )
        cancelled = self.os.store.cancel_provider_rebootstrap(
            self.ws.id, "google_drive", self.account_key, "managed:google", "folder:root", self.fence
        )
        self.assertEqual(cancelled["cancelled_tasks"], 1)
        self.assertEqual(self.os.store.pending_provider_task_count(
            self.ws.id, "google_drive", self.account_key, "managed:google", generation["id"]
        ), 0)
        with self.assertRaises(ValidationError):
            self.os.store.complete_provider_sync_generation(generation["id"], fence=self.fence)

    def test_cursor_promotion_waits_for_running_generation_coverage(self) -> None:
        batch = self._record("coverage", mutations=False)
        self.repo.mark_event_ingested(batch["events"][0]["id"])
        generation = self.os.store.start_provider_sync_generation(
            self.ws.id, "google_drive", self.account_key, "managed:google", "folder:root",
            "baseline", fence=self.fence,
        )
        with self.assertRaises(ValidationError):
            self.repo.complete_batch(
                batch["id"], self.fence.stream_lock_id, self.fence.reservation_token,
                self.fence.credential_binding_id, self.fence.credential_generation,
            )
        self.os.store.cancel_provider_rebootstrap(
            self.ws.id, "google_drive", self.account_key, "managed:google", "folder:root", self.fence
        )
        completed = self.repo.complete_batch(
            batch["id"], self.fence.stream_lock_id, self.fence.reservation_token,
            self.fence.credential_binding_id, self.fence.credential_generation,
        )
        self.assertEqual(completed["status"], "completed")


if __name__ == "__main__":
    unittest.main()
