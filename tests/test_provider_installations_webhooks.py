from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import unittest
from http.client import HTTPConnection

from auremgrid.domain.errors import AuthorizationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS, new_id
from auremgrid.services.integration_security import ProviderInstallationService, WebhookIntakeService
from auremgrid.api.http import serve


class _Secrets:
    def resolve(self, reference: str) -> str:
        return {"env:HOOK_A": "secret-a", "env:HOOK_B": "secret-b"}[reference]


class ProviderInstallationWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:"); self.org = self.os.create_organization("Secure")
        self.other = self.os.create_organization("Other")
        self.identity = AuthenticatedIdentity("p", self.org.id, "person", "session", frozenset({"integration_configure", "integration_sync"}))
        self.installs = ProviderInstallationService(self.os.store.conn, new_id)
        self.webhooks = WebhookIntakeService(self.os.store.conn, new_id, _Secrets())

    def tearDown(self) -> None: self.os.close()

    def test_multiple_accounts_are_isolated_and_webhook_dedupes(self) -> None:
        a = self.installs.create(self.identity, self.org.id, None, "slack", "team-a", "https://app.test/cb", webhook_secret_reference="env:HOOK_A")
        b = self.installs.create(self.identity, self.org.id, None, "slack", "team-b", "https://app.test/cb", webhook_secret_reference="env:HOOK_B")
        self.assertEqual((a["status"], b["status"]), ("disabled", "disabled"))
        self.os.store.conn.execute("UPDATE provider_installations SET status='active' WHERE id IN (?,?)", (a["id"], b["id"])); self.os.store.conn.commit()
        self.assertNotEqual(a["id"], b["id"])
        body = b'{"event":"ok"}'; signature = "sha256=" + hmac.new(b"secret-a", body, hashlib.sha256).hexdigest(); queued = []
        first = self.webhooks.receive(self.identity, a["id"], body, signature, provider_event_id="evt", enqueue=queued.append, timestamp=int(time.time()))
        duplicate = self.webhooks.receive(self.identity, a["id"], body, signature, provider_event_id="evt", enqueue=queued.append, timestamp=int(time.time()))
        self.assertFalse(first["duplicate"]); self.assertTrue(duplicate["duplicate"]); self.assertEqual(len(queued), 1)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0], 1)
        with self.assertRaises(AuthorizationError): self.webhooks.receive(self.identity, b["id"], body, signature)
        with self.assertRaises(AuthorizationError): self.webhooks.receive(self.identity, a["id"], body, signature, timestamp=int(time.time()) - 1000)
        self.assertNotIn(body.decode(), str(self.os.store.conn.execute("SELECT * FROM webhook_events").fetchall()))


class ProviderWebhookHttpBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("AUREMGRID_WEBHOOK_RECEIPTS_ENABLED", None)
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("HTTP Webhooks", "org_webhook_http")
        self.install_identity = AuthenticatedIdentity("p", self.org.id, "person", "session", frozenset({"integration_configure", "integration_sync"}))
        self.install = ProviderInstallationService(self.os.store.conn, new_id).create(
            self.install_identity, self.org.id, None, "slack", "team-http", "https://app.test/callback", webhook_secret_reference="env:HOOK_HTTP"
        )
        self.os.store.conn.execute("UPDATE provider_installations SET status='active' WHERE id=?", (self.install["id"],)); self.os.store.conn.commit()
        os.environ["HOOK_HTTP"] = "secret-http"
        self.server = serve(self.os, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        os.environ.pop("AUREMGRID_WEBHOOK_RECEIPTS_ENABLED", None); os.environ.pop("HOOK_HTTP", None)
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5); self.os.close()

    def request(self, body: bytes, signature: str = "", timestamp: str | None = None, event_id: str = "evt-http") -> tuple[int, dict]:
        headers = {"Content-Type": "application/octet-stream", "X-Webhook-Signature": signature, "X-Provider-Event-ID": event_id}
        if timestamp is not None: headers["X-Webhook-Timestamp"] = timestamp
        connection = HTTPConnection(self.host, self.port, timeout=5); connection.request("POST", f"/webhooks/provider/{self.install['id']}", body=body, headers=headers)
        response = connection.getresponse(); result = json.loads(response.read()); connection.close(); return response.status, result

    def test_receipt_is_disabled_by_default_and_dedupes_when_enabled(self) -> None:
        body = b'{"event":"ok"}'; signature = "sha256=" + hmac.new(b"secret-http", body, hashlib.sha256).hexdigest(); now = str(int(time.time()))
        status, disabled = self.request(body, signature, now); self.assertEqual(status, 404); self.assertEqual(disabled["error"], "webhook_receipts_disabled")
        os.environ["AUREMGRID_WEBHOOK_RECEIPTS_ENABLED"] = "1"
        status, accepted = self.request(body, signature, now); self.assertEqual(status, 202); self.assertEqual(accepted["status"], "accepted")
        status, duplicate = self.request(body, signature, now); self.assertEqual(status, 200); self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0], 1)

    def test_receipt_rejects_bad_hmac_without_echoing_payload(self) -> None:
        os.environ["AUREMGRID_WEBHOOK_RECEIPTS_ENABLED"] = "1"; body = b'{"secret":"do-not-echo"}'
        status, result = self.request(body, "sha256=bad", str(int(time.time())))
        self.assertEqual(status, 401); self.assertNotIn("do-not-echo", json.dumps(result))


if __name__ == "__main__": unittest.main()
