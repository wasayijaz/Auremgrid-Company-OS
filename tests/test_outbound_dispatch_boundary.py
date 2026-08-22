from __future__ import annotations

import unittest

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS, new_id
from auremgrid.services.integration_security import OutboundSendService, ProviderInstallationService


class OutboundDispatchBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:"); self.org = self.os.create_organization("Secure")
        self.identity = AuthenticatedIdentity("p", self.org.id, "person", "session", frozenset({"integration_configure", "external_send"}))
        self.install = ProviderInstallationService(self.os.store.conn, new_id).create(self.identity, self.org.id, None, "github", "acct", "https://app.test/cb")
        now = "2026-08-22T00:00:00+00:00"
        self.approval = new_id("approval")
        self.os.store.conn.execute("INSERT INTO approval_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (self.approval, self.org.id, None, "person", "p", "provider", "external_send", "{}", "test", "person", "human", "approved", now, None, "", now)); self.os.store.conn.commit()
        self.service = OutboundSendService(self.os.store.conn, new_id)

    def tearDown(self) -> None: self.os.close()

    def test_atomic_approval_idempotency_recovery_and_retry(self) -> None:
        item = self.service.create_intent(self.identity, self.install["id"], self.approval, "send-1", {"message": "hello"})
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0], 1)
        self.assertEqual(self.service.create_intent(self.identity, self.install["id"], self.approval, "send-1", {"message": "hello"})["id"], item["id"])
        attempts = []
        def transport(payload):
            attempts.append(payload)
            if len(attempts) == 1: raise RuntimeError("temporary")
        self.assertEqual(self.service.dispatch(item["id"], transport)["status"], "pending")
        self.assertEqual(self.service.dispatch(item["id"], transport)["status"], "sent")
        self.assertEqual(len(attempts), 2)
        self.os.store.conn.execute("INSERT INTO system_state(key,value,updated_at) VALUES ('recovery_mode','1',?)", ("2026-08-22T00:00:00+00:00",)); self.os.store.conn.commit()
        with self.assertRaises(ValidationError): self.service.create_intent(self.identity, self.install["id"], self.approval, "send-2", {"message": "blocked"})

    def test_approval_and_secret_material_are_required(self) -> None:
        pending = new_id("approval"); now = "2026-08-22T00:00:00+00:00"
        self.os.store.conn.execute("INSERT INTO approval_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (pending, self.org.id, None, "person", "p", "provider", "external_send", "{}", "test", "person", "human", "pending", None, None, "", now)); self.os.store.conn.commit()
        with self.assertRaises(AuthorizationError): self.service.create_intent(self.identity, self.install["id"], pending, "send-pending", {"message": "hello"})
        with self.assertRaises(ValidationError): self.service.create_intent(self.identity, self.install["id"], self.approval, "send-secret", {"authorization": "Bearer abc"})


if __name__ == "__main__": unittest.main()
