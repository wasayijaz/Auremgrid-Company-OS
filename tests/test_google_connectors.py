from __future__ import annotations

import urllib.parse
import urllib.error
import unittest
import tempfile
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from auremgrid.connectors.gmail import GMAIL_API, GmailConnector
from auremgrid.connectors.google_auth import (
    DRIVE_READ_SCOPES,
    GMAIL_READ_SCOPES,
    GOOGLE_TOKEN_ENDPOINT,
    ConnectorInboxRepository,
    HttpResponse,
    GoogleOAuthClient,
    UrllibTransport,
    classify_google_failure,
)
from auremgrid.connectors.google_drive import DRIVE_API, GOOGLE_FOLDER, DriveBackfillTask, GoogleDriveConnector
from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS, new_id


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self.responses: list[HttpResponse] = []

    def queue(self, response: HttpResponse) -> None:
        self.responses.append(response)

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        self.calls.append((method, url, headers or {}, body))
        if not self.responses:
            raise AssertionError(f"unexpected HTTP call: {method} {url}")
        return self.responses.pop(0)


class GoogleConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.person = self.os.create_person(self.org.id, "Ops Lead", "ops@example.test", role="admin")
        self.principal = self.os.auth.create_principal(self.org.id, self.person.id, "ops@example.test")
        self.repo = ConnectorInboxRepository(self.os.store.conn, new_id)

    def tearDown(self) -> None:
        self.os.close()

    def test_v12_schema_has_connector_inbox_without_token_value_columns(self) -> None:
        self.assertEqual(self.os.store.schema_version, 14)
        self.assertTrue(
            {
                "connector_cursors",
                "connector_ingest_batches",
                "connector_source_events",
                "connector_dedupe_keys",
                "connector_batch_events",
                "connector_stream_locks",
            }
            <= self._tables()
        )
        connector_columns = set()
        for table in ("connector_cursors", "connector_ingest_batches", "connector_source_events", "connector_dedupe_keys"):
            connector_columns |= {
                row["name"]
                for row in self.os.store.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
        self.assertNotIn("access_token", connector_columns)
        self.assertNotIn("refresh_token", connector_columns)
        integration_columns = {
            row["name"]
            for row in self.os.store.conn.execute("PRAGMA table_info(integrations)").fetchall()
        }
        self.assertTrue(
            {
                "expected_account_id",
                "provider_account_id",
                "provider_account_name",
                "granted_permissions",
                "credential_verified_at",
            }
            <= integration_columns
        )
        stream_lock_columns = {
            row["name"]
            for row in self.os.store.conn.execute("PRAGMA table_info(connector_stream_locks)").fetchall()
        }
        self.assertTrue(
            {
                "stream_key",
                "job_id",
                "mapping_hash",
                "lease_owner",
                "reservation_token",
                "lease_expires_at",
                "status",
            }
            <= stream_lock_columns
        )

    def test_oauth_refresh_uses_official_endpoint_and_keeps_tokens_out_of_db(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"access_token": "ya29.access", "expires_in": 120, "scope": "drive.readonly gmail.readonly"}))
        result = GoogleOAuthClient(transport).refresh_access_token("client-id", "client-secret", "refresh-token")
        method, url, headers, body = transport.calls[0]
        form = urllib.parse.parse_qs(body.decode("utf-8"))
        self.assertEqual(method, "POST")
        self.assertEqual(url, GOOGLE_TOKEN_ENDPOINT)
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(result.access_token, "ya29.access")
        self.assertEqual(result.scopes, ("drive.readonly", "gmail.readonly"))
        rows = self.os.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        self.assertTrue(rows)

    def test_drive_initial_pull_returns_start_page_token_without_events(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"startPageToken": "drive-token-1"}))
        result = GoogleDriveConnector("access", transport).pull(None)
        self.assertEqual(result.events, [])
        self.assertEqual(json.loads(result.next_cursor)["checkpoint"], "drive-token-1")
        self.assertEqual(transport.calls[0][1], f"{DRIVE_API}/changes/startPageToken")

    def test_drive_changes_record_dedupe_and_advance_cursor_only_after_ingestion(self) -> None:
        transport = FakeTransport()
        transport.queue(
            HttpResponse(
                200,
                {},
                {
                    "newStartPageToken": "drive-token-2",
                    "changes": [
                        {
                            "fileId": "file-1",
                            "time": "2026-08-19T12:00:00Z",
                            "file": {
                                "id": "file-1",
                                "name": "Brief",
                                "mimeType": "application/vnd.google-apps.document",
                                "modifiedTime": "2026-08-19T12:00:00Z",
                                "webViewLink": "https://drive.google.com/file/d/file-1",
                                "parents": ["folder-1"],
                            },
                        }
                    ],
                },
            )
        )
        transport.queue(HttpResponse(200, {}, None, "Approved launch brief"))
        result = GoogleDriveConnector("access", transport, folder_workspace_mappings={"folder-1": self.ws.id}).pull(
            json.dumps({"v": 1, "phase": "changes", "checkpoint": "drive-token-1", "page_token": None})
        )
        self.assertEqual(len(result.events), 1)
        self.assertIn("/changes?", transport.calls[0][1])
        self.assertIn("/files/file-1/export?mimeType=text/plain", transport.calls[1][1])
        batch = self.repo.record_pull(
            self.org.id, self.ws.id, "google_drive", "account@example.test", "drive-token-1", result.next_cursor, result.events
        )
        event = batch["events"][0]
        self.assertEqual(event["status"], "pending")
        self.assertIsNone(self.repo.get_cursor(self.org.id, self.ws.id, "google_drive", "account@example.test"))
        self.repo.mark_event_ingested(event["id"])
        self.repo.complete_batch(batch["id"])
        promoted = self.repo.get_cursor(self.org.id, self.ws.id, "google_drive", "account@example.test")
        self.assertEqual(json.loads(promoted)["checkpoint"], "drive-token-2")
        duplicate = self.repo.record_pull(
            self.org.id, self.ws.id, "google_drive", "account@example.test", "drive-token-2", "drive-token-3", result.events
        )
        self.assertEqual(duplicate["events"][0]["id"], event["id"])
        self.assertEqual(duplicate["events"][0]["status"], "skipped")
        self.assertEqual(duplicate["events"][0]["original_status"], "ingested")
        self.repo.complete_batch(duplicate["id"])
        self.assertEqual(
            self.repo.get_cursor(self.org.id, self.ws.id, "google_drive", "account@example.test"),
            "drive-token-3",
        )

    def test_failed_ingestion_does_not_advance_cursor(self) -> None:
        transport = FakeTransport()
        transport.queue(
            HttpResponse(
                200,
                {},
                {
                    "newStartPageToken": "drive-token-2",
                    "changes": [{"fileId": "file-2", "removed": True, "time": "2026-08-19T12:00:00Z"}],
                },
            )
        )
        result = GoogleDriveConnector(
            "access", transport, folder_workspace_mappings={"folder-1": self.ws.id},
            route_state={"file-2": ["folder:folder-1"]},
        ).pull(json.dumps({"v": 1, "phase": "changes", "checkpoint": "drive-token-1", "page_token": None}))
        batch = self.repo.record_pull(
            self.org.id, self.ws.id, "google_drive", "account@example.test", "drive-token-1", result.next_cursor, result.events
        )
        self.repo.mark_event_failed(batch["events"][0]["id"], "ingest failed")
        with self.assertRaises(ValidationError):
            self.repo.complete_batch(batch["id"])
        self.assertIsNone(self.repo.get_cursor(self.org.id, self.ws.id, "google_drive", "account@example.test"))
        duplicate = self.repo.record_pull(
            self.org.id, self.ws.id, "google_drive", "account@example.test", "drive-token-1", result.next_cursor, result.events
        )
        self.assertEqual(duplicate["events"][0]["id"], batch["events"][0]["id"])
        self.assertEqual(duplicate["events"][0]["status"], "failed")
        with self.assertRaises(ValidationError):
            self.repo.complete_batch(duplicate["id"])

    def test_replay_reuses_original_event_after_crash_before_mark_ingested(self) -> None:
        event = self._source_event()
        first = self.repo.record_pull(
            self.org.id, self.ws.id, "google_drive", "account@example.test", None, "cursor-1", [event]
        )
        claimed = self.repo.claim_event(self.org.id, self.ws.id, "google_drive", "account@example.test", "worker-a")
        self.assertEqual(claimed["id"], first["events"][0]["id"])
        replay = self.repo.record_pull(
            self.org.id, self.ws.id, "google_drive", "account@example.test", None, "cursor-1", [event]
        )
        self.assertEqual(replay["events"][0]["id"], first["events"][0]["id"])
        with self.assertRaises(ValidationError):
            self.repo.complete_batch(replay["id"])
        self.repo.complete_event(claimed["id"], "worker-a", claimed["lease_token"])
        self.repo.complete_batch(replay["id"])
        self.assertEqual(
            self.repo.get_cursor(self.org.id, self.ws.id, "google_drive", "account@example.test"),
            "cursor-1",
        )

    def test_two_worker_event_lease_exclusion_and_expired_recovery(self) -> None:
        now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        batch = self.repo.record_pull(
            self.org.id, self.ws.id, "google_drive", "account@example.test", None, "cursor-1", [self._source_event()]
        )
        first = self.repo.claim_event(
            self.org.id, self.ws.id, "google_drive", "account@example.test", "worker-a", lease_seconds=10, now=now
        )
        self.assertEqual(first["id"], batch["events"][0]["id"])
        self.assertIsNone(
            self.repo.claim_event(self.org.id, self.ws.id, "google_drive", "account@example.test", "worker-b", now=now)
        )
        with self.assertRaises(ValidationError):
            self.repo.complete_event(first["id"], "worker-b", first["lease_token"], now=now + timedelta(seconds=1))
        recovered = self.repo.claim_event(
            self.org.id,
            self.ws.id,
            "google_drive",
            "account@example.test",
            "worker-b",
            lease_seconds=10,
            now=now + timedelta(seconds=11),
        )
        self.assertEqual(recovered["id"], first["id"])
        self.assertNotEqual(recovered["lease_token"], first["lease_token"])
        with self.assertRaises(ValidationError):
            self.repo.complete_event(first["id"], "worker-a", first["lease_token"], now=now + timedelta(seconds=12))

    def test_poison_event_can_be_quarantined_and_batch_completed(self) -> None:
        batch = self.repo.record_pull(
            self.org.id, self.ws.id, "google_drive", "account@example.test", None, "cursor-1", [self._source_event()]
        )
        claimed = self.repo.claim_event(self.org.id, self.ws.id, "google_drive", "account@example.test", "worker-a")
        quarantined = self.repo.quarantine_event(claimed["id"], "worker-a", claimed["lease_token"], "poison payload")
        self.assertEqual(quarantined["status"], "quarantined")
        self.repo.complete_batch(batch["id"])
        self.assertEqual(
            self.repo.get_cursor(self.org.id, self.ws.id, "google_drive", "account@example.test"),
            "cursor-1",
        )

    def test_cursor_promotion_rejects_stale_batch_fence(self) -> None:
        event = self._source_event()
        first = self.repo.record_pull(
            self.org.id, self.ws.id, "google_drive", "account@example.test", None, "cursor-1", [event]
        )
        second = self.repo.record_pull(
            self.org.id, self.ws.id, "google_drive", "account@example.test", None, "cursor-2", [event]
        )
        self.repo.mark_event_ingested(first["events"][0]["id"])
        self.repo.complete_batch(first["id"])
        with self.assertRaises(ValidationError):
            self.repo.complete_batch(second["id"])

    def test_stream_lock_requires_terminal_job_or_explicit_cancel_and_fences_stale_token(self) -> None:
        now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        job = self.os.jobs.enqueue_job(
            self.org.id,
            self.ws.id,
            self.principal["id"],
            "connector.sync",
            {"stream": "drive-changes"},
        )
        lock = self.repo.reserve_stream(
            self.org.id,
            self.ws.id,
            "google_drive",
            "account@example.test",
            "google_drive:account@example.test:changes",
            job["id"],
            "mapping-hash-1",
            "worker-a",
            lease_seconds=30,
            now=now,
        )
        self.assertEqual(lock["status"], "active")
        self.assertEqual(
            self.repo.active_stream_lock(
                self.org.id, self.ws.id, "google_drive", "google_drive:account@example.test:changes"
            )["id"],
            lock["id"],
        )
        with self.assertRaises(ValidationError):
            self.repo.release_stream(lock["id"], lock["reservation_token"], now=now + timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            self.repo.heartbeat_stream(lock["id"], "wrong-token", now=now + timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            self.repo.heartbeat_stream(lock["id"], lock["reservation_token"], now=now + timedelta(seconds=31))
        self.os.jobs.cancel_job(
            self.org.id,
            self.ws.id,
            job["id"],
            "superseded stream request",
            now=now + timedelta(seconds=2),
        )
        released = self.repo.release_stream(lock["id"], lock["reservation_token"], now=now + timedelta(seconds=3))
        self.assertEqual(released["status"], "released")

    def test_stream_lock_replace_only_after_linked_job_terminal(self) -> None:
        now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        first_job = self.os.jobs.enqueue_job(
            self.org.id,
            self.ws.id,
            self.principal["id"],
            "connector.sync",
            {"stream": "gmail-history", "run": 1},
        )
        second_job = self.os.jobs.enqueue_job(
            self.org.id,
            self.ws.id,
            self.principal["id"],
            "connector.sync",
            {"stream": "gmail-history", "run": 2},
        )
        lock = self.repo.reserve_stream(
            self.org.id,
            self.ws.id,
            "gmail",
            "account@example.test",
            "gmail:account@example.test:history",
            first_job["id"],
            "mapping-hash-1",
            "worker-a",
            lease_seconds=60,
            now=now,
        )
        with self.assertRaises(ValidationError):
            self.repo.replace_stream(
                lock["id"],
                lock["reservation_token"],
                second_job["id"],
                "mapping-hash-2",
                "worker-b",
                now=now + timedelta(seconds=1),
            )
        self.os.jobs.cancel_job(
            self.org.id,
            self.ws.id,
            first_job["id"],
            "replace with newer mapping",
            now=now + timedelta(seconds=2),
        )
        replacement = self.repo.replace_stream(
            lock["id"],
            lock["reservation_token"],
            second_job["id"],
            "mapping-hash-2",
            "worker-b",
            now=now + timedelta(seconds=3),
        )
        self.assertEqual(replacement["status"], "active")
        self.assertEqual(replacement["job_id"], second_job["id"])
        self.assertNotEqual(replacement["reservation_token"], lock["reservation_token"])
        self.assertEqual(self.repo.get_stream_lock(lock["id"])["status"], "replaced")
        with self.assertRaises(ValidationError):
            self.repo.cancel_stream(replacement["id"], lock["reservation_token"], now=now + timedelta(seconds=4))

    def test_stream_lock_explicit_cancel_frees_stream_without_job_terminal(self) -> None:
        now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        first_job = self.os.jobs.enqueue_job(
            self.org.id,
            self.ws.id,
            self.principal["id"],
            "connector.sync",
            {"stream": "drive-cancel", "run": 1},
        )
        second_job = self.os.jobs.enqueue_job(
            self.org.id,
            self.ws.id,
            self.principal["id"],
            "connector.sync",
            {"stream": "drive-cancel", "run": 2},
        )
        lock = self.repo.reserve_stream(
            self.org.id,
            self.ws.id,
            "google_drive",
            "account@example.test",
            "google_drive:account@example.test:cancel",
            first_job["id"],
            "mapping-hash-1",
            "worker-a",
            lease_seconds=60,
            now=now,
        )
        cancelled = self.repo.cancel_stream(lock["id"], lock["reservation_token"], now=now + timedelta(seconds=1))
        self.assertEqual(cancelled["status"], "cancelled")
        next_lock = self.repo.reserve_stream(
            self.org.id,
            self.ws.id,
            "google_drive",
            "account@example.test",
            "google_drive:account@example.test:cancel",
            second_job["id"],
            "mapping-hash-2",
            "worker-b",
            lease_seconds=60,
            now=now + timedelta(seconds=2),
        )
        self.assertEqual(next_lock["job_id"], second_job["id"])

    def test_stream_lock_two_file_connection_race_is_db_enforced(self) -> None:
        now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "company-os.sqlite"
            first_os = CompanyOS(db_path)
            second_os = None
            try:
                org = first_os.create_organization("Agency")
                ws = first_os.create_organization_workspace(org.id, "Prime", "client")
                person = first_os.create_person(org.id, "Ops Lead", "ops@example.test", role="admin")
                principal = first_os.auth.create_principal(org.id, person.id, "ops@example.test")
                first_job = first_os.jobs.enqueue_job(
                    org.id,
                    ws.id,
                    principal["id"],
                    "connector.sync",
                    {"stream": "race", "run": 1},
                )
                second_os = CompanyOS(db_path)
                second_job = second_os.jobs.enqueue_job(
                    org.id,
                    ws.id,
                    principal["id"],
                    "connector.sync",
                    {"stream": "race", "run": 2},
                )
                first_repo = ConnectorInboxRepository(first_os.store.conn, new_id)
                second_repo = ConnectorInboxRepository(second_os.store.conn, new_id)
                first_lock = first_repo.reserve_stream(
                    org.id,
                    ws.id,
                    "gmail",
                    "account@example.test",
                    "gmail:account@example.test:race",
                    first_job["id"],
                    "mapping-hash-1",
                    "worker-a",
                    lease_seconds=60,
                    now=now,
                )
                with self.assertRaises(ValidationError):
                    second_repo.reserve_stream(
                        org.id,
                        ws.id,
                        "gmail",
                        "account@example.test",
                        "gmail:account@example.test:race",
                        second_job["id"],
                        "mapping-hash-2",
                        "worker-b",
                        lease_seconds=60,
                        now=now,
                    )
                first_os.jobs.cancel_job(
                    org.id,
                    ws.id,
                    first_job["id"],
                    "finished stream reservation",
                    now=now + timedelta(seconds=1),
                )
                first_repo.release_stream(first_lock["id"], first_lock["reservation_token"], now=now + timedelta(seconds=2))
                second_lock = second_repo.reserve_stream(
                    org.id,
                    ws.id,
                    "gmail",
                    "account@example.test",
                    "gmail:account@example.test:race",
                    second_job["id"],
                    "mapping-hash-2",
                    "worker-b",
                    lease_seconds=60,
                    now=now + timedelta(seconds=3),
                )
                self.assertEqual(second_lock["status"], "active")
                self.assertEqual(second_lock["job_id"], second_job["id"])
            finally:
                if second_os is not None:
                    second_os.close()
                first_os.close()

    def test_drive_rate_limit_returns_retry_metadata_without_sleeping(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(429, {"Retry-After": "30"}, {"error": {"message": "quota"}}, "quota"))
        cursor = json.dumps({"v": 1, "phase": "changes", "checkpoint": "drive-token-1", "page_token": None})
        result = GoogleDriveConnector("access", transport).pull(cursor)
        self.assertTrue(result.rate_limited)
        self.assertEqual(result.retry_after_seconds, 30)
        self.assertEqual(json.loads(result.next_cursor), json.loads(cursor))

    def test_gmail_initial_and_history_pull_follow_history_semantics(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"historyId": "100"}))
        initial = GmailConnector("access", transport).pull(None)
        self.assertEqual(initial.events, [])
        self.assertEqual(json.loads(initial.next_cursor)["checkpoint"], "100")
        self.assertEqual(transport.calls[0][1], f"{GMAIL_API}/users/me/profile")
        transport.queue(
            HttpResponse(
                200,
                {},
                {"historyId": "101", "history": [{"id": "101", "messagesAdded": [{"message": {"id": "msg-1", "labelIds": ["Label_1"]}}]}]},
            )
        )
        transport.queue(
            HttpResponse(
                200,
                {},
                {
                    "id": "msg-1",
                    "threadId": "thread-1",
                    "snippet": "Hello from the client",
                    "internalDate": "1787140800000",
                    "labelIds": ["Label_1"],
                    "payload": {"headers": [{"name": "Subject", "value": "Launch"}, {"name": "From", "value": "client@example.test"}]},
                },
            )
        )
        result = GmailConnector("access", transport, label_workspace_mappings={"label:Label_1": self.ws.id}).pull(
            json.dumps({"v": 1, "phase": "history", "checkpoint": "100", "page_token": None})
        )
        self.assertEqual(json.loads(result.next_cursor)["checkpoint"], "101")
        self.assertEqual(result.events[0].dedupe_key, "gmail:msg-1:101:message_added")
        self.assertIn("/users/me/history?", transport.calls[1][1])
        self.assertIn("/users/me/messages/msg-1?", transport.calls[2][1])

    def test_gmail_expired_history_cursor_is_reported_without_cursor_advance(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(404, {}, {"error": {"message": "History expired"}}, "History expired"))
        result = GmailConnector("access", transport).pull(
            json.dumps({"v": 1, "phase": "history", "checkpoint": "old-history", "page_token": None})
        )
        self.assertTrue(result.cursor_expired)
        self.assertIsNone(result.next_cursor)

    def test_oauth_never_treats_requested_scopes_as_granted(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"access_token": "access", "expires_in": 60}))
        result = GoogleOAuthClient(transport).refresh_access_token(
            "client", "secret", "refresh", tuple(DRIVE_READ_SCOPES)
        )
        self.assertEqual(result.scopes, ())

    def test_drive_verifies_immutable_identity_scope_and_folder(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"user": {"permissionId": "perm-1", "emailAddress": "ops@example.test", "displayName": "Ops"}}))
        transport.queue(HttpResponse(200, {}, {"id": "folder-1", "mimeType": GOOGLE_FOLDER, "trashed": False}))
        connector = GoogleDriveConnector(
            "access", transport, folder_workspace_mappings={"folder-1": self.ws.id},
            expected_account_id="perm-1", granted_scopes=(next(iter(DRIVE_READ_SCOPES)),),
        )
        identity = connector.verify_credentials()
        self.assertEqual(identity.account_id, "perm-1")
        self.assertEqual(identity.email_address, "ops@example.test")
        self.assertIn("/about?", transport.calls[0][1])
        self.assertIn("/files/folder-1?", transport.calls[1][1])

    def test_drive_rejects_missing_actual_scope_without_network_call(self) -> None:
        transport = FakeTransport()
        with self.assertRaises(ValidationError):
            GoogleDriveConnector("access", transport, granted_scopes=()).verify_credentials()
        self.assertEqual(transport.calls, [])

    def test_drive_recursive_backfill_outputs_durable_tasks_and_route_mutations(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"startPageToken": "base-1"}))
        transport.queue(HttpResponse(200, {}, {"files": [
            {"id": "subfolder", "name": "Nested", "mimeType": GOOGLE_FOLDER, "parents": ["root"]},
            {"id": "file-1", "name": "Secret access", "mimeType": "application/pdf", "parents": ["root"], "modifiedTime": "2026-08-19T12:00:00Z"},
        ]}))
        result = GoogleDriveConnector(
            "access", transport, folder_workspace_mappings={"root": self.ws.id}
        ).pull(None)
        self.assertEqual(json.loads(result.next_cursor)["phase"], "backfill")
        self.assertEqual(result.events[0].source_key, "google-drive/files/subfolder")
        self.assertEqual(result.lifecycle_mutations[0].route_key, "folder:root")
        self.assertEqual(result.backfill_tasks, (DriveBackfillTask("folder:root", "subfolder"),))
        self.assertNotIn("access", json.dumps(result.events[1].payload))

    def test_drive_changes_are_one_page_and_expose_continuation(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"nextPageToken": "page-2", "changes": []}))
        cursor = json.dumps({"v": 1, "phase": "changes", "checkpoint": "base-1", "page_token": None})
        result = GoogleDriveConnector("access", transport).pull(cursor)
        self.assertTrue(result.has_more)
        self.assertEqual(json.loads(result.next_cursor)["page_token"], "page-2")
        self.assertEqual(len(transport.calls), 1)

    def test_drive_distinguishes_quota_403_from_permission_403(self) -> None:
        cursor = json.dumps({"v": 1, "phase": "changes", "checkpoint": "base-1", "page_token": None})
        quota = FakeTransport()
        quota.queue(HttpResponse(403, {"Retry-After": "7"}, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}))
        quota_result = GoogleDriveConnector("access", quota).pull(cursor)
        self.assertEqual(quota_result.error_code, "quota_exhausted")
        self.assertTrue(quota_result.retryable)
        self.assertEqual(quota_result.retry_after_seconds, 7)
        permission = FakeTransport()
        permission.queue(HttpResponse(403, {}, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}))
        permission_result = GoogleDriveConnector("access", permission).pull(cursor)
        self.assertEqual(permission_result.error_code, "permission_denied")
        self.assertFalse(permission_result.retryable)

    def test_drive_removed_object_emits_tombstone_from_durable_route_state(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"newStartPageToken": "base-2", "changes": [
            {"fileId": "file-1", "removed": True, "time": "2026-08-19T12:00:00Z"}
        ]}))
        cursor = json.dumps({"v": 1, "phase": "changes", "checkpoint": "base-1", "page_token": None})
        result = GoogleDriveConnector(
            "access", transport, folder_workspace_mappings={"root": self.ws.id},
            route_state={"file-1": ["folder:root"]},
        ).pull(cursor)
        self.assertEqual(result.events[0].event_type, "file_removed")
        self.assertEqual(result.lifecycle_mutations[0].operation, "tombstone")

    def test_gmail_verifies_identity_scope_and_canonical_labels(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"emailAddress": "ops@example.test", "historyId": "100"}))
        transport.queue(HttpResponse(200, {}, {"labels": [{"id": "Label_1"}]}))
        connector = GmailConnector(
            "access", transport, label_workspace_mappings={"label:Label_1": self.ws.id},
            expected_account_id="OPS@example.test", granted_scopes=(next(iter(GMAIL_READ_SCOPES)),),
        )
        self.assertEqual(connector.verify_credentials().email_address, "ops@example.test")
        with self.assertRaises(ValidationError):
            GmailConnector("access", FakeTransport(), label_workspace_mappings={"Label_1": self.ws.id})

    def test_gmail_baseline_precedes_backfill_and_outputs_route_mutation(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"emailAddress": "ops@example.test", "historyId": "100"}))
        transport.queue(HttpResponse(200, {}, {"messages": [{"id": "msg-1"}]}))
        transport.queue(HttpResponse(200, {}, {"id": "msg-1", "labelIds": ["Label_1"], "internalDate": "1787140800000", "payload": {"headers": []}}))
        result = GmailConnector(
            "access", transport, label_workspace_mappings={"label:Label_1": self.ws.id}
        ).pull(None)
        self.assertIn("/profile", transport.calls[0][1])
        self.assertIn("/messages?", transport.calls[1][1])
        self.assertEqual(result.events[0].source_key, "gmail/messages/msg-1")
        self.assertEqual(result.lifecycle_mutations[0].route_key, "label:Label_1")

    def test_gmail_label_removal_emits_deterministic_tombstone(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"historyId": "101", "history": [{
            "id": "101", "labelsRemoved": [{"message": {"id": "msg-1"}, "labelIds": ["Label_1"]}]
        }]}))
        cursor = json.dumps({"v": 1, "phase": "history", "checkpoint": "100", "page_token": None})
        result = GmailConnector(
            "access", transport, label_workspace_mappings={"label:Label_1": self.ws.id}
        ).pull(cursor)
        self.assertEqual(result.events[0].event_type, "labels_removed")
        self.assertEqual(result.events[0].dedupe_key, "gmail:msg-1:101:labels_removed")
        self.assertEqual(result.lifecycle_mutations[0].operation, "tombstone")

    def test_drive_nested_change_consumes_durable_root_ancestry(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"newStartPageToken": "base-2", "changes": [{
            "fileId": "file-1", "time": "2026-08-19T12:00:00Z",
            "file": {"id": "file-1", "name": "Nested", "mimeType": "application/pdf", "parents": ["nested"]},
        }]}))
        cursor = json.dumps({"v": 1, "phase": "changes", "checkpoint": "base-1", "page_token": None})
        result = GoogleDriveConnector(
            "access", transport, folder_workspace_mappings={"root": self.ws.id},
            route_state={"file-1": ["folder:root"]},
            ancestry_state={"nested": {"route_keys": ["folder:root"], "reconciliation_status": "resolved"}},
        ).pull(cursor)
        self.assertEqual(result.events[0].event_type, "file_changed")
        self.assertEqual(result.events[0].payload["route_keys"], ["folder:root"])
        self.assertFalse(any(item.operation == "tombstone" for item in result.lifecycle_mutations))
        self.assertEqual(result.reconciliation_requests, ())

    def test_drive_unknown_ancestry_preserves_route_and_requests_reconciliation(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"newStartPageToken": "base-2", "changes": [{
            "fileId": "file-1", "time": "2026-08-19T12:00:00Z",
            "file": {"id": "file-1", "name": "Unknown parent", "mimeType": "application/pdf", "parents": ["missing"]},
        }]}))
        cursor = json.dumps({"v": 1, "phase": "changes", "checkpoint": "base-1", "page_token": None})
        result = GoogleDriveConnector(
            "access", transport, folder_workspace_mappings={"root": self.ws.id},
            route_state={"file-1": ["folder:root"]}, ancestry_state={},
        ).pull(cursor)
        self.assertEqual(result.events[0].payload["route_keys"], ["folder:root"])
        self.assertFalse(any(item.operation == "tombstone" for item in result.lifecycle_mutations))
        self.assertEqual(result.reconciliation_requests[0].parent_ids, ("missing",))
        self.assertEqual(result.reconciliation_requests[0].reason, "unknown_ancestry")

    def test_drive_repeated_removals_have_stable_distinct_transition_keys(self) -> None:
        change = {"fileId": "file-1", "removed": True, "time": "2026-08-19T12:00:00Z"}

        def pull(checkpoint: str) -> object:
            transport = FakeTransport()
            transport.queue(HttpResponse(200, {}, {"newStartPageToken": checkpoint + "-next", "changes": [change]}))
            cursor = json.dumps({"v": 1, "phase": "changes", "checkpoint": checkpoint, "page_token": None})
            return GoogleDriveConnector(
                "access", transport, folder_workspace_mappings={"root": self.ws.id},
                route_state={"file-1": ["folder:root"]},
            ).pull(cursor)

        first = pull("base-1")
        replay = pull("base-1")
        repeated = pull("base-2")
        self.assertEqual(first.events[0].dedupe_key, replay.events[0].dedupe_key)
        self.assertNotEqual(first.events[0].dedupe_key, repeated.events[0].dedupe_key)
        self.assertEqual(first.lifecycle_mutations[0].event_dedupe_key, first.events[0].dedupe_key)

    def test_drive_content_export_failure_is_structured_without_cursor_advance(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"newStartPageToken": "base-2", "changes": [{
            "fileId": "doc-1", "time": "2026-08-19T12:00:00Z",
            "file": {"id": "doc-1", "name": "Doc", "mimeType": "application/vnd.google-apps.document", "parents": ["root"]},
        }]}))
        transport.queue(HttpResponse(403, {"Retry-After": "9"}, {"error": {"errors": [{"reason": "quotaExceeded"}]}}))
        cursor = json.dumps({"v": 1, "phase": "changes", "checkpoint": "base-1", "page_token": None})
        result = GoogleDriveConnector(
            "access", transport, folder_workspace_mappings={"root": self.ws.id}
        ).pull(cursor)
        self.assertEqual(result.error_code, "quota_exhausted")
        self.assertTrue(result.retryable)
        self.assertEqual(result.retry_after_seconds, 9)
        self.assertEqual(result.next_cursor, cursor)
        self.assertEqual(result.events, [])

    def test_gmail_message_fetch_failure_is_structured_without_cursor_advance(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"historyId": "101", "history": [{
            "id": "101", "messagesAdded": [{"message": {"id": "msg-1", "labelIds": ["Label_1"]}}],
        }]}))
        transport.queue(HttpResponse(401, {}, {"error": {"errors": [{"reason": "authError"}]}}))
        cursor = json.dumps({"v": 1, "phase": "history", "checkpoint": "100", "page_token": None})
        result = GmailConnector(
            "access", transport, label_workspace_mappings={"label:Label_1": self.ws.id}
        ).pull(cursor)
        self.assertEqual(result.error_code, "authorization_required")
        self.assertFalse(result.retryable)
        self.assertEqual(result.next_cursor, cursor)
        self.assertEqual(result.events, [])

    def test_strict_google_cursor_validation_rejects_extra_missing_and_wrong_fields(self) -> None:
        invalid_drive = (
            {"v": 1, "phase": "changes", "checkpoint": "c"},
            {"v": 1, "phase": "changes", "checkpoint": "c", "page_token": None, "extra": True},
            {"v": 1, "phase": "backfill", "checkpoint": 1},
        )
        for value in invalid_drive:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                GoogleDriveConnector("access", FakeTransport()).pull(json.dumps(value))
        invalid_gmail = (
            {"v": 1, "phase": "history", "checkpoint": "c"},
            {"v": 1, "phase": "backfill", "checkpoint": "c", "label_index": True, "page_token": None},
            {"v": 1, "phase": "history", "checkpoint": "c", "page_token": ""},
        )
        for value in invalid_gmail:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                GmailConnector("access", FakeTransport()).pull(json.dumps(value))

    def test_malformed_success_pages_fail_before_cursor_advancement(self) -> None:
        drive = FakeTransport()
        drive.queue(HttpResponse(200, {}, {"newStartPageToken": "base-2", "changes": {}}))
        drive_cursor = json.dumps({"v": 1, "phase": "changes", "checkpoint": "base-1", "page_token": None})
        with self.assertRaises(ValidationError):
            GoogleDriveConnector("access", drive).pull(drive_cursor)
        gmail = FakeTransport()
        gmail.queue(HttpResponse(200, {}, {"historyId": "101", "history": {}}))
        gmail_cursor = json.dumps({"v": 1, "phase": "history", "checkpoint": "100", "page_token": None})
        with self.assertRaises(ValidationError):
            GmailConnector("access", gmail).pull(gmail_cursor)

    def test_network_and_transient_status_classification_is_stable(self) -> None:
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("secret host detail")):
            response = UrllibTransport()("GET", "https://example.invalid")
        network = classify_google_failure(response, "Google")
        self.assertEqual(network.code, "network_error")
        self.assertTrue(network.retryable)
        self.assertNotIn("secret host detail", network.message)
        self.assertEqual(classify_google_failure(HttpResponse(408, {}, {}), "Google").code, "request_timeout")
        self.assertEqual(classify_google_failure(HttpResponse(425, {}, {}), "Google").code, "too_early")
        multi = HttpResponse(403, {}, {"error": {"errors": [
            {"reason": "userRateLimitExceeded"}, {"reason": "dailyLimitExceeded"},
        ]}})
        first = classify_google_failure(multi, "Google")
        second = classify_google_failure(multi, "Google")
        self.assertEqual(first, second)
        self.assertIn("dailylimitexceeded", first.message)

    def test_drive_expected_identity_must_be_permission_id_not_email(self) -> None:
        transport = FakeTransport()
        transport.queue(HttpResponse(200, {}, {"user": {
            "permissionId": "perm-1", "emailAddress": "ops@example.test", "displayName": "Ops",
        }}))
        with self.assertRaises(AuthorizationError):
            GoogleDriveConnector(
                "access", transport, expected_account_id="ops@example.test",
                granted_scopes=(next(iter(DRIVE_READ_SCOPES)),),
            ).verify_credentials()

    def _tables(self) -> set[str]:
        return {
            row["name"]
            for row in self.os.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

    def _source_event(self):
        event, _, _ = GoogleDriveConnector(
            "access", FakeTransport(), folder_workspace_mappings={"folder-1": self.ws.id},
            route_state={"file-replay": ["folder:folder-1"]},
        )._event_from_change(
            {"fileId": "file-replay", "removed": True, "time": "2026-08-19T12:00:00Z"}
        )
        return event


if __name__ == "__main__":
    unittest.main()
