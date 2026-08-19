from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError
from auremgrid.domain.security import AuthenticatedIdentity, CAPABILITIES
from auremgrid.services.brain import CompanyOS, new_id
from auremgrid.services.secrets import EnvironmentSecretStore, SecretBindingService, redact


class SecretBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "company.sqlite"
        self.os = CompanyOS(self.db_path)
        self.org = self.os.create_organization("Auremgrid")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client")
        self.person = self.os.create_person(self.org.id, "Owner", role="owner")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")
        self.identity = AuthenticatedIdentity(
            "principal_test", self.org.id, self.person.id, "session", frozenset(CAPABILITIES), workspace_id=self.ws.id
        )
        self.env_name = "AUREMGRID_TEST_SECRET_SENTINEL"
        self.secret = "sentinel-secret-must-not-persist"
        os.environ[self.env_name] = self.secret
        self.service = SecretBindingService(
            self.os.store.conn, new_id, EnvironmentSecretStore({self.env_name})
        )

    def tearDown(self) -> None:
        self.os.close()
        os.environ.pop(self.env_name, None)
        self.temp.cleanup()

    def test_binding_persists_reference_only_and_resolves_at_use_time(self) -> None:
        binding = self.service.create(
            self.identity, self.org.id, "Connector key", "example", f"env:{self.env_name}", ["sync"], self.ws.id
        )
        self.assertNotIn(self.secret, str(binding))
        self.assertNotIn(self.secret.encode(), self.db_path.read_bytes())
        self.assertEqual(self.service.resolve_for_use(self.identity, binding["id"], "sync"), self.secret)
        self.assertNotIn(self.secret.encode(), self.db_path.read_bytes())

    def test_scope_and_revocation_block_resolution(self) -> None:
        binding = self.service.create(
            self.identity, self.org.id, "Connector key", "example", f"env:{self.env_name}", ["read"], self.ws.id
        )
        with self.assertRaises(AuthorizationError):
            self.service.resolve_for_use(self.identity, binding["id"], "sync")
        self.service.revoke(self.identity, binding["id"])
        with self.assertRaises(AuthorizationError):
            self.service.resolve_for_use(self.identity, binding["id"], "read")

    def test_redactor_covers_nested_keys_bearers_and_known_values(self) -> None:
        value = {"headers": {"Authorization": "Bearer abc.def"}, "detail": self.secret, "safe": "ok"}
        redacted = redact(value, (self.secret,))
        self.assertEqual(redacted["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(redacted["detail"], "[REDACTED]")
        self.assertEqual(redacted["safe"], "ok")


if __name__ == "__main__":
    unittest.main()
