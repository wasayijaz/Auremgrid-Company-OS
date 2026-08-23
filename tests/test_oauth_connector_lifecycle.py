from __future__ import annotations

import json
import os as environment
import threading
import unittest
from http.client import HTTPConnection
from urllib.parse import urlencode

from auremgrid.api.http import serve
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS, new_id
from auremgrid.services.integration_security import EncryptedSecretVault, OAuthConnectorService
from tests.auth_support import issue_identity


class OAuthConnectorLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("OAuth")
        self.identity = AuthenticatedIdentity(
            "person", self.org.id, "person", "session",
            frozenset({"integration_configure", "integration_sync"}),
        )
        self.vault = EncryptedSecretVault(self.os.store.conn, new_id, "deployment-key-123456")

    def tearDown(self) -> None:
        self.os.close()

    def test_google_install_exchange_health_and_revoke(self) -> None:
        service = OAuthConnectorService(
            self.os.store.conn, new_id, self.vault,
            {"google": {"https://app.test/callback"}},
            exchange_transport=lambda **kwargs: {
                "access_token": "access-secret", "refresh_token": "refresh-secret",
                "account_id": "acct-1", "email": "owner@example.test",
                "scope": "openid drive.readonly", "expires_in": 3600,
            },
        )
        started = service.begin(self.identity, self.org.id, None, "google", "client", "https://app.test/callback", "openid")
        installed = service.complete(started["state"], "auth-code", started["code_verifier"], "https://app.test/callback", "google")
        self.assertEqual(installed["status"], "active")
        row = self.os.store.conn.execute("SELECT ciphertext FROM local_secret_vault").fetchone()
        self.assertNotIn("access-secret", row["ciphertext"])
        health = service.health(self.identity, installed["installation_id"])
        self.assertTrue(health["healthy"])
        revoked = service.revoke(self.identity, installed["installation_id"])
        self.assertEqual(revoked["status"], "revoked")
        self.assertFalse(service.health(self.identity, installed["installation_id"])["healthy"])


class OAuthHttpCallbackSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        environment.environ.pop("AUREMGRID_DEPLOYMENT_KEY", None)
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("OAuth HTTP", "org_oauth_http")
        self.os.create_person("org_oauth_http", "Owner", "owner@oauth.test", role="owner", person_id="person_oauth_owner")
        self.token, _ = issue_identity(self.os, "org_oauth_http", "person_oauth_owner")
        self.server = serve(self.os, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        environment.environ.pop("AUREMGRID_DEPLOYMENT_KEY", None)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.os.close()

    def request(
        self, method: str, path: str, token: str | None = None, payload: dict | None = None
    ) -> tuple[int, dict]:
        connection = HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def install_service(self, exchange_transport=None) -> list[dict]:
        calls: list[dict] = []

        def exchange(**kwargs):
            calls.append(kwargs)
            if exchange_transport is not None:
                return exchange_transport(**kwargs)
            return {
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "account_id": "acct-1",
                "email": "owner@example.test",
                "scope": "openid drive.readonly",
                "expires_in": 3600,
            }

        self.os._oauth_service = OAuthConnectorService(
            self.os.store.conn,
            new_id,
            None,
            {"google": {"https://app.test/callback"}},
            exchange_transport=exchange,
        )
        return calls

    def begin(self) -> dict:
        status, body = self.request(
            "POST",
            "/oauth/begin",
            self.token,
            {
                "organization_id": self.org.id,
                "provider": "google",
                "client_id": "client",
                "redirect_uri": "https://app.test/callback",
                "scope": "openid",
            },
        )
        self.assertEqual(status, 200, body)
        self.assertNotIn("code_verifier", body)
        return body

    def callback_path(self, started: dict, **overrides: str) -> str:
        params = {
            "state": started["state"],
            "code": "auth-code",
            "redirect_uri": "https://app.test/callback",
            "provider": "google",
        }
        params.update(overrides)
        return "/oauth/callback?" + urlencode(params)

    def test_http_callback_uses_hidden_verifier_and_rejects_replay(self) -> None:
        environment.environ["AUREMGRID_DEPLOYMENT_KEY"] = "deployment-key-123456"
        calls = self.install_service()
        started = self.begin()
        status, installed = self.request("GET", self.callback_path(started))
        self.assertEqual(status, 200, installed)
        self.assertEqual(installed["status"], "active")
        self.assertNotIn("access-secret", json.dumps(installed))
        self.assertNotIn("refresh-secret", json.dumps(installed))
        self.assertEqual(calls[0]["code"], "auth-code")
        self.assertTrue(calls[0]["code_verifier"])
        status, replay = self.request("GET", self.callback_path(started))
        self.assertEqual(status, 400, replay)

    def test_missing_deployment_key_fails_closed_after_callback(self) -> None:
        self.install_service()
        started = self.begin()
        status, body = self.request("GET", self.callback_path(started))
        self.assertEqual(status, 400, body)
        self.assertIn("deployment key", body["message"])
        rows = self.os.store.conn.execute("SELECT * FROM provider_installations").fetchall()
        self.assertEqual(rows, [])

    def test_exchange_failure_is_redacted_and_does_not_install(self) -> None:
        environment.environ["AUREMGRID_DEPLOYMENT_KEY"] = "deployment-key-123456"

        def failing_exchange(**_: object) -> dict:
            raise RuntimeError("secret host detail")

        self.install_service(failing_exchange)
        started = self.begin()
        status, body = self.request("GET", self.callback_path(started))
        self.assertEqual(status, 400, body)
        self.assertEqual(body["message"], "OAuth provider transport failed")
        self.assertNotIn("secret host detail", json.dumps(body))
        rows = self.os.store.conn.execute("SELECT * FROM provider_installations").fetchall()
        self.assertEqual(rows, [])

    def test_wrong_redirect_or_provider_is_rejected(self) -> None:
        environment.environ["AUREMGRID_DEPLOYMENT_KEY"] = "deployment-key-123456"
        self.install_service()
        started = self.begin()
        status, body = self.request("GET", self.callback_path(started, redirect_uri="https://evil.test/callback"))
        self.assertEqual(status, 400, body)
        status, body = self.request("GET", self.callback_path(started, provider="slack"))
        self.assertEqual(status, 400, body)

    def test_callback_public_contract_never_accepts_verifier(self) -> None:
        environment.environ["AUREMGRID_DEPLOYMENT_KEY"] = "deployment-key-123456"
        self.install_service()
        started = self.begin()
        status, body = self.request("GET", self.callback_path(started, code_verifier="leaked"))
        self.assertEqual(status, 400, body)
        self.assertIn("code_verifier", body["message"])
        status, body = self.request(
            "POST",
            "/oauth/callback",
            payload={
                "state": started["state"],
                "code": "auth-code",
                "redirect_uri": "https://app.test/callback",
                "provider": "google",
                "code_verifier": "leaked",
            },
        )
        self.assertEqual(status, 400, body)
        self.assertIn("code_verifier", body["message"])


if __name__ == "__main__":
    unittest.main()
