from __future__ import annotations

import json
import os as environment
import unittest
from unittest.mock import patch

from auremgrid.connectors.figma import FIGMA_REQUIRED_PERMISSIONS
from auremgrid.connectors.http import ConnectorTransportError, HttpResponse
from auremgrid.domain.errors import ValidationError
from auremgrid.connectors.google_auth import ConnectorSourceEvent,RouteLifecycleMutation
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class FigmaApiTransport:
    """Exact provider-shaped transport used by the service wiring test."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def request(self, method, url, headers, body=None):
        self.calls.append(url)
        if url.endswith("/v1/me"):
            payload = {"id": "user-1", "email": "owner@figma.test"}
        elif url.endswith("/v1/files/file-1/meta"):
            payload = {
                "file": {
                    "name": "Consultation design",
                    "version": "version-1",
                    "last_touched_at": "2026-08-19T00:00:00Z",
                }
            }
        elif url.endswith("/v1/files/file-1?depth=1"):
            payload = {"name": "Consultation design", "document": {"id": "0:0"}}
        elif url.endswith("/v1/files/file-1?version=version-1"):
            payload = {
                "name": "Consultation design",
                "lastModified": "2026-08-19T00:00:00Z",
                "document": {
                    "id": "0:0", "name": "Page", "type": "DOCUMENT",
                    "children": [{
                        "id": "1:2", "name": "Consultation homepage", "type": "FRAME",
                        "absoluteBoundingBox": {"x": 1, "y": 2, "width": 1440, "height": 900},
                        "children": [{"id": "1:3", "type": "TEXT", "characters": "Book a consultation"}],
                    }],
                },
            }
        elif url.endswith("/v1/files/file-1/versions?page_size=50"):
            payload = {
                "versions": [{
                    "id": "version-history-1",
                    "label": "Client review",
                    "description": "Approved layout revision",
                    "created_at": "2026-08-18T12:00:00Z",
                    "user": {"id": "user-1", "handle": "Owner"},
                }],
            }
        elif url.endswith("/v1/files/file-1/versions?page_size=1"):
            payload = {"versions": []}
        else:
            raise AssertionError(f"unexpected Figma request: {method} {url}")
        return HttpResponse(200, {}, json.dumps(payload).encode())

class FigmaIntegrationWiringTests(unittest.TestCase):
    def setUp(self):
        self.os=CompanyOS(":memory:");self.org=self.os.create_organization("Agency","org_figma")
        self.ws=self.os.create_organization_workspace(self.org.id,"Client","client","ws_figma")
        self.person=self.os.create_person(self.org.id,"Owner","owner@figma.test",role="owner",person_id="person_figma")
        self.os.add_person_to_workspace(self.org.id,self.ws.id,self.person.id,"admin");self.os.create_actor(self.ws.id,"Figma","admin","actor_figma")
        _,self.identity=issue_identity(self.os,self.org.id,self.person.id,self.ws.id,"actor_figma")
        environment.environ["AUREMGRID_TEST_FIGMA"]="figma-token"
    def tearDown(self):environment.environ.pop("AUREMGRID_TEST_FIGMA",None);self.os.close()
    def test_exact_file_lifecycle_cursor_and_idempotent_restart(self):
        permissions=sorted(FIGMA_REQUIRED_PERMISSIONS)
        integration=self.os.integrations.configure(self.identity,"figma","user-1",{"file:file-1":self.ws.id},permissions)
        self.os.integrations.bind_credential(self.identity,integration["id"],"Figma token","env:AUREMGRID_TEST_FIGMA",["connector:figma",*permissions])
        state={"version":"v1","pulls":0}
        def factory(mode,_source,_secret,*args):
            if mode=="verify":return {"account_id":"user-1","account_name":"Owner","granted_permissions":permissions}
            route,workspace,cursor,_runtime=args;state["pulls"]+=1
            if cursor and state["version"] in cursor:return [],cursor,False,{"lifecycle_mutations":()}
            dedupe=f"file-1:{state['version']}";event=ConnectorSourceEvent(dedupe,"figma/files/file-1","file","figma/files/file-1","https://figma.test/file-1",f"design {state['version']}",{"route_keys":[route],"workspace_ids":[workspace]},"2026-08-19T00:00:00+00:00")
            mutation=RouteLifecycleMutation("figma/files/file-1",route,workspace,"upsert",state["version"],dedupe)
            return [event],f'{{"v":1,"file_key":"file-1","provider_version":"{state["version"]}"}}',False,{"lifecycle_mutations":(mutation,)}
        self.os.integrations.connector_factory=factory
        with patch("auremgrid.services.integration_ops.LIVE_SOURCES",frozenset({"figma"})):
            self.os.integrations.verify(self.identity,integration["id"]);self.os.integrations.sync(self.identity,integration["id"])
            first=self.os.integrations.get(self.identity,integration["id"]);self.assertEqual(first["object_count"],1)
            self.os.integrations.sync(self.identity,integration["id"]);self.assertEqual(self.os.integrations.get(self.identity,integration["id"])["object_count"],1)
            state["version"]="v2";self.os.integrations.sync(self.identity,integration["id"])
        versions=self.os.store.conn.execute("SELECT COUNT(*) FROM sources WHERE workspace_id=? AND source_key=?",(self.ws.id,"figma/files/file-1")).fetchone()[0]
        self.assertEqual(versions,2);self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM provider_object_routes WHERE connector='figma' AND status='active'").fetchone()[0],1)

    def test_cross_workspace_file_overlap_quarantines_without_workspace_evidence(self):
        ws2 = self.os.create_organization_workspace(self.org.id, "Client Two", "client", "ws_figma_two")
        self.os.add_person_to_workspace(self.org.id, ws2.id, self.person.id, "admin")
        self.os.create_actor(ws2.id, "Figma Two", "admin", "actor_figma_two")
        _, identity = issue_identity(self.os, self.org.id, self.person.id)
        self.os.auth.bind_actor(identity, self.ws.id, "actor_figma")
        self.os.auth.bind_actor(identity, ws2.id, "actor_figma_two")
        permissions = sorted(FIGMA_REQUIRED_PERMISSIONS)
        integration = self.os.integrations.configure(
            identity, "figma", "user-1",
            {"file:file-1": self.ws.id, "file:file-2": ws2.id}, permissions,
        )
        self.os.integrations.bind_credential(
            identity, integration["id"], "Figma token", "env:AUREMGRID_TEST_FIGMA",
            ["connector:figma", *permissions],
        )

        def factory(mode, _source, _secret, *args):
            if mode == "verify":
                return {"account_id": "user-1", "account_name": "Owner", "granted_permissions": permissions}
            route, workspace, _cursor, _runtime = args
            dedupe = "file-shared:overlap"
            event = ConnectorSourceEvent(
                dedupe, "figma/files/file-shared", "file", "figma/files/file-shared",
                "https://figma.test/ambiguous", "must not be ingested",
                {"route_keys": ["file:file-1", "file:file-2"], "workspace_ids": [self.ws.id, ws2.id]},
            )
            mutation = RouteLifecycleMutation("figma/files/file-shared", route, workspace, "upsert", "1", dedupe)
            return [event], '{"v":1,"file_key":"file-1","provider_version":"1"}', False, {"lifecycle_mutations": (mutation,)}

        self.os.integrations.connector_factory = factory
        self.os.integrations.verify(identity, integration["id"])
        with self.assertRaises(ConnectorTransportError):
            self.os.integrations.sync(identity, integration["id"])
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM connector_source_events").fetchone()[0], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM connector_cursors").fetchone()[0], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM provider_object_routes").fetchone()[0], 0)
        self.assertEqual(self.os.integrations.get(identity, integration["id"])["object_count"], 0)
        quarantine = self.os.store.conn.execute("SELECT * FROM provider_sync_quarantines").fetchall()
        self.assertEqual(len(quarantine), 1)
        self.assertNotIn("workspace_id", quarantine[0].keys())
        self.assertNotIn("must not be ingested", str(dict(quarantine[0])))

    def test_missing_each_required_scope_is_rejected(self):
        for missing in FIGMA_REQUIRED_PERMISSIONS:
            with self.subTest(missing=missing),self.assertRaises(ValidationError):
                self.os.integrations.configure(self.identity,"figma","user-1",{"file:file-1":self.ws.id},sorted(FIGMA_REQUIRED_PERMISSIONS-{missing}))

    def test_real_adapter_uses_official_metadata_shape_and_version_fence(self):
        permissions = sorted(FIGMA_REQUIRED_PERMISSIONS)
        integration = self.os.integrations.configure(
            self.identity,
            "figma",
            "user-1",
            {"file:file-1": self.ws.id},
            permissions,
        )
        self.os.integrations.bind_credential(
            self.identity,
            integration["id"],
            "Figma token",
            "env:AUREMGRID_TEST_FIGMA",
            ["connector:figma", *permissions],
        )
        transport = FigmaApiTransport()

        with patch("auremgrid.connectors.figma.HttpTransport", return_value=transport):
            verified = self.os.integrations.verify(self.identity, integration["id"])
            self.assertEqual(verified["integration"]["status"], "authorized")
            self.os.integrations.sync(self.identity, integration["id"])
            self.os.integrations.sync(self.identity, integration["id"])

        self.assertIn(
            "https://api.figma.com/v1/files/file-1?version=version-1",
            transport.calls,
        )
        self.assertEqual(
            transport.calls.count("https://api.figma.com/v1/files/file-1?version=version-1"),
            1,
        )
        source_rows = self.os.store.conn.execute(
            "SELECT workspace_id,source_key FROM sources ORDER BY source_key",
        ).fetchall()
        self.assertEqual(
            [(row["workspace_id"], row["source_key"]) for row in source_rows],
            [(self.ws.id, "figma/files/file-1"), (self.ws.id, "figma/files/file-1/nodes/1:2")],
        )
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM connector_source_events").fetchone()[0],
            2,
        )
        self.assertEqual(
            self.os.store.conn.execute(
                "SELECT COUNT(*) FROM provider_object_routes WHERE connector='figma' AND status='active'",
            ).fetchone()[0],
            1,
        )
        self.assertEqual(self.os.integrations.get(self.identity, integration["id"])["object_count"], 1)

    def test_real_adapter_persists_optional_version_evidence_without_extra_provider_route(self):
        permissions = sorted(FIGMA_REQUIRED_PERMISSIONS | {"file_versions:read"})
        integration = self.os.integrations.configure(
            self.identity,
            "figma",
            "user-1",
            {"file:file-1": self.ws.id},
            permissions,
        )
        self.os.integrations.bind_credential(
            self.identity,
            integration["id"],
            "Figma token",
            "env:AUREMGRID_TEST_FIGMA",
            ["connector:figma", *permissions],
        )
        transport = FigmaApiTransport()

        with patch("auremgrid.connectors.figma.HttpTransport", return_value=transport):
            self.os.integrations.verify(self.identity, integration["id"])
            self.os.integrations.sync(self.identity, integration["id"])
            source_count_after_first = self.os.store.conn.execute(
                "SELECT COUNT(*) FROM sources WHERE workspace_id=?", (self.ws.id,),
            ).fetchone()[0]
            event_count_after_first = self.os.store.conn.execute(
                "SELECT COUNT(*) FROM connector_source_events",
            ).fetchone()[0]
            version_reads_after_first = transport.calls.count(
                "https://api.figma.com/v1/files/file-1/versions?page_size=50",
            )
            self.os.integrations.sync(self.identity, integration["id"])

        source_keys = [
            row["source_key"]
            for row in self.os.store.conn.execute(
                "SELECT source_key FROM sources WHERE workspace_id=? ORDER BY source_key", (self.ws.id,),
            ).fetchall()
        ]
        self.assertIn("figma/files/file-1", source_keys)
        self.assertIn("figma/files/file-1/versions/version-history-1", source_keys)
        self.assertEqual(
            self.os.store.conn.execute(
                "SELECT COUNT(*) FROM provider_object_routes WHERE connector='figma' AND status='active'",
            ).fetchone()[0],
            1,
        )
        self.assertEqual(self.os.integrations.get(self.identity, integration["id"])["object_count"], 1)
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM sources WHERE workspace_id=?", (self.ws.id,)).fetchone()[0],
            source_count_after_first,
        )
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM connector_source_events").fetchone()[0],
            event_count_after_first,
        )
        self.assertEqual(version_reads_after_first, 1)
        self.assertEqual(
            transport.calls.count("https://api.figma.com/v1/files/file-1/versions?page_size=50"),
            version_reads_after_first,
        )
        self.assertEqual(sum("/comments" in url for url in transport.calls), 0)

if __name__=="__main__":unittest.main()
