from __future__ import annotations

import tempfile
import threading
import unittest
import os as environment
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from auremgrid.connectors.google_auth import ConnectorSourceEvent
from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS


class IntegrationStreamRaceTests(unittest.TestCase):
    def test_revocation_waits_for_atomic_canonical_ingestion_then_wins_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential-ingest-race.sqlite"
            first = CompanyOS(path)
            org = first.create_organization("Auremgrid")
            ws = first.create_organization_workspace(org.id, "Client", "client")
            person = first.create_person(org.id, "Owner", "owner@credential-race.test", role="owner")
            first.add_person_to_workspace(org.id, ws.id, person.id, "admin")
            actor = first.create_actor(ws.id, "Connector actor", "admin")
            principal = first.auth.create_principal(org.id, person.id, "owner@credential-race.test")
            identity = first.auth.identity_for_principal(principal["id"], ws.id)
            first.auth.bind_actor(identity, ws.id, actor.id)
            integration = first.integrations.configure(
                identity, "slack", "T1", {"C1": ws.id}, ["channels:history"]
            )
            environment.environ["AUREMGRID_RACE_SLACK_TOKEN"] = "race-token"
            binding = first.integrations.bind_credential(
                identity, integration["id"], "Slack read token",
                "env:AUREMGRID_RACE_SLACK_TOKEN", ["connector:slack", "channels:history"],
            )

            def factory(mode, *_args):
                if mode == "verify":
                    return {
                        "account_id": "T1", "account_name": "Expected",
                        "granted_permissions": ["channels:history"],
                    }
                return [ConnectorSourceEvent(
                    "race-1", "race-1", "upsert", "slack:C1:race-1", "slack://C1/race-1",
                    "# Atomic evidence", {"connector": "slack"},
                )], '{"oldest":"1","page_cursor":null}', False

            first.integrations.connector_factory = factory
            first.integrations.verify(identity, integration["id"])
            second = CompanyOS(path)
            second_identity = second.auth.identity_for_principal(principal["id"], ws.id)
            ingestion_paused = threading.Event()
            resume_ingestion = threading.Event()
            revoke_started = threading.Event()
            revoke_finished = threading.Event()
            original_create_document = first.store.create_document

            def paused_create_document(document):
                ingestion_paused.set()
                if not resume_ingestion.wait(timeout=5):
                    raise AssertionError("test did not resume canonical ingestion")
                return original_create_document(document)

            first.store.create_document = paused_create_document

            def revoke():
                revoke_started.set()
                second.secrets.revoke(second_identity, binding["id"])
                revoke_finished.set()

            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    sync_future = pool.submit(first.integrations.sync, identity, integration["id"])
                    self.assertTrue(ingestion_paused.wait(timeout=5))
                    revoke_future = pool.submit(revoke)
                    self.assertTrue(revoke_started.wait(timeout=5))
                    self.assertFalse(revoke_finished.wait(timeout=0.2))
                    resume_ingestion.set()
                    self.assertEqual(sync_future.result(timeout=5)["status"], "completed")
                    revoke_future.result(timeout=5)
                self.assertEqual(first.store.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
                self.assertEqual(first.store.conn.execute(
                    "SELECT status FROM connector_source_events WHERE external_id='race-1'"
                ).fetchone()[0], "ingested")
                current = first.integrations.get(identity, integration["id"])
                self.assertEqual(current["status"], "reauth_required")
                self.assertEqual(current["health"], "error")
            finally:
                resume_ingestion.set()
                second.close()
                first.close()
                environment.environ.pop("AUREMGRID_RACE_SLACK_TOKEN", None)

    def test_two_connections_cannot_enqueue_the_same_active_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"stream-race.sqlite"
            first=CompanyOS(path)
            org=first.create_organization("Auremgrid")
            ws=first.create_organization_workspace(org.id,"Client","client")
            person=first.create_person(org.id,"Owner","owner@stream-race.test",role="owner")
            first.add_person_to_workspace(org.id,ws.id,person.id,"admin")
            principal=first.auth.create_principal(org.id,person.id,"owner@stream-race.test")
            identity=first.auth.identity_for_principal(principal["id"],ws.id)
            integration=first.integrations.configure(
                identity,"slack","T1",{"C1":ws.id},["channels:history"]
            )
            first.store.conn.execute(
                "UPDATE integrations SET status='authorized' WHERE id=?",(integration["id"],)
            )
            first.store.conn.commit()
            second=CompanyOS(path)
            second_identity=second.auth.identity_for_principal(principal["id"],ws.id)
            barrier=threading.Barrier(2)

            def enqueue(os_instance,caller):
                barrier.wait(timeout=5)
                try:
                    return os_instance.integrations.enqueue_sync(caller,integration["id"])[0]["id"]
                except ValidationError:
                    return "blocked"

            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results=list(pool.map(lambda pair: enqueue(*pair),[(first,identity),(second,second_identity)]))
                self.assertEqual(results.count("blocked"),1)
                self.assertEqual(first.store.conn.execute(
                    "SELECT COUNT(*) FROM connector_stream_locks WHERE status='active'"
                ).fetchone()[0],1)
                self.assertEqual(first.store.conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE type='connector.sync'"
                ).fetchone()[0],1)
            finally:
                second.close();first.close()


if __name__=="__main__":
    unittest.main()
