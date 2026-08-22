from __future__ import annotations

import sqlite3
import tempfile
import json
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.api.http import serve
from auremgrid.domain.errors import ValidationError
from auremgrid.domain.models import AgentLevel, LEVEL_DEFINITIONS
from auremgrid.services.brain import CompanyOS
from auremgrid.storage.migrations import MIGRATIONS
from auremgrid.storage.sqlite import SCHEMA
from tests.auth_support import LATEST_SCHEMA_VERSION, issue_identity


class AgentLevelRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.owner.id, "admin")
        self.agents = self.os.agent_ops.seed_primary_agents(self.org.id, self.owner.id)
        for agent in self.agents:
            self.os.agent_ops.configure_agent(
                self.org.id,
                self.owner.id,
                agent["id"],
                "local-provider-slot",
                ["work.list"],
                [self.ws.id],
                [],
            )

    def tearDown(self) -> None:
        self.os.close()

    def agent(self, name: str) -> dict[str, object]:
        return next(agent for agent in self.os.agent_ops.command_center(self.org.id, self.owner.id)["agents"] if agent["name"] == name)

    def test_level_definitions_are_source_neutral(self) -> None:
        payload = LEVEL_DEFINITIONS[AgentLevel.L2_BUILD].to_dict()
        self.assertNotIn("models", payload)
        self.assertEqual(payload["level"], "L2")
        self.assertIn("verify", payload["capability_tags"])

    def test_seed_primary_agents_have_levels_and_capabilities(self) -> None:
        levels = {agent["name"]: agent["level"] for agent in self.os.agent_ops.command_center(self.org.id, self.owner.id)["agents"]}
        self.assertEqual(levels, {"Luna": "L1", "Sol": "L3", "Terra": "L2"})

    def test_cheapest_eligible_resolution_validates_tags(self) -> None:
        self.assertEqual(self.os.agent_ops.resolve_level(["format"]), AgentLevel.L0_EXECUTE)
        self.assertEqual(self.os.agent_ops.resolve_level(["format", "communicate"]), AgentLevel.L1_OPERATE)
        self.assertEqual(self.os.agent_ops.resolve_level(["execute", "verify"]), AgentLevel.L2_BUILD)
        self.assertEqual(self.os.agent_ops.resolve_level(["synthesize"]), AgentLevel.L3_REASON)
        with self.assertRaises(ValidationError):
            self.os.agent_ops.resolve_level(["telepathy"])
        with self.assertRaises(ValidationError):
            self.os.agent_ops.resolve_level([])

    def test_task_creation_persists_recommended_selected_level(self) -> None:
        terra = self.agent("Terra")
        task = self.os.agent_ops.enqueue_task(
            self.org.id,
            self.owner.id,
            str(terra["id"]),
            "Build check",
            "Implement and verify a workflow path",
            self.ws.id,
            intent_tags=["build", "verify"],
        )
        self.assertEqual(task["recommended_level"], "L2")
        self.assertEqual(task["selected_level"], "L2")
        self.assertEqual(task["level_override_reason"], None)
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM agent_level_overrides").fetchone()[0],
            0,
        )

    def test_escalation_override_is_audited(self) -> None:
        sol = self.agent("Sol")
        task = self.os.agent_ops.enqueue_task(
            self.org.id,
            self.owner.id,
            str(sol["id"]),
            "Risk pass",
            "Review a risky implementation",
            self.ws.id,
            intent_tags=["review"],
            selected_level="L3",
            override_reason="Strategic risk review",
        )
        self.assertEqual(task["recommended_level"], "L2")
        self.assertEqual(task["selected_level"], "L3")
        override = self.os.store.conn.execute("SELECT * FROM agent_level_overrides WHERE task_id=?", (task["id"],)).fetchone()
        self.assertIsNotNone(override)
        self.assertEqual(override["recommended_level"], "L2")
        self.assertEqual(override["selected_level"], "L3")

    def test_level_override_audit_is_immutable(self) -> None:
        sol = self.agent("Sol")
        task = self.os.agent_ops.enqueue_task(
            self.org.id,
            self.owner.id,
            str(sol["id"]),
            "Risk pass",
            "Review a risky implementation",
            self.ws.id,
            intent_tags=["review"],
            selected_level="L3",
            override_reason="Strategic risk review",
        )
        override = self.os.store.conn.execute("SELECT * FROM agent_level_overrides WHERE task_id=?", (task["id"],)).fetchone()
        self.assertIsNotNone(override)
        with self.assertRaises(sqlite3.IntegrityError):
            self.os.store.conn.execute("UPDATE agent_level_overrides SET reason='changed' WHERE id=?", (override["id"],))
        self.os.store.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.os.store.conn.execute("DELETE FROM agent_level_overrides WHERE id=?", (override["id"],))
        self.os.store.conn.rollback()

    def test_de_escalation_and_underleveled_agent_are_rejected(self) -> None:
        luna = self.agent("Luna")
        with self.assertRaises(ValidationError):
            self.os.agent_ops.enqueue_task(
                self.org.id,
                self.owner.id,
                str(luna["id"]),
                "Implement",
                "Implement a production change",
                self.ws.id,
                intent_tags=["implement"],
            )
        sol = self.agent("Sol")
        with self.assertRaises(ValidationError):
            self.os.agent_ops.enqueue_task(
                self.org.id,
                self.owner.id,
                str(sol["id"]),
                "Strategy",
                "Decide a high-risk strategy",
                self.ws.id,
                intent_tags=["strategize"],
                selected_level="L2",
                override_reason="Try to save cost",
            )

    def test_migration19_replay_preserves_existing_agents_and_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent-level-replay.sqlite"
            first = CompanyOS(path)
            try:
                org = first.create_organization("Replay Agency")
                ws = first.create_organization_workspace(org.id, "Replay Client", "client")
                owner = first.create_person(org.id, "Owner", role="owner")
                first.add_person_to_workspace(org.id, ws.id, owner.id, "admin")
                agents = first.agent_ops.seed_primary_agents(org.id, owner.id)
                terra = next(agent for agent in agents if agent["name"] == "Terra")
                first.agent_ops.configure_agent(
                    org.id,
                    owner.id,
                    str(terra["id"]),
                    "local-provider-slot",
                    ["work.list"],
                    [ws.id],
                    [],
                )
                task = first.agent_ops.enqueue_task(
                    org.id,
                    owner.id,
                    str(terra["id"]),
                    "Replay-safe implementation",
                    "Build and verify the migration replay path",
                    ws.id,
                    intent_tags=["build", "verify"],
                )
            finally:
                first.close()

            conn = sqlite3.connect(path)
            try:
                conn.execute("DELETE FROM schema_migrations WHERE version=19")
                conn.commit()
            finally:
                conn.close()

            second = CompanyOS(path)
            try:
                agent_columns = {
                    row["name"] for row in second.store.conn.execute("PRAGMA table_info(agents)").fetchall()
                }
                task_columns = {
                    row["name"] for row in second.store.conn.execute("PRAGMA table_info(agent_tasks)").fetchall()
                }
                self.assertEqual(second.store.schema_version, LATEST_SCHEMA_VERSION)
                self.assertTrue({"level", "capability_tags"}.issubset(agent_columns))
                self.assertTrue(
                    {
                        "intent_tags",
                        "recommended_level",
                        "selected_level",
                        "level_override_reason",
                    }.issubset(task_columns)
                )
                agent_row = second.store.conn.execute(
                    "SELECT level,capability_tags FROM agents WHERE id=?",
                    (terra["id"],),
                ).fetchone()
                task_row = second.store.conn.execute(
                    "SELECT recommended_level,selected_level,intent_tags FROM agent_tasks WHERE id=?",
                    (task["id"],),
                ).fetchone()
                self.assertIsNotNone(agent_row)
                self.assertIsNotNone(task_row)
                self.assertEqual(agent_row["level"], "L2")
                self.assertEqual(task_row["recommended_level"], "L2")
                self.assertEqual(task_row["selected_level"], "L2")
            finally:
                second.close()

    def test_migration19_upgrades_true_schema18_seeded_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pre19.sqlite"
            _create_schema18_seeded_agent_database(path)

            upgraded = CompanyOS(path)
            try:
                self.assertEqual(upgraded.store.schema_version, LATEST_SCHEMA_VERSION)
                rows = upgraded.store.conn.execute(
                    """SELECT a.id,a.name,a.level,a.capability_tags,r.name role_name
                    FROM agents a JOIN agent_roles r ON r.id=a.role_id
                    WHERE a.organization_id='org_pre18' ORDER BY a.name"""
                ).fetchall()
                by_name = {row["name"]: dict(row) for row in rows}
                self.assertEqual({name: row["id"] for name, row in by_name.items()}, {
                    "Luna": "agent_luna_pre18",
                    "Sol": "agent_sol_pre18",
                    "Terra": "agent_terra_pre18",
                })
                self.assertEqual({name: row["level"] for name, row in by_name.items()}, {
                    "Luna": "L1",
                    "Sol": "L3",
                    "Terra": "L2",
                })
                self.assertIn("strategize", json.loads(by_name["Sol"]["capability_tags"]))
                self.assertIn("verify", json.loads(by_name["Terra"]["capability_tags"]))
                self.assertIn("communicate", json.loads(by_name["Luna"]["capability_tags"]))
                task_row = upgraded.store.conn.execute(
                    "SELECT title,instructions,agent_id FROM agent_tasks WHERE id='task_terra_pre18'"
                ).fetchone()
                self.assertEqual(task_row["title"], "Existing implementation")
                self.assertEqual(task_row["instructions"], "Preserve this old task")
                self.assertEqual(task_row["agent_id"], "agent_terra_pre18")

                reseeded = upgraded.agent_ops.seed_primary_agents("org_pre18", "person_owner_pre18")
                self.assertEqual(len(reseeded), 3)
                self.assertEqual(
                    upgraded.store.conn.execute(
                        "SELECT COUNT(*) FROM agents WHERE organization_id='org_pre18'"
                    ).fetchone()[0],
                    3,
                )
            finally:
                upgraded.close()

    def test_rest_agent_task_accepts_routing_fields_and_audits_override(self) -> None:
        token, _identity = issue_identity(self.os, self.org.id, self.owner.id, self.ws.id)
        server = serve(self.os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            sol = self.agent("Sol")
            status, body = _post_json(
                server.server_address,
                token,
                "/agents/tasks",
                {
                    "organization_id": self.org.id,
                    "person_id": self.owner.id,
                    "agent_id": sol["id"],
                    "title": "API risk pass",
                    "instructions": "Review risky implementation",
                    "workspace_id": self.ws.id,
                    "intent_tags": ["review"],
                    "selected_level": "L3",
                    "override_reason": "Strategic risk review",
                },
            )
            self.assertEqual(status, 201)
            self.assertEqual(body["recommended_level"], "L2")
            self.assertEqual(body["selected_level"], "L3")
            self.assertEqual(body["level_override_reason"], "Strategic risk review")
            override = self.os.store.conn.execute(
                "SELECT selected_level,reason FROM agent_level_overrides WHERE task_id=?",
                (body["id"],),
            ).fetchone()
            self.assertEqual(override["selected_level"], "L3")
        finally:
            server.shutdown()
            server.server_close()

    def test_rest_agent_task_rejects_invalid_level_selection(self) -> None:
        token, _identity = issue_identity(self.os, self.org.id, self.owner.id, self.ws.id)
        server = serve(self.os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            sol = self.agent("Sol")
            status, body = _post_json(
                server.server_address,
                token,
                "/agents/tasks",
                {
                    "organization_id": self.org.id,
                    "person_id": self.owner.id,
                    "agent_id": sol["id"],
                    "title": "Bad strategy",
                    "instructions": "Decide a high-risk strategy",
                    "workspace_id": self.ws.id,
                    "intent_tags": ["strategize"],
                    "selected_level": "L2",
                    "override_reason": "Try to save cost",
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"], "validation_error")
        finally:
            server.shutdown()
            server.server_close()


def _create_schema18_seeded_agent_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO schema_migrations(version, name) VALUES (1, 'v0_1_kernel')")
        for migration in MIGRATIONS:
            if migration.version >= 19:
                continue
            conn.executescript(migration.sql)
            conn.execute("INSERT INTO schema_migrations(version, name) VALUES (?, ?)", (migration.version, migration.name))
        now = "2026-01-01T00:00:00+00:00"
        conn.execute("INSERT INTO organizations(id,name,created_at) VALUES (?,?,?)", ("org_pre18", "Pre18 Agency", now))
        conn.execute("INSERT INTO workspaces(id,name,created_at) VALUES (?,?,?)", ("ws_pre18", "Pre18 Client", now))
        conn.execute("INSERT INTO workspace_organization(workspace_id,organization_id,kind) VALUES (?,?,?)", ("ws_pre18", "org_pre18", "client"))
        conn.execute(
            """INSERT INTO people(
                id,organization_id,name,email,title,department,manager_id,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("person_owner_pre18", "org_pre18", "Owner", None, None, None, None, "active", now, now),
        )
        conn.execute(
            "INSERT INTO organization_memberships(id,organization_id,person_id,role,created_at) VALUES (?,?,?,?,?)",
            ("membership_owner_pre18", "org_pre18", "person_owner_pre18", "owner", now),
        )
        conn.execute(
            "INSERT INTO workspace_memberships(id,workspace_id,person_id,role,created_at) VALUES (?,?,?,?,?)",
            ("workspace_membership_owner_pre18", "ws_pre18", "person_owner_pre18", "admin", now),
        )
        roles = (
            ("role_sol_pre18", "advisor_reviewer", "Review architecture and detect risk", "[]"),
            ("role_terra_pre18", "builder", "Implement deep product work", '["domain.write","code.write"]'),
            ("role_luna_pre18", "executor", "Execute operations and consistency work", '["domain.write"]'),
        )
        for role_id, name, description, writes in roles:
            conn.execute(
                "INSERT INTO agent_roles(id,organization_id,name,description,default_tools,default_write_permissions) VALUES (?,?,?,?,?,?)",
                (role_id, "org_pre18", name, description, "[]", writes),
            )
        agents = (
            ("agent_sol_pre18", "Sol", "role_sol_pre18", "[]"),
            ("agent_terra_pre18", "Terra", "role_terra_pre18", '["domain.write","code.write"]'),
            ("agent_luna_pre18", "Luna", "role_luna_pre18", '["domain.write"]'),
        )
        for agent_id, name, role_id, writes in agents:
            conn.execute(
                """INSERT INTO agents(
                    id,organization_id,name,role_id,model,tools,allowed_workspace_ids,memory_access,
                    write_permissions,status,current_task_id,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (agent_id, "org_pre18", name, role_id, "unconfigured", "[]", '["ws_pre18"]', "proposal_only", writes, "idle", None, now),
            )
        conn.execute(
            """INSERT INTO agent_tasks(
                id,organization_id,workspace_id,agent_id,title,instructions,priority,status,
                approval_request_id,created_at,started_at,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "task_terra_pre18", "org_pre18", "ws_pre18", "agent_terra_pre18",
                "Existing implementation", "Preserve this old task", 25, "queued", None, now, None, None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _post_json(address: tuple[str, int], token: str, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    connection = HTTPConnection(address[0], address[1], timeout=5)
    connection.request(
        "POST",
        path,
        json.dumps(payload),
        {"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    response = connection.getresponse()
    body = json.loads(response.read())
    connection.close()
    return response.status, body


if __name__ == "__main__":
    unittest.main()
