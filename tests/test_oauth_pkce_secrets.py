from __future__ import annotations

import unittest
from datetime import timedelta

from auremgrid.domain.errors import ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS, new_id
from auremgrid.services.integration_security import EncryptedSecretVault, OAuthPKCEService


class OAuthPkceSecretsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Secure")
        self.identity = AuthenticatedIdentity("p", self.org.id, "person", "session", frozenset({"integration_configure"}))

    def tearDown(self) -> None:
        self.os.close()

    def test_encrypted_vault_never_stores_plaintext_and_requires_key(self) -> None:
        with self.assertRaises(ValidationError): EncryptedSecretVault(self.os.store.conn, new_id, "short")
        vault = EncryptedSecretVault(self.os.store.conn, new_id, "deployment-key-123456")
        vault.put(self.org.id, None, "webhook", "do-not-persist", "env:WEBHOOK_SECRET")
        row = self.os.store.conn.execute("SELECT ciphertext,reference FROM local_secret_vault").fetchone()
        self.assertNotIn("do-not-persist", row["ciphertext"]); self.assertEqual(row["reference"], "env:WEBHOOK_SECRET")
        self.assertEqual(vault.resolve(self.org.id, None, "webhook"), "do-not-persist")

    def test_oauth_expiry_replay_redirect_and_pkce(self) -> None:
        oauth = OAuthPKCEService(self.os.store.conn, new_id, {"google": {"https://app.test/callback"}}, ttl_seconds=10)
        state = oauth.begin(self.identity, self.org.id, None, "google", "client", "https://app.test/callback", "openid")
        result = oauth.consume(state["state"], state["code_verifier"], "https://app.test/callback", "google")
        self.assertEqual(result["client_id"], "client")
        with self.assertRaises(ValidationError): oauth.consume(state["state"], state["code_verifier"], "https://app.test/callback", "google")
        expired = oauth.begin(self.identity, self.org.id, None, "google", "client", "https://app.test/callback", "openid")
        self.os.store.conn.execute("UPDATE oauth_states SET expires_at='2000-01-01T00:00:00+00:00' WHERE state_digest=?", (__import__('hashlib').sha256(expired["state"].encode()).hexdigest(),)); self.os.store.conn.commit()
        with self.assertRaises(ValidationError): oauth.consume(expired["state"], expired["code_verifier"], "https://app.test/callback", "google")
        bad = oauth.begin(self.identity, self.org.id, None, "google", "client", "https://app.test/callback", "openid")
        with self.assertRaises(ValidationError): oauth.consume(bad["state"], "wrong", "https://app.test/callback", "google")


if __name__ == "__main__": unittest.main()
