from __future__ import annotations

import json
import os as environment
import unittest
from unittest.mock import patch

from auremgrid.connectors.google_auth import ConnectorSourceEvent, RouteLifecycleMutation
from auremgrid.connectors.google_drive import DriveBackfillTask
from auremgrid.services.brain import CompanyOS
from auremgrid.services.integration_ops import GMAIL_READ_SCOPE, GOOGLE_DRIVE_READ_SCOPE
from tests.auth_support import issue_identity


class GoogleIntegrationWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency", "org_google_wiring")
        self.ws = self.os.create_organization_workspace(
            self.org.id, "Client", "client", "ws_google_wiring"
        )
        self.person = self.os.create_person(
            self.org.id, "Owner", "owner@wiring.test", role="owner",
            person_id="person_google_wiring",
        )
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")
        self.os.create_actor(self.ws.id, "Connector", "admin", "actor_google_wiring")
        _, self.identity = issue_identity(
            self.os, self.org.id, self.person.id, self.ws.id, "actor_google_wiring"
        )
        environment.environ["AUREMGRID_TEST_GOOGLE_WIRING"] = json.dumps({
            "client_id": "client", "client_secret": "secret", "refresh_token": "refresh"
        })

    def tearDown(self) -> None:
        environment.environ.pop("AUREMGRID_TEST_GOOGLE_WIRING", None)
        self.os.close()

    def _integration(self, mappings: dict[str, str]):
        integration = self.os.integrations.configure(
            self.identity, "gmail", "owner@wiring.test", mappings, [GMAIL_READ_SCOPE]
        )
        self.os.integrations.bind_credential(
            self.identity, integration["id"], "Google bundle",
            "env:AUREMGRID_TEST_GOOGLE_WIRING", ["connector:gmail", GMAIL_READ_SCOPE],
        )
        return integration

    @staticmethod
    def _identity_result():
        return {
            "account_id": "owner@wiring.test", "account_name": "Mailbox",
            "granted_permissions": [GMAIL_READ_SCOPE],
        }

    def test_exact_event_upsert_then_tombstone_updates_current_truth(self) -> None:
        integration = self._integration({"label:INBOX": self.ws.id})
        state = {"tombstone": False}

        def factory(mode, _source, _secret, *args):
            if mode == "refresh":
                return {"access_token": "access", "scopes": [GMAIL_READ_SCOPE]}
            if mode == "verify":
                return self._identity_result()
            route, workspace = args[0], args[1]
            operation = "tombstone" if state["tombstone"] else "upsert"
            dedupe = f"message-1:{operation}"
            event = ConnectorSourceEvent(
                dedupe, "message-1", operation, "gmail/messages/message-1",
                "https://mail.google.test/message-1", "message body",
                {"route_keys": [route], "workspace_ids": [workspace]},
            )
            mutation = RouteLifecycleMutation(
                "message-1", route, workspace, operation,
                "2" if state["tombstone"] else "1", dedupe,
            )
            return [event], '{"v":1,"phase":"history","checkpoint":"2","page_token":null}', False, {
                "lifecycle_mutations": (mutation,)
            }

        self.os.integrations.connector_factory = factory
        with patch(
            "auremgrid.services.integration_ops.LIVE_SOURCES",
            frozenset({"slack", "clickup", "gmail"}),
        ):
            self.os.integrations.verify(self.identity, integration["id"])
            self.os.integrations.sync(self.identity, integration["id"])
            current = self.os.integrations.get(self.identity, integration["id"])
            self.assertEqual(current["object_count"], 1)
            state["tombstone"] = True
            self.os.integrations.sync(self.identity, integration["id"])

        route = self.os.store.conn.execute(
            "SELECT status,active_source_id FROM provider_object_routes WHERE external_id='message-1'"
        ).fetchone()
        self.assertEqual((route["status"], route["active_source_id"]), ("retired", None))
        self.assertEqual(self.os.integrations.get(self.identity, integration["id"])["object_count"], 0)
        self.assertEqual(
            self.os.store.conn.execute(
                "SELECT COUNT(*) FROM sources WHERE source_key='gmail/messages/message-1'"
            ).fetchone()[0],
            1,
        )

    def test_two_gmail_labels_are_isolated_durable_streams(self) -> None:
        mappings = {"label:INBOX": self.ws.id, "label:Projects": self.ws.id}
        integration = self._integration(mappings)

        def factory(mode, _source, _secret, *args):
            if mode == "refresh":
                return {"access_token": "access", "scopes": [GMAIL_READ_SCOPE]}
            if mode == "verify":
                return self._identity_result()
            route, workspace = args[0], args[1]
            external_id = route.split(":", 1)[1].lower()
            dedupe = f"{external_id}:upsert"
            event = ConnectorSourceEvent(
                dedupe, external_id, "message_discovered", f"gmail/messages/{external_id}",
                f"https://mail.google.test/{external_id}", external_id,
                {"route_keys": [route], "workspace_ids": [workspace]},
            )
            mutation = RouteLifecycleMutation(external_id, route, workspace, "upsert", "1", dedupe)
            return [event], '{"v":1,"phase":"history","checkpoint":"2","page_token":null}', False, {
                "lifecycle_mutations": (mutation,)
            }

        self.os.integrations.connector_factory = factory
        with patch(
            "auremgrid.services.integration_ops.LIVE_SOURCES",
            frozenset({"slack", "clickup", "gmail"}),
        ):
            self.os.integrations.verify(self.identity, integration["id"])
            self.os.integrations.sync(self.identity, integration["id"])

        rows = self.os.store.conn.execute(
            "SELECT DISTINCT account_key,route_key,status FROM provider_object_routes ORDER BY route_key"
        ).fetchall()
        self.assertEqual([row["route_key"] for row in rows], ["label:INBOX", "label:Projects"])
        self.assertEqual(len({row["account_key"] for row in rows}), 2)
        self.assertTrue(all(row["status"] == "active" for row in rows))

    def test_drive_backfill_task_and_nested_ancestry_finish_before_cursor_promotion(self) -> None:
        integration = self.os.integrations.configure(
            self.identity, "google_drive", "permission-1",
            {"folder:root": self.ws.id}, [GOOGLE_DRIVE_READ_SCOPE],
        )
        self.os.integrations.bind_credential(
            self.identity, integration["id"], "Google bundle",
            "env:AUREMGRID_TEST_GOOGLE_WIRING",
            ["connector:google_drive", GOOGLE_DRIVE_READ_SCOPE],
        )
        pulls = {"count": 0}

        def factory(mode, _source, _secret, *args):
            if mode == "refresh":
                return {"access_token": "access", "scopes": [GOOGLE_DRIVE_READ_SCOPE]}
            if mode == "verify":
                return {
                    "account_id": "permission-1", "account_name": "Drive",
                    "granted_permissions": [GOOGLE_DRIVE_READ_SCOPE],
                }
            route, workspace, _cursor, runtime = args
            pulls["count"] += 1
            nested = pulls["count"] == 2
            external_id = "child-file" if nested else "child-folder"
            dedupe = f"{external_id}:1"
            event = ConnectorSourceEvent(
                dedupe, external_id, "file_discovered",
                f"google-drive/files/{external_id}", f"https://drive.test/{external_id}",
                external_id,
                {
                    "file": {
                        "id": external_id,
                        "parents": ["child-folder" if nested else "root"],
                        "mimeType": "text/plain" if nested else "application/vnd.google-apps.folder",
                    },
                    "route_keys": [route], "workspace_ids": [workspace], "phase": "backfill",
                },
            )
            mutation = RouteLifecycleMutation(external_id, route, workspace, "upsert", "1", dedupe)
            if not nested:
                self.assertIsNone(runtime.get("_runtime_backfill_task"))
                return [event], '{"v":1,"phase":"backfill","checkpoint":"cp"}', True, {
                    "lifecycle_mutations": (mutation,),
                    "backfill_tasks": (DriveBackfillTask(route, "child-folder"),),
                }
            self.assertEqual(runtime["_runtime_backfill_task"].container_id, "child-folder")
            return [event], '{"v":1,"phase":"changes","checkpoint":"cp","page_token":null}', False, {
                "lifecycle_mutations": (mutation,), "backfill_tasks": (),
            }

        self.os.integrations.connector_factory = factory
        with patch(
            "auremgrid.services.integration_ops.LIVE_SOURCES",
            frozenset({"slack", "clickup", "google_drive"}),
        ):
            self.os.integrations.verify(self.identity, integration["id"])
            result = self.os.integrations.sync(self.identity, integration["id"])

        self.assertEqual(pulls["count"], 2)
        self.assertFalse(result["backfill_remaining"])
        cursor = self.os.store.conn.execute(
            "SELECT cursor_value FROM connector_cursors WHERE connector='google_drive'"
        ).fetchone()["cursor_value"]
        self.assertEqual(json.loads(cursor)["phase"], "changes")
        ancestry = self.os.store.conn.execute(
            "SELECT root_route_keys FROM provider_object_ancestry WHERE external_id='child-file'"
        ).fetchone()
        self.assertEqual(json.loads(ancestry["root_route_keys"]), ["folder:root"])
        self.assertEqual(
            self.os.store.conn.execute(
                "SELECT COUNT(*) FROM provider_sync_tasks WHERE status!='completed'"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
