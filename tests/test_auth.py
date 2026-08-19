from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError
from auremgrid.domain.security import role_capabilities
from auremgrid.services.auth import AuthService, hash_token
from auremgrid.services.brain import CompanyOS


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE people(id TEXT PRIMARY KEY, organization_id TEXT, email TEXT, status TEXT);
            CREATE TABLE organization_memberships(id TEXT PRIMARY KEY, organization_id TEXT, person_id TEXT, role TEXT);
            CREATE TABLE workspace_organization(workspace_id TEXT PRIMARY KEY, organization_id TEXT);
            CREATE TABLE workspace_memberships(id TEXT PRIMARY KEY, workspace_id TEXT, person_id TEXT, role TEXT);
            CREATE TABLE auth_principals(id TEXT PRIMARY KEY, organization_id TEXT, person_id TEXT, email TEXT, status TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE auth_sessions(id TEXT PRIMARY KEY, principal_id TEXT, token_hash TEXT UNIQUE, created_at TEXT, expires_at TEXT, revoked_at TEXT, last_seen_at TEXT);
            CREATE TABLE api_tokens(id TEXT PRIMARY KEY, principal_id TEXT, name TEXT, token_hash TEXT UNIQUE, scopes TEXT, created_at TEXT, expires_at TEXT, revoked_at TEXT, last_used_at TEXT);
            """
        )
        self.conn.executemany("INSERT INTO people VALUES (?,?,?,?)", [("p1", "o1", "one@example.test", "active"), ("p2", "o2", "two@example.test", "active")])
        self.conn.executemany("INSERT INTO organization_memberships VALUES (?,?,?,?)", [("m1", "o1", "p1", "member"), ("m2", "o2", "p2", "owner")])
        self.conn.execute("INSERT INTO workspace_organization VALUES (?,?)", ("w1", "o1"))
        self.conn.execute("INSERT INTO workspace_memberships VALUES (?,?,?,?)", ("wm1", "w1", "p1", "viewer"))
        self.conn.commit()
        counter = {"n": 0}
        def new_id(prefix: str) -> str:
            counter["n"] += 1
            return f"{prefix}_{counter['n']}"
        self.auth = AuthService(self.conn, new_id=new_id)
        self.principal = self.auth.create_principal("o1", "p1", "one@example.test")

    def tearDown(self) -> None:
        self.conn.close()

    def test_hash_only_storage_and_session_authentication(self) -> None:
        session = self.auth.create_session(self.principal["id"])
        row = self.conn.execute("SELECT token_hash FROM auth_sessions").fetchone()
        self.assertNotEqual(row[0], session["token"])
        self.assertEqual(row[0], hash_token(session["token"]))
        identity = self.auth.authenticate_session(session["token"])
        self.assertEqual(identity.person_id, "p1")

    def test_expiry_and_revoke(self) -> None:
        expired = self.auth.create_session(self.principal["id"])
        self.conn.execute("UPDATE auth_sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (expired["id"],))
        self.conn.commit()
        with self.assertRaises(AuthorizationError):
            self.auth.authenticate_session(expired["token"])
        live = self.auth.create_session(self.principal["id"])
        self.auth.revoke_session(live["token"])
        with self.assertRaises(AuthorizationError):
            self.auth.authenticate_session(live["token"])

    def test_disabled_principal_and_cross_org_denial(self) -> None:
        session = self.auth.create_session(self.principal["id"])
        self.conn.execute("UPDATE auth_principals SET status='disabled' WHERE id=?", (self.principal["id"],))
        self.conn.commit()
        with self.assertRaises(AuthorizationError):
            self.auth.authenticate_session(session["token"])
        self.conn.execute("UPDATE auth_principals SET status='active' WHERE id=?", (self.principal["id"],))
        self.conn.commit()
        with self.assertRaises(AuthorizationError):
            self.auth.authenticate_session(session["token"], organization_id="o2")

    def test_api_scopes_intersect_role_and_viewer_cannot_write(self) -> None:
        token = self.auth.create_api_token(self.principal["id"], "limited", ["workspace_read", "workspace_write"])
        identity = self.auth.authenticate_api_token(token["token"], workspace_id="w1")
        self.assertTrue(identity.can("workspace_read"))
        self.assertFalse(identity.can("workspace_write"))
        with self.assertRaises(AuthorizationError):
            self.auth.authorize(identity, "workspace_write", "o1", "w1")

    def test_role_capability_policy_and_rotation(self) -> None:
        self.assertIn("auth_manage", role_capabilities("owner"))
        self.assertNotIn("auth_manage", role_capabilities("member"))
        old = self.auth.create_session(self.principal["id"])
        new = self.auth.rotate_session(old["token"])
        self.assertNotEqual(old["token"], new["token"])
        with self.assertRaises(AuthorizationError):
            self.auth.authenticate_session(old["token"])
        self.assertEqual(self.auth.authenticate_session(new["token"]).person_id, "p1")


class AuthFileStorageTests(unittest.TestCase):
    def test_schema_11_upgrade_adds_credential_generation_in_schema_12(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema-11.sqlite"
            original = CompanyOS(path)
            original.close()
            connection = sqlite3.connect(path)
            connection.execute("ALTER TABLE secret_bindings DROP COLUMN generation")
            for column in (
                "expected_account_id", "provider_account_id", "provider_account_name",
                "granted_permissions", "credential_verified_at",
            ):
                connection.execute(f"ALTER TABLE integrations DROP COLUMN {column}")
            connection.execute("DELETE FROM schema_migrations WHERE version=12")
            connection.commit()
            connection.close()

            upgraded = CompanyOS(path)
            try:
                columns = {
                    row[1] for row in upgraded.store.conn.execute(
                        "PRAGMA table_info(secret_bindings)"
                    ).fetchall()
                }
                self.assertEqual(upgraded.store.schema_version, 12)
                self.assertIn("generation", columns)
            finally:
                upgraded.close()

    def test_plaintext_session_and_api_tokens_are_absent_from_database_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.sqlite"
            os = CompanyOS(path)
            org = os.create_organization("Auremgrid")
            person = os.create_person(org.id, "Owner", "owner@auth.test", role="owner")
            principal = os.auth.create_principal(org.id, person.id, "owner@auth.test")
            session = os.auth.create_session(principal["id"])
            api_token = os.auth.create_api_token(principal["id"], "service", ["workspace_read"])
            os.close()
            raw = path.read_bytes()
            self.assertNotIn(session["token"].encode(), raw)
            self.assertNotIn(api_token["token"].encode(), raw)


if __name__ == "__main__":
    unittest.main()
