import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from auremgrid.cli import main
from auremgrid.domain.errors import AuthorizationError
from auremgrid.services.brain import CompanyOS


class SetupAgencyCliTests(unittest.TestCase):
    def test_setup_agency_creates_owner_workspace_binding_and_one_time_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "agency.sqlite"
            output = io.StringIO()
            with redirect_stdout(output):
                code = main([
                    "setup-agency",
                    "--agency", "Northwind Studio",
                    "--admin-name", "Nora Owner",
                    "--admin-email", "nora@northwind.test",
                    "--db", str(database),
                    "--dashboard-url", "http://127.0.0.1:8791/",
                ])

            self.assertEqual(code, 0)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["status"], "ready")
            self.assertEqual(receipt["agency"]["id"], "org_northwind_studio")
            self.assertEqual(receipt["workspace"]["id"], "ws_northwind_studio")
            self.assertEqual(receipt["owner"]["role"], "owner")
            self.assertTrue(receipt["session"]["shown_once"])
            self.assertEqual(receipt["dashboard_url"], "http://127.0.0.1:8791/")

            os = CompanyOS(database)
            try:
                identity = os.auth.authenticate_session(
                    receipt["session"]["token"], workspace_id="ws_northwind_studio"
                )
                self.assertEqual(identity.organization_id, "org_northwind_studio")
                self.assertEqual(identity.person_id, "person_nora")
                self.assertEqual(
                    os.auth.actor_for_identity(identity, "ws_northwind_studio"),
                    "act_ws_northwind_studio_admin",
                )
                # Session plaintext is returned in the receipt, never persisted in SQL.
                stored = os.store.conn.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
                self.assertNotEqual(stored, receipt["session"]["token"])
            finally:
                os.close()

    def test_setup_agency_refuses_to_reinitialize_an_existing_organization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "agency.sqlite"
            args = [
                "setup-agency", "--agency", "Northwind Studio",
                "--admin-name", "Nora Owner", "--admin-email", "nora@northwind.test",
                "--db", str(database),
            ]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(args)
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM organizations").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 1)
            finally:
                connection.close()

    def test_local_admin_invite_cli_consumes_once_and_revokes_session_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "agency.sqlite"
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([
                    "setup-agency",
                    "--agency", "Northwind Studio",
                    "--admin-name", "Nora Owner",
                    "--admin-email", "nora@northwind.test",
                    "--db", str(database),
                ]), 0)
            admin_token = json.loads(output.getvalue())["session"]["token"]
            os = CompanyOS(database)
            try:
                person = os.create_person("org_northwind_studio", "Ravi Recovery", "ravi@example.test", role="member", person_id="person_ravi")
                os.add_person_to_workspace("org_northwind_studio", "ws_northwind_studio", person.id, "viewer")
                os.create_actor("ws_northwind_studio", "Ravi actor", "operator", "actor_ravi")
            finally:
                os.close()

            invite_out = io.StringIO()
            with redirect_stdout(invite_out):
                self.assertEqual(main([
                    "auth-invite-create",
                    "--db", str(database),
                    "--admin-token", admin_token,
                    "--person", "person_ravi",
                    "--email", "ravi@example.test",
                    "--workspace", "ws_northwind_studio",
                    "--actor", "actor_ravi",
                ]), 0)
            invite = json.loads(invite_out.getvalue())
            self.assertTrue(invite["shown_once"])

            consume_out = io.StringIO()
            with redirect_stdout(consume_out):
                self.assertEqual(main([
                    "auth-invite-consume",
                    "--db", str(database),
                    "--admin-token", admin_token,
                    "--invite-token", invite["token"],
                ]), 0)
            consumed = json.loads(consume_out.getvalue())
            issued_token = consumed["session"]["token"]
            issued_id = consumed["session"]["id"]

            sessions_out = io.StringIO()
            with redirect_stdout(sessions_out):
                self.assertEqual(main([
                    "auth-sessions",
                    "--db", str(database),
                    "--admin-token", admin_token,
                ]), 0)
            sessions = json.loads(sessions_out.getvalue())["sessions"]
            self.assertTrue(any(item["id"] == issued_id and "token_hash" not in item for item in sessions))

            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([
                    "auth-session-revoke",
                    "--db", str(database),
                    "--admin-token", admin_token,
                    "--session-id", issued_id,
                ]), 0)
            os = CompanyOS(database)
            try:
                with self.assertRaises(AuthorizationError):
                    os.auth.authenticate_session(issued_token)
            finally:
                os.close()


if __name__ == "__main__":
    unittest.main()
