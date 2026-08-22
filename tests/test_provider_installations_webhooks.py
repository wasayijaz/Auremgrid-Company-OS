from __future__ import annotations

import hashlib
import hmac
import time
import unittest

from auremgrid.domain.errors import AuthorizationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS, new_id
from auremgrid.services.integration_security import ProviderInstallationService, WebhookIntakeService


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
        self.assertNotEqual(a["id"], b["id"])
        body = b'{"event":"ok"}'; signature = "sha256=" + hmac.new(b"secret-a", body, hashlib.sha256).hexdigest(); queued = []
        first = self.webhooks.receive(self.identity, a["id"], body, signature, provider_event_id="evt", enqueue=queued.append, timestamp=int(time.time()))
        duplicate = self.webhooks.receive(self.identity, a["id"], body, signature, provider_event_id="evt", enqueue=queued.append, timestamp=int(time.time()))
        self.assertFalse(first["duplicate"]); self.assertTrue(duplicate["duplicate"]); self.assertEqual(len(queued), 1)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0], 1)
        with self.assertRaises(AuthorizationError): self.webhooks.receive(self.identity, b["id"], body, signature)
        with self.assertRaises(AuthorizationError): self.webhooks.receive(self.identity, a["id"], body, signature, timestamp=int(time.time()) - 1000)
        self.assertNotIn(body.decode(), str(self.os.store.conn.execute("SELECT * FROM webhook_events").fetchall()))


if __name__ == "__main__": unittest.main()
