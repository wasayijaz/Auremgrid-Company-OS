from __future__ import annotations

import json
import os as environment
import tempfile
import unittest
from unittest.mock import patch

from auremgrid.connectors.google_auth import ConnectorSourceEvent, HttpResponse, RouteLifecycleMutation
from auremgrid.connectors.http import ConnectorTransportError
from auremgrid.connectors.google_drive import DriveBackfillTask, DriveReconciliationRequest, GoogleDriveConnector
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

    def test_drive_backfill_survives_process_close_before_drain_and_promotes_gap_free_cursor(self) -> None:
        """A process crash after durable page state must resume the same wave.

        The first worker records the historical page, event, generation baseline,
        and child task, then crashes before claiming the inbox event.  A fresh
        CompanyOS process reopens the same file, drains the task and pending
        event, and only then promotes the changes cursor.  This is deliberately
        on-disk so SQLite durability and coordinator restart semantics are both
        exercised.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/google-restart.sqlite"
            first = CompanyOS(db_path)
            org = first.create_organization("Agency", "org_google_restart")
            ws = first.create_organization_workspace(org.id, "Client", "client", "ws_google_restart")
            person = first.create_person(
                org.id, "Owner", "owner@restart.test", role="owner", person_id="person_google_restart"
            )
            first.add_person_to_workspace(org.id, ws.id, person.id, "admin")
            first.create_actor(ws.id, "Connector", "admin", "actor_google_restart")
            _, identity = issue_identity(first, org.id, person.id, ws.id, "actor_google_restart")
            integration = first.integrations.configure(
                identity, "google_drive", "permission-restart",
                {"folder:root": ws.id}, [GOOGLE_DRIVE_READ_SCOPE],
            )
            first.integrations.bind_credential(
                identity, integration["id"], "Google bundle", "env:AUREMGRID_TEST_GOOGLE_WIRING",
                ["connector:google_drive", GOOGLE_DRIVE_READ_SCOPE],
            )
            calls: list[tuple[str | None, str | None]] = []

            def factory(mode, _source, _secret, *args):
                if mode == "refresh":
                    return {"access_token": "access", "scopes": [GOOGLE_DRIVE_READ_SCOPE]}
                if mode == "verify":
                    return {
                        "account_id": "permission-restart", "account_name": "Drive",
                        "granted_permissions": [GOOGLE_DRIVE_READ_SCOPE],
                    }
                route, workspace, cursor, runtime = args
                task_type = runtime.get("_runtime_provider_task_type")
                calls.append((task_type, cursor))
                if task_type == "backfill":
                    external_id = "restart-child"
                    event = ConnectorSourceEvent(
                        "restart-child:v1", external_id, "file_discovered",
                        f"google-drive/files/{external_id}", f"https://drive.test/{external_id}",
                        "restart child",
                        {"file": {"id": external_id, "parents": ["root"], "mimeType": "text/plain"},
                         "route_keys": [route], "workspace_ids": [workspace], "phase": "backfill"},
                    )
                    mutation = RouteLifecycleMutation(
                        external_id, route, workspace, "upsert", "v1", "restart-child:v1"
                    )
                    return [event], '{"v":1,"phase":"changes","checkpoint":"after-baseline","page_token":null}', False, {
                        "lifecycle_mutations": (mutation,), "backfill_tasks": (),
                    }
                external_id = "restart-root"
                event = ConnectorSourceEvent(
                    "restart-root:v1", external_id, "file_discovered",
                    f"google-drive/files/{external_id}", f"https://drive.test/{external_id}",
                    "restart root",
                    {"file": {"id": external_id, "parents": ["root"], "mimeType": "application/vnd.google-apps.folder"},
                     "route_keys": [route], "workspace_ids": [workspace], "phase": "backfill"},
                )
                mutation = RouteLifecycleMutation(
                    external_id, route, workspace, "upsert", "v1", "restart-root:v1"
                )
                return [event], '{"v":1,"phase":"backfill","checkpoint":"baseline"}', True, {
                    "lifecycle_mutations": (mutation,),
                    "backfill_tasks": (DriveBackfillTask(route, "restart-child"),),
                }

            first.integrations.connector_factory = factory
            with patch(
                "auremgrid.services.integration_ops.LIVE_SOURCES",
                frozenset({"slack", "clickup", "google_drive"}),
            ):
                first.integrations.verify(identity, integration["id"])
                # Simulate an abrupt process stop immediately after the page,
                # task, and event have committed but before inbox drain.
                with patch.object(first.integrations.inbox, "claim_event", side_effect=RuntimeError("process stopped")):
                    with self.assertRaises(RuntimeError):
                        first.integrations.sync(identity, integration["id"])

            account_key = first.integrations._mapping_hash("google_drive", "folder:root", ws.id)
            task_state = first.store.conn.execute(
                "SELECT status FROM provider_sync_tasks"
            ).fetchall()
            self.assertEqual([row["status"] for row in task_state], ["pending"])
            event_state = first.store.conn.execute(
                "SELECT status FROM connector_source_events"
            ).fetchall()
            self.assertEqual([row["status"] for row in event_state], ["pending"])
            baseline = first.store.conn.execute(
                "SELECT baseline_cursor FROM provider_sync_generations WHERE status='running'"
            ).fetchone()["baseline_cursor"]
            self.assertEqual(json.loads(baseline)["checkpoint"], "baseline")
            self.assertIsNone(first.store.conn.execute("SELECT cursor_value FROM connector_cursors").fetchone())
            first.close()

            second = CompanyOS(db_path)
            try:
                _, resumed_identity = issue_identity(second, org.id, person.id, ws.id, "actor_google_restart")
                resumed_integration = second.integrations.get(resumed_identity, integration["id"])
                second.integrations.connector_factory = factory
                with patch(
                    "auremgrid.services.integration_ops.LIVE_SOURCES",
                    frozenset({"slack", "clickup", "google_drive"}),
                ):
                    result = second.integrations.sync(resumed_identity, resumed_integration["id"])
                self.assertEqual(result["status"], "completed")
                self.assertFalse(result["backfill_remaining"])
                cursor = second.store.conn.execute(
                    "SELECT cursor_value FROM connector_cursors WHERE connector='google_drive'"
                ).fetchone()["cursor_value"]
                self.assertEqual(json.loads(cursor)["checkpoint"], "after-baseline")
                self.assertEqual(
                    second.store.conn.execute(
                        "SELECT COUNT(*) FROM provider_sync_tasks WHERE status!='completed'"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    second.store.conn.execute(
                        "SELECT status FROM provider_sync_generations"
                    ).fetchone()[0],
                    "completed",
                )
                source_rows = second.store.conn.execute(
                    "SELECT source_key,COUNT(*) AS n FROM sources GROUP BY source_key"
                ).fetchall()
                self.assertEqual({row["source_key"] for row in source_rows}, {
                    "google-drive/files/restart-root", "google-drive/files/restart-child"
                })
                self.assertTrue(all(row["n"] == 1 for row in source_rows))
                versions = second.store.conn.execute(
                    "SELECT external_id,provider_version FROM provider_object_routes ORDER BY external_id"
                ).fetchall()
                self.assertEqual(
                    [(row["external_id"], row["provider_version"]) for row in versions],
                    [("restart-child", "v1"), ("restart-root", "v1")],
                )
                self.assertEqual(calls[0][0], None)
                self.assertEqual(calls[-1][0], "backfill")
                self.assertEqual(json.loads(baseline)["checkpoint"], "baseline")
                self.assertEqual(account_key, second.integrations._mapping_hash("google_drive", "folder:root", ws.id))
            finally:
                second.close()

    def test_drive_reconciliation_task_restarts_without_cursor_promotion(self) -> None:
        integration = self.os.integrations.configure(
            self.identity, "google_drive", "permission-1",
            {"folder:root": self.ws.id}, [GOOGLE_DRIVE_READ_SCOPE],
        )
        self.os.integrations.bind_credential(
            self.identity, integration["id"], "Google bundle",
            "env:AUREMGRID_TEST_GOOGLE_WIRING",
            ["connector:google_drive", GOOGLE_DRIVE_READ_SCOPE],
        )
        calls = {"pull": 0, "reconciled": False}

        def factory(mode, _source, _secret, *args):
            if mode == "refresh":
                return {"access_token": "access", "scopes": [GOOGLE_DRIVE_READ_SCOPE]}
            if mode == "verify":
                return {
                    "account_id": "permission-1", "account_name": "Drive",
                    "granted_permissions": [GOOGLE_DRIVE_READ_SCOPE],
                }
            route, workspace, cursor, runtime = args
            calls["pull"] += 1
            event_id = "moved-folder"
            dedupe = f"{event_id}:{calls['pull']}"
            event = ConnectorSourceEvent(
                dedupe, event_id, "file_changed", f"google-drive/files/{event_id}",
                f"https://drive.test/{event_id}", "folder metadata",
                {
                    "file": {"id": event_id, "parents": ["root"], "mimeType": "application/vnd.google-apps.folder"},
                    "route_keys": [route], "workspace_ids": [workspace],
                },
            )
            mutation = RouteLifecycleMutation(event_id, route, workspace, "upsert", str(calls["pull"]), dedupe)
            if runtime.get("_runtime_provider_task_type") == "reconcile":
                calls["reconciled"] = True
                return [event], cursor, False, {"lifecycle_mutations": (mutation,)}
            if calls["reconciled"]:
                return [], '{"v":1,"phase":"changes","checkpoint":"base-2","page_token":null}', False, {}
            request = DriveReconciliationRequest(
                event_id, ("root",), "unknown_ancestry", False, operation_key="wave-1"
            )
            return [event], '{"v":1,"phase":"changes","checkpoint":"base-1","page_token":null}', False, {
                "lifecycle_mutations": (mutation,), "reconciliation_requests": (request,),
            }

        self.os.integrations.connector_factory = factory
        self.os.integrations.verify(self.identity, integration["id"])
        first = self.os.integrations.sync(self.identity, integration["id"])
        self.assertEqual(first["status"], "completed")
        cursor_before = self.os.store.conn.execute(
            "SELECT cursor_value FROM connector_cursors WHERE connector='google_drive'"
        ).fetchone()
        self.assertIsNone(cursor_before)
        pending = self.os.store.conn.execute(
            "SELECT task_type,status FROM provider_sync_tasks"
        ).fetchall()
        self.assertEqual([(row["task_type"], row["status"]) for row in pending], [("reconcile", "pending")])
        # The resumed worker executes the reconciliation wave, then consumes a
        # regular changes page and promotes the original checkpoint in one run.
        self.os.integrations.sync(self.identity, integration["id"])
        self.assertEqual(calls["pull"], 3)
        cursor_after = self.os.store.conn.execute(
            "SELECT cursor_value FROM connector_cursors WHERE connector='google_drive'"
        ).fetchone()["cursor_value"]
        self.assertEqual(json.loads(cursor_after)["checkpoint"], "base-2")
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM provider_sync_tasks WHERE status!='completed'").fetchone()[0],
            0,
        )

    def test_drive_multilevel_descendant_wave_propagates_parent_and_drains(self) -> None:
        integration = self.os.integrations.configure(
            self.identity, "google_drive", "permission-1",
            {"folder:root": self.ws.id}, [GOOGLE_DRIVE_READ_SCOPE],
        )
        self.os.integrations.bind_credential(
            self.identity, integration["id"], "Google bundle",
            "env:AUREMGRID_TEST_GOOGLE_WIRING",
            ["connector:google_drive", GOOGLE_DRIVE_READ_SCOPE],
        )
        calls = {"count": 0}

        def factory(mode, _source, _secret, *args):
            if mode == "refresh":
                return {"access_token": "access", "scopes": [GOOGLE_DRIVE_READ_SCOPE]}
            if mode == "verify":
                return {
                    "account_id": "permission-1", "account_name": "Drive",
                    "granted_permissions": [GOOGLE_DRIVE_READ_SCOPE],
                }
            route, workspace, cursor, runtime = args
            calls["count"] += 1
            task_type = runtime.get("_runtime_provider_task_type")
            payload = runtime.get("_runtime_provider_task_payload") or {}
            if task_type == "descendants":
                external_id = "child-folder"
                next_tasks = (DriveBackfillTask(route, external_id),)
                phase = '{"v":1,"phase":"backfill","checkpoint":"base"}'
            elif task_type == "backfill" and payload.get("parent_wave") == "moved-folder" and payload.get("kind") == "drive_tree":
                external_id = "grandchild-folder"
                next_tasks = (DriveBackfillTask(route, external_id),)
                phase = '{"v":1,"phase":"backfill","checkpoint":"base"}'
            elif task_type == "backfill":
                external_id = "leaf-file"
                next_tasks = ()
                phase = '{"v":1,"phase":"changes","checkpoint":"base-2","page_token":null}'
            else:
                external_id = "moved-folder"
                next_tasks = ()
                phase = '{"v":1,"phase":"changes","checkpoint":"base-1","page_token":null}'
            dedupe = f"{external_id}:{calls['count']}"
            event = ConnectorSourceEvent(
                dedupe, external_id, "file_changed", f"google-drive/files/{external_id}",
                f"https://drive.test/{external_id}", external_id,
                {"file": {"id": external_id, "parents": ["root"], "mimeType": "application/pdf"},
                 "route_keys": [route], "workspace_ids": [workspace]},
            )
            mutation = RouteLifecycleMutation(external_id, route, workspace, "upsert", str(calls["count"]), dedupe)
            if task_type is None:
                request = DriveReconciliationRequest(
                    "moved-folder", ("root",), "folder_moved", True,
                    operation_key="move-wave", descendant_ids=(),
                )
                return [event], phase, False, {"lifecycle_mutations": (mutation,), "reconciliation_requests": (request,)}
            return [event], phase, False, {"lifecycle_mutations": (mutation,), "backfill_tasks": next_tasks}

        self.os.integrations.connector_factory = factory
        self.os.integrations.verify(self.identity, integration["id"])
        self.os.integrations.sync(self.identity, integration["id"])
        result = self.os.integrations.sync(self.identity, integration["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls["count"], 4)
        rows = self.os.store.conn.execute(
            "SELECT payload,status FROM provider_sync_tasks ORDER BY created_at,id"
        ).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row["status"] == "completed" for row in rows))
        self.assertTrue(all(json.loads(row["payload"]).get("parent_wave") in {None, "moved-folder"} for row in rows))

    def test_real_drive_adapter_move_out_persists_parent_first_ancestry_and_retires_descendants(self) -> None:
        integration = self.os.integrations.configure(
            self.identity, "google_drive", "permission-1",
            {"folder:root": self.ws.id}, [GOOGLE_DRIVE_READ_SCOPE],
        )
        self.os.integrations.bind_credential(
            self.identity, integration["id"], "Google bundle",
            "env:AUREMGRID_TEST_GOOGLE_WIRING",
            ["connector:google_drive", GOOGLE_DRIVE_READ_SCOPE],
        )
        calls = {"pull": 0}

        def factory(mode, _source, _secret, *args):
            if mode == "refresh":
                return {"access_token": "access", "scopes": [GOOGLE_DRIVE_READ_SCOPE]}
            if mode == "verify":
                return {"account_id": "permission-1", "account_name": "Drive", "granted_permissions": [GOOGLE_DRIVE_READ_SCOPE]}
            route, workspace, cursor, runtime = args
            calls["pull"] += 1
            task_type = runtime.get("_runtime_provider_task_type")
            if task_type == "reconcile":
                transport = type("Transport", (), {})()
                responses = [
                    {"id": "target", "mimeType": "application/vnd.google-apps.folder", "parents": ["outside"]},
                    {"id": "outside", "mimeType": "application/vnd.google-apps.folder", "parents": []},
                ]
                def request(_method, _url, _headers, _body):
                    body = responses.pop(0)
                    return HttpResponse(200, {}, body)
                transport.__call__ = request
                # A tiny callable transport keeps the test on the real adapter
                # path while making provider I/O deterministic.
                class QueueTransport:
                    def __init__(self, values): self.values = list(values)
                    def __call__(self, _method, _url, _headers=None, _body=None): return HttpResponse(200, {}, self.values.pop(0))
                adapter = GoogleDriveConnector(
                    "access", QueueTransport(responses), folder_workspace_mappings={"root": workspace},
                    owned_route_key=route, route_state=self.os.store.provider_route_state(
                        workspace, "google_drive", f"{integration['id']}:{self.os.integrations._mapping_hash('google_drive', route, workspace)}"
                    ), ancestry_state=self.os.integrations._drive_ancestry_state(
                        workspace, "google_drive", f"{integration['id']}:{self.os.integrations._mapping_hash('google_drive', route, workspace)}"
                    ), backfill_task=DriveBackfillTask(route, "target"), task_type="reconcile",
                    task_payload=runtime.get("_runtime_provider_task_payload") or {},
                )
                result = adapter.pull(cursor)
                return result.events, result.next_cursor, result.has_more, {
                    "lifecycle_mutations": result.lifecycle_mutations,
                    "reconciliation_requests": result.reconciliation_requests,
                    "ancestry_resolutions": result.ancestry_resolutions,
                }
            if task_type == "descendants":
                adapter = GoogleDriveConnector(
                    "access", folder_workspace_mappings={"root": workspace}, owned_route_key=route,
                    route_state=self.os.store.provider_route_state(
                        workspace, "google_drive", f"{integration['id']}:{self.os.integrations._mapping_hash('google_drive', route, workspace)}"
                    ), task_type="descendants", task_payload=runtime.get("_runtime_provider_task_payload") or {},
                    backfill_task=DriveBackfillTask(route, "target"),
                )
                result = adapter.pull(cursor)
                return result.events, result.next_cursor, result.has_more, {"lifecycle_mutations": result.lifecycle_mutations}
            if calls["pull"] == 1:
                target = ConnectorSourceEvent(
                    "target:active", "target", "file_changed", "google-drive/files/target", "https://drive.test/target",
                    "target", {"file": {"id": "target", "parents": ["root"], "mimeType": "application/vnd.google-apps.folder"}, "route_keys": [route], "workspace_ids": [workspace]},
                )
                child = ConnectorSourceEvent(
                    "child:active", "child", "file_changed", "google-drive/files/child", "https://drive.test/child",
                    "child", {"file": {"id": "child", "parents": ["target"], "mimeType": "application/pdf"}, "route_keys": [route], "workspace_ids": [workspace]},
                )
                mutations = (
                    RouteLifecycleMutation("target", route, workspace, "upsert", "1", "target:active"),
                    RouteLifecycleMutation("child", route, workspace, "upsert", "1", "child:active"),
                )
                request = DriveReconciliationRequest("target", ("outside",), "folder_moved", False, operation_key="move-wave")
                return [target, child], '{"v":1,"phase":"changes","checkpoint":"base-1","page_token":null}', False, {
                    "lifecycle_mutations": mutations, "reconciliation_requests": (request,)
                }
            return [], '{"v":1,"phase":"changes","checkpoint":"base-final","page_token":null}', False, {}

        self.os.integrations.connector_factory = factory
        self.os.integrations.verify(self.identity, integration["id"])
        self.os.integrations.sync(self.identity, integration["id"])
        result = self.os.integrations.sync(self.identity, integration["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls["pull"], 4)
        cursor = self.os.store.conn.execute("SELECT cursor_value FROM connector_cursors WHERE connector='google_drive'").fetchone()["cursor_value"]
        self.assertEqual(json.loads(cursor)["checkpoint"], "base-final")
        routes = self.os.store.conn.execute("SELECT external_id,status FROM provider_object_routes ORDER BY external_id").fetchall()
        self.assertEqual([(row["external_id"], row["status"]) for row in routes], [("child", "retired"), ("target", "retired")])
        outside = self.os.store.conn.execute("SELECT root_route_keys,reconciliation_status FROM provider_object_ancestry WHERE external_id='outside'").fetchone()
        self.assertEqual(json.loads(outside["root_route_keys"]), [])
        self.assertEqual(outside["reconciliation_status"], "resolved")
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM provider_sync_tasks WHERE status!='completed'").fetchone()[0], 0)

    def test_drive_cross_workspace_overlap_quarantines_without_workspace_evidence(self) -> None:
        ws2 = self.os.create_organization_workspace(self.org.id, "Client Two", "client", "ws_google_wiring_two")
        self.os.add_person_to_workspace(self.org.id, ws2.id, self.person.id, "admin")
        self.os.create_actor(ws2.id, "Connector Two", "admin", "actor_google_wiring_two")
        _, global_identity = issue_identity(self.os, self.org.id, self.person.id)
        self.os.auth.bind_actor(global_identity, self.ws.id, "actor_google_wiring")
        self.os.auth.bind_actor(global_identity, ws2.id, "actor_google_wiring_two")
        integration = self.os.integrations.configure(
            global_identity, "google_drive", "permission-1",
            {"folder:root-a": self.ws.id, "folder:root-b": ws2.id}, [GOOGLE_DRIVE_READ_SCOPE],
        )
        self.os.integrations.bind_credential(
            global_identity, integration["id"], "Google bundle",
            "env:AUREMGRID_TEST_GOOGLE_WIRING",
            ["connector:google_drive", GOOGLE_DRIVE_READ_SCOPE],
        )
        state = {"external_id": "ambiguous-one"}

        def factory(mode, _source, _secret, *args):
            if mode == "refresh":
                return {"access_token": "access", "scopes": [GOOGLE_DRIVE_READ_SCOPE]}
            if mode == "verify":
                return {
                    "account_id": "permission-1", "account_name": "Drive",
                    "granted_permissions": [GOOGLE_DRIVE_READ_SCOPE],
                }
            route, workspace, _cursor, _runtime = args
            external_id = state["external_id"]
            dedupe = f"{external_id}:overlap"
            event = ConnectorSourceEvent(
                dedupe, external_id, "file_changed", f"google-drive/files/{external_id}",
                "https://drive.test/ambiguous", "must not be ingested",
                {"route_keys": ["folder:root-a", "folder:root-b"], "workspace_ids": [self.ws.id, ws2.id]},
            )
            mutation = RouteLifecycleMutation(external_id, route, workspace, "upsert", "1", dedupe)
            return [event], '{"v":1,"phase":"changes","checkpoint":"base","page_token":null}', False, {
                "lifecycle_mutations": (mutation,)
            }

        self.os.integrations.connector_factory = factory
        self.os.integrations.verify(global_identity, integration["id"])
        with self.assertRaises(ConnectorTransportError):
            self.os.integrations.sync(global_identity, integration["id"])
        state["external_id"] = "ambiguous-two"
        with self.assertRaises(ConnectorTransportError):
            self.os.integrations.sync(global_identity, integration["id"])
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM connector_source_events").fetchone()[0], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM connector_cursors").fetchone()[0], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
        quarantine = self.os.store.conn.execute(
            "SELECT * FROM provider_sync_quarantines ORDER BY created_at,id"
        ).fetchall()
        self.assertEqual(len(quarantine), 2)
        self.assertTrue(all("workspace_id" not in quarantine[0].keys() for _ in [0]))
        self.assertNotIn("must not be ingested", "\n".join(str(dict(row)) for row in quarantine))

    def test_gmail_cross_workspace_overlap_quarantines_without_workspace_evidence(self) -> None:
        ws2 = self.os.create_organization_workspace(self.org.id, "Mailbox Two", "client", "ws_google_wiring_mail_two")
        self.os.add_person_to_workspace(self.org.id, ws2.id, self.person.id, "admin")
        self.os.create_actor(ws2.id, "Mailbox Connector Two", "admin", "actor_google_wiring_mail_two")
        _, global_identity = issue_identity(self.os, self.org.id, self.person.id)
        self.os.auth.bind_actor(global_identity, self.ws.id, "actor_google_wiring")
        self.os.auth.bind_actor(global_identity, ws2.id, "actor_google_wiring_mail_two")
        integration = self.os.integrations.configure(
            global_identity, "gmail", "owner@wiring.test",
            {"label:INBOX": self.ws.id, "label:Projects": ws2.id}, [GMAIL_READ_SCOPE],
        )
        self.os.integrations.bind_credential(
            global_identity, integration["id"], "Gmail bundle",
            "env:AUREMGRID_TEST_GOOGLE_WIRING",
            ["connector:gmail", GMAIL_READ_SCOPE],
        )
        state = {"external_id": "message-overlap-one"}

        def factory(mode, _source, _secret, *args):
            if mode == "refresh":
                return {"access_token": "access", "scopes": [GMAIL_READ_SCOPE]}
            if mode == "verify":
                return {
                    "account_id": "owner@wiring.test", "account_name": "Mailbox",
                    "granted_permissions": [GMAIL_READ_SCOPE],
                }
            route, workspace, _cursor, _runtime = args
            external_id = state["external_id"]
            dedupe = f"{external_id}:overlap"
            event = ConnectorSourceEvent(
                dedupe, external_id, "message_added", f"gmail/messages/{external_id}",
                "https://mail.google.test/ambiguous", "must not be ingested",
                {"route_keys": ["label:INBOX", "label:Projects"], "workspace_ids": [self.ws.id, ws2.id]},
            )
            mutation = RouteLifecycleMutation(external_id, route, workspace, "upsert", "1", dedupe)
            return [event], '{"v":1,"phase":"history","checkpoint":"101","page_token":null}', False, {
                "lifecycle_mutations": (mutation,)
            }

        self.os.integrations.connector_factory = factory
        self.os.integrations.verify(global_identity, integration["id"])
        with self.assertRaises(ConnectorTransportError):
            self.os.integrations.sync(global_identity, integration["id"])
        state["external_id"] = "message-overlap-two"
        with self.assertRaises(ConnectorTransportError):
            self.os.integrations.sync(global_identity, integration["id"])
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM connector_source_events").fetchone()[0], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM connector_cursors").fetchone()[0], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
        quarantine = self.os.store.conn.execute(
            "SELECT * FROM provider_sync_quarantines ORDER BY created_at,id"
        ).fetchall()
        self.assertEqual(len(quarantine), 2)
        self.assertNotIn("must not be ingested", "\n".join(str(dict(row)) for row in quarantine))


if __name__ == "__main__":
    unittest.main()
