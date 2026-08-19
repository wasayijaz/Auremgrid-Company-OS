from __future__ import annotations

import os as environment
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from auremgrid.connectors.google_auth import ConnectorSourceEvent
from auremgrid.connectors.http import ConnectorTransportError
from auremgrid.api.mcp import McpToolRouter
from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.services.worker import run_one_job
from tests.auth_support import issue_identity


class IntegrationOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS()
        self.org = self.os.create_organization("Auremgrid", "org_integrations")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client", "ws_integrations")
        self.person = self.os.create_person(self.org.id, "Owner", "owner@integration.test", role="owner", person_id="person_integrations")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")
        self.os.create_actor(self.ws.id, "Connector actor", "admin", "actor_integrations")
        self.token, self.identity = issue_identity(
            self.os, self.org.id, self.person.id, self.ws.id, "actor_integrations"
        )
        environment.environ["AUREMGRID_TEST_SLACK_TOKEN"] = "sentinel-live-token"

    def tearDown(self) -> None:
        environment.environ.pop("AUREMGRID_TEST_SLACK_TOKEN", None)
        self.os.close()

    def _configured(self) -> dict:
        integration = self.os.integrations.configure(
            self.identity, "slack", "T1", {"C1": self.ws.id}, ["channels:history"]
        )
        self.os.integrations.bind_credential(
            self.identity, integration["id"], "Slack read token", "env:AUREMGRID_TEST_SLACK_TOKEN",
            ["connector:slack", "channels:history"],
        )
        return integration

    def test_verified_sync_is_durable_deduplicated_and_secret_free(self) -> None:
        integration = self._configured()

        def factory(mode, source, secret, *args):
            self.assertEqual(source, "slack")
            self.assertEqual(secret, "sentinel-live-token")
            if mode == "verify":
                return {"account_id": "T1", "account_name": "Agency workspace",
                        "granted_permissions": ["channels:history"]}
            _, workspace_id, cursor, _ = args
            event = ConnectorSourceEvent(
                "slack:C1:1:rev1", "1", "upsert", "slack:C1:1", "slack://C1/1",
                "# Slack message\n\nA durable update sentinel-live-token", {"connector": "slack", "Authorization": "Bearer sentinel-live-token"},
                datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
            )
            return [event], '{"oldest":"1","page_cursor":null}'

        self.os.integrations.connector_factory = factory
        verified = self.os.integrations.verify(self.identity, integration["id"])
        self.assertEqual(verified["integration"]["status"], "authorized")
        first = self.os.integrations.sync(self.identity, integration["id"])
        second = self.os.integrations.sync(self.identity, integration["id"])
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["seen"], 1)
        cursor = self.os.integrations.inbox.get_cursor(
            self.org.id, self.ws.id, "slack",
            f"{integration['id']}:{self.os.integrations._mapping_hash('slack','C1',self.ws.id)}"
        )
        self.assertEqual(cursor, '{"oldest":"1","page_cursor":null}')
        raw = "\n".join(self.os.store.conn.iterdump())
        self.assertNotIn("sentinel-live-token", raw)
        current = self.os.integrations.get(self.identity, integration["id"])
        self.assertEqual(current["status"], "connected")
        self.assertNotIn("reference", current["credential"])

    def test_provider_mapping_cannot_cross_organization(self) -> None:
        other = self.os.create_organization("Other", "org_other_integrations")
        other_ws = self.os.create_organization_workspace(other.id, "Other client", "client", "ws_other_integrations")
        with self.assertRaises(AuthorizationError):
            self.os.integrations.configure(self.identity, "slack", "T1", {"C2": other_ws.id}, ["channels:history"])

    def test_provider_minimum_permissions_are_server_enforced(self) -> None:
        with self.assertRaises(ValidationError):
            self.os.integrations.configure(self.identity,"slack","T1",{"C1":self.ws.id},[])
        with self.assertRaises(ValidationError):
            self.os.integrations.configure(self.identity,"clickup","T1",{"L1":self.ws.id},["tasks:read"])

    def test_verification_rejects_wrong_account_and_missing_permission(self) -> None:
        integration=self._configured()
        self.os.integrations.connector_factory=lambda *_: {
            "account_id":"T2","account_name":"Wrong","granted_permissions":["channels:history"]
        }
        with self.assertRaises(AuthorizationError):
            self.os.integrations.verify(self.identity,integration["id"])
        self.os.integrations.connector_factory=lambda *_: {
            "account_id":"T1","account_name":"Expected","granted_permissions":[]
        }
        with self.assertRaises(AuthorizationError):
            self.os.integrations.verify(self.identity,integration["id"])
        current=self.os.integrations.get(self.identity,integration["id"])
        self.assertEqual(current["status"],"not_connected")
        self.assertEqual(current["credential"]["status"],"unverified")

    def test_sync_reverifies_current_env_credential_before_provider_read(self) -> None:
        integration=self._configured()
        pulls=[]
        def factory(mode,source,secret,*args):
            if mode=="verify":
                return {"account_id":"T1" if secret=="sentinel-live-token" else "T2",
                        "account_name":"Provider","granted_permissions":["channels:history"]}
            pulls.append(args)
            return [],"checkpoint"
        self.os.integrations.connector_factory=factory
        self.os.integrations.verify(self.identity,integration["id"])
        environment.environ["AUREMGRID_TEST_SLACK_TOKEN"]="replacement-team-token"
        with self.assertRaises(ConnectorTransportError):
            self.os.integrations.sync(self.identity,integration["id"])
        self.assertEqual(pulls,[])
        self.assertEqual(self.os.store.conn.execute(
            "SELECT COUNT(*) FROM connector_ingest_batches"
        ).fetchone()[0],0)
        self.assertEqual(self.os.integrations.get(self.identity,integration["id"])["status"],"reauth_required")

    def test_inflight_credential_revocation_fences_all_sync_writes(self) -> None:
        integration = self._configured()
        binding_id = self.os.integrations.get(self.identity, integration["id"])["credential"]["id"]
        revoked = []

        def factory(mode, source, secret, *args):
            if mode == "verify":
                return {
                    "account_id": "T1",
                    "account_name": "Expected",
                    "granted_permissions": ["channels:history"],
                }
            revoked.append(self.os.secrets.revoke(self.identity, binding_id))
            return [ConnectorSourceEvent(
                "revoked-1", "revoked-1", "upsert", "slack:C1:revoked",
                "slack://C1/revoked", "# Must not persist", {"connector": "slack"},
            )], '{"oldest":"1","page_cursor":null}', False

        self.os.integrations.connector_factory = factory
        self.os.integrations.verify(self.identity, integration["id"])

        with self.assertRaises(ValidationError):
            self.os.integrations.sync(self.identity, integration["id"])

        self.assertTrue(revoked)
        self.assertEqual(self.os.store.conn.execute(
            "SELECT COUNT(*) FROM connector_ingest_batches"
        ).fetchone()[0], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
        current = self.os.integrations.get(self.identity, integration["id"])
        self.assertEqual(current["status"], "reauth_required")
        self.assertEqual(current["health"], "error")

    def test_same_tick_rotation_and_reverification_cannot_reuse_old_generation(self) -> None:
        integration = self._configured()
        self.os.integrations.connector_factory = lambda mode, *_args: {
            "account_id": "T1",
            "account_name": "Expected",
            "granted_permissions": ["channels:history"],
        }
        self.os.integrations.verify(self.identity, integration["id"])
        before = self.os.integrations._verified_binding(self.identity, integration)
        environment.environ["AUREMGRID_TEST_SLACK_TOKEN_NEXT"] = "next-token"
        try:
            with patch("auremgrid.services.secrets._now", return_value="2026-01-01T00:00:00+00:00"):
                self.os.secrets.rotate_reference(
                    self.identity, before["id"], "env:AUREMGRID_TEST_SLACK_TOKEN_NEXT"
                )
                self.os.integrations.verify(self.identity, integration["id"])
            after = self.os.integrations._verified_binding(self.identity, integration)
            self.assertEqual(after["updated_at"], "2026-01-01T00:00:00+00:00")
            self.assertEqual(after["generation"], before["generation"] + 2)
            with self.assertRaises(ValidationError):
                self.os.integrations.inbox._assert_credential_fence(
                    before["id"], before["generation"]
                )
        finally:
            environment.environ.pop("AUREMGRID_TEST_SLACK_TOKEN_NEXT", None)

    def test_worker_honors_provider_retry_after(self) -> None:
        integration = self._configured()
        state={"limited":True}
        def factory(mode,*_args):
            if mode=="verify":
                return {"account_id":"T1","account_name":"Expected",
                        "granted_permissions":["channels:history"]}
            if state["limited"]:
                raise ConnectorTransportError("rate limit", status=429, retryable=True, retry_after=120)
            return [],'{"oldest":"1","page_cursor":null}'

        self.os.integrations.connector_factory = factory
        self.os.integrations.verify(self.identity,integration["id"])
        job = self.os.integrations.enqueue_sync(self.identity,integration["id"],max_attempts=3)[0]
        result = run_one_job(self.os, self.org.id, self.ws.id, "connector-worker")
        self.assertEqual(result["id"], job["id"])
        self.assertEqual(result["status"], "retry_wait")
        available = datetime.fromisoformat(result["available_at"])
        updated = datetime.fromisoformat(result["updated_at"])
        self.assertGreaterEqual((available - updated).total_seconds(), 120)
        current=self.os.integrations.get(self.identity, integration["id"])
        self.assertEqual(current["health"], "rate_limited")
        self.assertEqual(current["status"], "authorized")
        self.assertEqual(self.os.store.conn.execute(
            "SELECT COUNT(*) FROM connector_stream_locks WHERE job_id=? AND status='active'",(job["id"],)
        ).fetchone()[0],1)
        state["limited"]=False
        self.os.store.conn.execute(
            "UPDATE jobs SET available_at='2000-01-01T00:00:00+00:00' WHERE id=?",(job["id"],)
        )
        self.os.store.conn.commit()
        completed=run_one_job(self.os,self.org.id,self.ws.id,"connector-worker-retry")
        self.assertEqual(completed["status"],"succeeded")
        self.assertEqual(self.os.store.conn.execute(
            "SELECT COUNT(*) FROM connector_stream_locks WHERE job_id=? AND status='active'",(job["id"],)
        ).fetchone()[0],0)

    def test_worker_executes_exact_snapshotted_stream(self) -> None:
        integration=self._configured()
        def factory(mode,source,secret,*args):
            if mode=="verify":
                return {"account_id":"T1","account_name":"Expected",
                        "granted_permissions":["channels:history"]}
            return [ConnectorSourceEvent(
                "event-1","event-1","upsert","slack:C1:event-1","slack://C1/event-1",
                "# Update\n\nDone",{"connector":"slack"}
            )],'{"oldest":"1","page_cursor":null}'
        self.os.integrations.connector_factory=factory
        self.os.integrations.verify(self.identity,integration["id"])
        job=self.os.integrations.enqueue_sync(self.identity,integration["id"])[0]
        result=run_one_job(self.os,self.org.id,self.ws.id,"connector-worker-success")
        self.assertEqual(result["id"],job["id"])
        self.assertEqual(result["status"],"succeeded")
        self.assertEqual(result["result"]["created"],1)
        self.assertEqual(self.os.store.conn.execute(
            "SELECT COUNT(*) FROM connector_stream_locks WHERE job_id=? AND status='active'",(job["id"],)
        ).fetchone()[0],0)

    def test_connected_requires_every_mapped_stream_to_complete(self) -> None:
        integration=self.os.integrations.configure(
            self.identity,"slack","T1",{"C1":self.ws.id,"C2":self.ws.id},["channels:history"]
        )
        self.os.integrations.bind_credential(
            self.identity,integration["id"],"Slack read token","env:AUREMGRID_TEST_SLACK_TOKEN",
            ["connector:slack","channels:history"],
        )
        def factory(mode,source,secret,*args):
            if mode=="verify":
                return {"account_id":"T1","account_name":"Expected",
                        "granted_permissions":["channels:history"]}
            external_key=args[0]
            return [ConnectorSourceEvent(
                f"{external_key}:1",f"{external_key}:1","upsert",f"slack:{external_key}:1",
                f"slack://{external_key}/1",f"# {external_key}",{"connector":"slack"}
            )],f'{{"oldest":"{external_key}","page_cursor":null}}'
        self.os.integrations.connector_factory=factory
        self.os.integrations.verify(self.identity,integration["id"])
        jobs=self.os.integrations.enqueue_sync(self.identity,integration["id"])
        self.assertEqual(len(jobs),2)
        run_one_job(self.os,self.org.id,self.ws.id,"worker-partial")
        partial=self.os.integrations.get(self.identity,integration["id"])
        self.assertEqual(partial["status"],"authorized")
        self.assertEqual(partial["health"],"partial")
        run_one_job(self.os,self.org.id,self.ws.id,"worker-complete")
        complete=self.os.integrations.get(self.identity,integration["id"])
        self.assertEqual(complete["status"],"connected")
        self.assertEqual(complete["health"],"healthy")

    def test_one_sync_job_finishes_two_provider_pages_before_healthy(self) -> None:
        integration=self._configured(); pages=[]
        def factory(mode,source,secret,*args):
            if mode=="verify":
                return {"account_id":"T1","account_name":"Expected",
                        "granted_permissions":["channels:history"]}
            cursor=args[2]; pages.append(cursor)
            number=len(pages)
            return [ConnectorSourceEvent(
                f"page-{number}",f"page-{number}","upsert",f"slack:C1:{number}",
                f"slack://C1/{number}",f"# Page {number}",{"connector":"slack"}
            )],("cursor-1" if number==1 else '{"oldest":"2","page_cursor":null}'),number==1
        self.os.integrations.connector_factory=factory
        self.os.integrations.verify(self.identity,integration["id"])
        result=self.os.integrations.sync(self.identity,integration["id"])
        self.assertEqual(pages,[None,"cursor-1"])
        self.assertEqual(result["seen"],2)
        self.assertEqual(len(result["batch_ids"]),2)
        self.assertFalse(result["backfill_remaining"])
        self.assertEqual(self.os.integrations.get(self.identity,integration["id"])["health"],"healthy")

    def test_bounded_backfill_does_not_claim_healthy_while_pages_remain(self) -> None:
        integration=self._configured(); pages=[]
        def factory(mode,source,secret,*args):
            if mode=="verify":
                return {"account_id":"T1","account_name":"Expected",
                        "granted_permissions":["channels:history"]}
            pages.append(args[2])
            return [],f"cursor-{len(pages)}",True
        self.os.integrations.connector_factory=factory
        self.os.integrations.verify(self.identity,integration["id"])
        result=self.os.integrations.sync(self.identity,integration["id"])
        self.assertEqual(len(pages),20)
        self.assertTrue(result["backfill_remaining"])
        current=self.os.integrations.get(self.identity,integration["id"])
        self.assertEqual(current["status"],"authorized")
        self.assertEqual(current["health"],"backfilling")

    def test_healthy_second_stream_cannot_mask_first_stream_backfill(self) -> None:
        integration=self.os.integrations.configure(
            self.identity,"slack","T1",{"C1":self.ws.id,"C2":self.ws.id},["channels:history"]
        )
        self.os.integrations.bind_credential(
            self.identity,integration["id"],"Slack read token","env:AUREMGRID_TEST_SLACK_TOKEN",
            ["connector:slack","channels:history"],
        )
        page={"C1":0}
        def factory(mode,source,secret,*args):
            if mode=="verify": return {"account_id":"T1","account_name":"Expected","granted_permissions":["channels:history"]}
            external=args[0]
            if external=="C1":
                page["C1"]+=1;return [],f"cursor-{page['C1']}",True
            return [],"{\"oldest\":\"1\",\"page_cursor\":null}",False
        self.os.integrations.connector_factory=factory
        self.os.integrations.verify(self.identity,integration["id"])
        self.os.integrations.sync(
            self.identity,integration["id"],"C1",self.ws.id,
            self.os.integrations._mapping_hash("slack","C1",self.ws.id),
        )
        self.os.integrations.sync(
            self.identity,integration["id"],"C2",self.ws.id,
            self.os.integrations._mapping_hash("slack","C2",self.ws.id),
        )
        current=self.os.integrations.get(self.identity,integration["id"])
        self.assertEqual(current["status"],"authorized")
        self.assertEqual(current["health"],"backfilling")

    def test_poison_event_quarantine_is_visible_as_degraded(self) -> None:
        integration=self._configured()
        def factory(mode,source,secret,*args):
            if mode=="verify":
                return {"account_id":"T1","account_name":"Expected",
                        "granted_permissions":["channels:history"]}
            return [ConnectorSourceEvent(
                "poison-1","poison-1","upsert","slack:C1:poison","slack://C1/poison",
                "# Poison",{"connector":"slack"}
            )],'{"oldest":"1","page_cursor":null}'
        self.os.integrations.connector_factory=factory
        self.os.integrations.verify(self.identity,integration["id"])
        original=self.os.ingest_text
        self.os.ingest_text=lambda *_args,**_kwargs: (_ for _ in ()).throw(ValidationError("poison"))
        try:
            for _ in range(2):
                with self.assertRaises(ConnectorTransportError):
                    self.os.integrations.sync(self.identity,integration["id"])
                self.os.store.conn.execute(
                    "UPDATE connector_source_events SET available_at='2000-01-01T00:00:00+00:00'"
                )
                self.os.store.conn.commit()
            result=self.os.integrations.sync(self.identity,integration["id"])
        finally:
            self.os.ingest_text=original
        self.assertEqual(result["quarantined"],1)
        current=self.os.integrations.get(self.identity,integration["id"])
        self.assertEqual(current["status"],"connected")
        self.assertEqual(current["health"],"degraded")

    def test_healthy_second_stream_cannot_mask_first_stream_quarantine(self) -> None:
        integration=self.os.integrations.configure(
            self.identity,"slack","T1",{"C1":self.ws.id,"C2":self.ws.id},["channels:history"]
        )
        self.os.integrations.bind_credential(
            self.identity,integration["id"],"Slack read token","env:AUREMGRID_TEST_SLACK_TOKEN",
            ["connector:slack","channels:history"],
        )
        def factory(mode,source,secret,*args):
            if mode=="verify":
                return {"account_id":"T1","account_name":"Expected",
                        "granted_permissions":["channels:history"]}
            external=args[0]
            return [ConnectorSourceEvent(
                f"{external}-event",f"{external}-event","upsert",f"slack:{external}:event",
                f"slack://{external}/event",f"# {external}",{"connector":"slack"}
            )],f'{{"oldest":"{external}","page_cursor":null}}',False
        self.os.integrations.connector_factory=factory
        self.os.integrations.verify(self.identity,integration["id"])
        original=self.os.ingest_text
        self.os.ingest_text=lambda *_args,**_kwargs: (_ for _ in ()).throw(ValidationError("poison"))
        hash_c1=self.os.integrations._mapping_hash("slack","C1",self.ws.id)
        try:
            for _ in range(2):
                with self.assertRaises(ConnectorTransportError):
                    self.os.integrations.sync(self.identity,integration["id"],"C1",self.ws.id,hash_c1)
                self.os.store.conn.execute(
                    "UPDATE connector_source_events SET available_at='2000-01-01T00:00:00+00:00'"
                );self.os.store.conn.commit()
            self.os.integrations.sync(self.identity,integration["id"],"C1",self.ws.id,hash_c1)
        finally:
            self.os.ingest_text=original
        hash_c2=self.os.integrations._mapping_hash("slack","C2",self.ws.id)
        self.os.integrations.sync(self.identity,integration["id"],"C2",self.ws.id,hash_c2)
        current=self.os.integrations.get(self.identity,integration["id"])
        self.assertEqual(current["status"],"connected")
        self.assertEqual(current["health"],"degraded")

    def test_mcp_connector_tools_use_the_trusted_identity(self) -> None:
        router = McpToolRouter(self.os, self.identity)
        names = {item["name"] for item in router.list_tools()}
        self.assertIn("integrations.sync", names)
        created = router.call("integrations.configure", {
            "organization_id": self.org.id,
            "source": "slack",
            "expected_account_id": "T1",
            "workspace_mappings": {"C1": self.ws.id},
            "permissions": ["channels:history"],
        })
        self.assertEqual(created["status"], "not_connected")
        forged = router.call("integrations.list", {"organization_id": "org_forged"})
        self.assertEqual(forged["error"], "AuthorizationError")

    def test_workspace_scoped_token_snapshots_stream_and_blocks_active_remap(self) -> None:
        integration=self._configured()
        self.os.store.conn.execute("UPDATE integrations SET status='authorized' WHERE id=?",(integration["id"],))
        self.os.store.conn.commit()
        issued=self.os.auth.create_api_token(self.identity.principal_id,"connector-only",["integration_sync"])
        restricted=self.os.auth.authenticate_api_token(issued["token"],self.org.id,self.ws.id)
        job=self.os.integrations.enqueue_sync(restricted,integration["id"])[0]
        self.assertEqual(job["workspace_id"],self.ws.id)
        self.assertEqual(job["payload"]["external_key"],"C1")
        self.assertEqual(job["payload"]["workspace_id"],self.ws.id)
        self.assertTrue(job["payload"]["mapping_hash"])
        other=self.os.create_organization_workspace(self.org.id,"Second client","client","ws_second_integrations")
        base=self.os.auth.identity_for_principal(self.identity.principal_id)
        with self.assertRaises(ValidationError):
            self.os.integrations.configure(base,"slack","T1",{"C1":other.id},["channels:history"])

    def test_cancelled_connector_job_releases_atomic_stream_reservation(self) -> None:
        integration=self._configured()
        self.os.store.conn.execute("UPDATE integrations SET status='authorized' WHERE id=?",(integration["id"],))
        self.os.store.conn.commit()
        job=self.os.integrations.enqueue_sync(self.identity,integration["id"])[0]
        cancelled=self.os.jobs.cancel_job(self.org.id,self.ws.id,job["id"],"operator cancelled")
        self.os.integrations.release_job_stream(job["id"])
        self.assertEqual(cancelled["status"],"cancelled")
        self.assertIsNone(self.os.integrations.inbox.active_stream_lock(
            self.org.id,self.ws.id,"slack",f"managed:{integration['id']}:{job['payload']['mapping_hash']}"
        ))
        replacement=self.os.integrations.enqueue_sync(self.identity,integration["id"])[0]
        self.assertNotEqual(replacement["id"],job["id"])

    def test_stale_stream_fence_cannot_write_after_worker_reclaim(self) -> None:
        integration=self._configured(); rotated=[]
        def factory(mode,source,secret,*args):
            if mode=="verify":
                return {"account_id":"T1","account_name":"Expected",
                        "granted_permissions":["channels:history"]}
            rotated.append(self.os.integrations.resume_job_stream(
                job["id"],"worker-new",job["payload"]["mapping_hash"]
            ))
            return [ConnectorSourceEvent(
                "stale-1","stale-1","upsert","slack:C1:stale","slack://C1/stale",
                "# Stale",{"connector":"slack"}
            )],"stale-cursor",False
        self.os.integrations.connector_factory=factory
        self.os.integrations.verify(self.identity,integration["id"])
        job=self.os.integrations.enqueue_sync(self.identity,integration["id"])[0]
        claimed=self.os.jobs.claim_job(self.org.id,self.ws.id,"worker-old")
        old_lock=self.os.integrations.resume_job_stream(
            job["id"],"worker-old",job["payload"]["mapping_hash"]
        )
        job_identity=self.os.auth.identity_for_principal(job["principal_id"],self.ws.id)
        with self.assertRaises(ValidationError):
            self.os.integrations.sync(
                job_identity,integration["id"],"C1",self.ws.id,job["payload"]["mapping_hash"],
                f"worker-old:{claimed['lease_token']}",None,old_lock["id"],old_lock["reservation_token"],
            )
        self.assertTrue(rotated)
        self.assertEqual(self.os.store.conn.execute(
            "SELECT COUNT(*) FROM connector_ingest_batches"
        ).fetchone()[0],0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],0)

    def test_multi_stream_enqueue_rolls_back_every_new_job_on_late_conflict(self) -> None:
        integration=self.os.integrations.configure(
            self.identity,"slack","T1",{"C1":self.ws.id,"C2":self.ws.id},["channels:history"]
        )
        self.os.store.conn.execute("UPDATE integrations SET status='authorized' WHERE id=?",(integration["id"],))
        self.os.store.conn.commit()
        hash_c2=self.os.integrations._mapping_hash("slack","C2",self.ws.id)
        existing=self.os.jobs.enqueue_job(
            self.org.id,self.ws.id,self.identity.principal_id,"connector.sync",
            {"integration_id":integration["id"],"external_key":"C2","workspace_id":self.ws.id,"mapping_hash":hash_c2},
        )
        self.os.integrations.inbox.reserve_stream(
            self.org.id,self.ws.id,"slack",f"{integration['id']}:{hash_c2}",
            f"managed:{integration['id']}:{hash_c2}",existing["id"],hash_c2,self.identity.principal_id,
            lease_seconds=604800,
        )
        with self.assertRaises(ValidationError):
            self.os.integrations.enqueue_sync(self.identity,integration["id"])
        self.assertEqual(self.os.store.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type='connector.sync'"
        ).fetchone()[0],1)
        self.assertEqual(self.os.store.conn.execute(
            "SELECT COUNT(*) FROM connector_stream_locks WHERE status='active'"
        ).fetchone()[0],1)


if __name__ == "__main__":
    unittest.main()
