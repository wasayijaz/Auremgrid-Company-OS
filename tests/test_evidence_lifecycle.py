from __future__ import annotations

import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from auremgrid.connectors.google_auth import ConnectorInboxRepository, ConnectorSourceEvent
from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS, new_id


class EvidenceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Lifecycle Co")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client")
        self.other_ws = self.os.create_organization_workspace(self.org.id, "Other", "client")
        self.person = self.os.create_person(self.org.id, "Owner", "owner@example.test", role="admin")
        self.actor = self.os.create_actor(self.ws.id, "Owner", "admin")
        self.other_actor = self.os.create_actor(self.other_ws.id, "Owner", "admin")

    def tearDown(self) -> None:
        self.os.close()

    def _ingest(self, key: str, content: str, workspace_id: str | None = None, actor_id: str | None = None):
        return self.os.ingest_text(
            workspace_id or self.ws.id,
            actor_id or self.actor.id,
            key,
            content,
            f"memory://{key}",
        ).source

    def test_schema_13_has_durable_lifecycle_route_and_queue_tables(self) -> None:
        self.assertEqual(self.os.store.schema_version, 14)
        names = {row["name"] for row in self.os.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({
            "source_lifecycle_intervals", "provider_object_routes", "provider_object_route_events",
            "provider_object_ancestry", "provider_route_mutation_staging", "provider_sync_tasks",
            "provider_sync_generations", "provider_object_generation_seen",
        } <= names)

    def test_retirement_hides_current_search_but_as_of_retains_evidence(self) -> None:
        source = self._ingest("brief", "Obsidian launch phrase")
        retirement = source.recorded_at + timedelta(seconds=2)
        self.assertTrue(self.os.store.retire_source(self.ws.id, source.id, retirement, "provider_deleted"))
        self.assertFalse(self.os.store.retire_source(self.ws.id, source.id, retirement, "provider_deleted"))
        self.assertTrue(self.os.search(
            self.ws.id, self.actor.id, "Obsidian launch", as_of=retirement + timedelta(seconds=1)
        ).unknown)
        historical = self.os.search(
            self.ws.id, self.actor.id, "Obsidian launch", as_of=source.recorded_at
        )
        self.assertFalse(historical.unknown)
        self.assertEqual(historical.items[0].citation.source_id, source.id)

    def test_new_source_version_is_only_current_version(self) -> None:
        old = self._ingest("plan", "Legacy amber positioning")
        new = self._ingest("plan", "Current cobalt positioning")
        self.assertFalse(self.os.store.source_is_active(self.ws.id, old.id))
        self.assertTrue(self.os.store.source_is_active(self.ws.id, new.id))
        self.assertTrue(self.os.search(self.ws.id, self.actor.id, "Legacy amber").unknown)
        self.assertFalse(self.os.search(self.ws.id, self.actor.id, "Current cobalt").unknown)
        intervals = self.os.store.conn.execute(
            "SELECT * FROM source_lifecycle_intervals WHERE workspace_id=? ORDER BY activated_at",
            (self.ws.id,),
        ).fetchall()
        self.assertEqual(sum(row["retired_at"] is None for row in intervals), 1)

    def test_two_routes_share_current_evidence_until_last_route_retires(self) -> None:
        source = self._ingest("gmail/messages/m1", "Joint label evidence")
        when = source.recorded_at + timedelta(seconds=1)
        for label in ("label:A", "label:B"):
            self.os.store.activate_provider_route(
                self.ws.id, "gmail", "acct", "m1", label, source.source_key,
                source.id, "100:add", when,
            )
        self.os.store.retire_provider_route(
            self.ws.id, "gmail", "acct", "m1", "label:A", source.source_key,
            "101:remove-A", when + timedelta(seconds=1),
        )
        self.assertTrue(self.os.store.source_is_active(self.ws.id, source.id))
        self.os.store.retire_provider_route(
            self.ws.id, "gmail", "acct", "m1", "label:B", source.source_key,
            "102:remove-B", when + timedelta(seconds=2),
        )
        self.assertFalse(self.os.store.source_is_active(self.ws.id, source.id))

    def test_new_object_version_repoints_all_active_routes(self) -> None:
        old = self._ingest("gmail/messages/m2", "Old routed evidence")
        when = old.recorded_at + timedelta(seconds=1)
        for label in ("label:A", "label:B"):
            self.os.store.activate_provider_route(
                self.ws.id, "gmail", "acct", "m2", label, old.source_key, old.id, "1", when
            )
        new = self._ingest("gmail/messages/m2", "New routed evidence")
        self.os.store.activate_provider_route(
            self.ws.id, "gmail", "acct", "m2", "label:A", new.source_key, new.id,
            "2", when + timedelta(seconds=2),
        )
        route_sources = {
            row["active_source_id"] for row in self.os.store.conn.execute(
                "SELECT active_source_id FROM provider_object_routes WHERE external_id='m2' AND status='active'"
            )
        }
        self.assertEqual(route_sources, {new.id})
        self.assertFalse(self.os.store.source_is_active(self.ws.id, old.id))

    def test_route_mutations_are_workspace_scoped(self) -> None:
        source = self._ingest("drive/files/f1", "Scoped evidence")
        with self.assertRaises(ValidationError):
            self.os.store.activate_provider_route(
                self.other_ws.id, "google_drive", "acct", "f1", "folder:root",
                source.source_key, source.id, "1",
            )
        self.assertEqual(self.os.store.provider_route_state(self.other_ws.id, "google_drive", "acct"), {})

    def test_nested_ancestry_inherits_root_and_unknown_parent_never_retires(self) -> None:
        root = self.os.store.resolve_provider_object_routes(
            self.ws.id, "google_drive", "acct", "root", "1",
            direct_route_keys=("folder:root",), is_container=True,
        )
        self.assertEqual(root["route_keys"], ("folder:root",))
        nested = self.os.store.resolve_provider_object_routes(
            self.ws.id, "google_drive", "acct", "nested", "2",
            parent_ids=("root",), is_container=True,
        )
        self.assertEqual(nested["route_keys"], ("folder:root",))
        child = self.os.store.resolve_provider_object_routes(
            self.ws.id, "google_drive", "acct", "file", "3", parent_ids=("nested",)
        )
        self.assertEqual(child["route_keys"], ("folder:root",))
        unresolved = self.os.store.resolve_provider_object_routes(
            self.ws.id, "google_drive", "acct", "file", "4", parent_ids=("unknown",)
        )
        self.assertEqual(unresolved["route_keys"], ("folder:root",))
        self.assertTrue(unresolved["reconciliation_required"])
        self.assertFalse(unresolved["may_retire"])

    def test_moved_folder_queues_descendant_reconciliation_contract(self) -> None:
        for root_id in ("root-a", "root-b"):
            self.os.store.resolve_provider_object_routes(
                self.ws.id, "google_drive", "acct", root_id, "1",
                direct_route_keys=(f"folder:{root_id}",), is_container=True,
            )
        self.os.store.resolve_provider_object_routes(
            self.ws.id, "google_drive", "acct", "nested", "2",
            parent_ids=("root-a",), is_container=True,
        )
        moved = self.os.store.resolve_provider_object_routes(
            self.ws.id, "google_drive", "acct", "nested", "3",
            parent_ids=("root-b",), is_container=True,
        )
        self.assertEqual(moved["route_keys"], ("folder:root-b",))
        self.assertTrue(moved["descendant_reconciliation_required"])
        queued = self.os.store.objects_requiring_reconciliation(self.ws.id, "google_drive", "acct")
        self.assertEqual(queued[0]["external_id"], "nested")
        self.assertTrue(self.os.store.acknowledge_descendant_reconciliation(
            self.ws.id, "google_drive", "acct", "nested"
        ))

    def test_projection_rebuild_excludes_retired_documents(self) -> None:
        active = self._ingest("active", "Active projection text")
        retired = self._ingest("retired", "Retired projection text")
        self.os.store.retire_source(self.ws.id, retired.id, retired.recorded_at + timedelta(seconds=1))
        result = self.os.rebuild_projections(self.ws.id)
        self.assertEqual(result["documents"], 1)
        self.assertIn(next(iter(self.os.store.conn.execute(
            "SELECT id FROM documents WHERE source_id=?", (active.id,)
        )))[0], self.os._embeddings)
        retired_doc = self.os.store.conn.execute("SELECT id FROM documents WHERE source_id=?", (retired.id,)).fetchone()[0]
        self.assertNotIn(retired_doc, self.os._embeddings)

    def test_generation_seen_markers_retire_only_unseen_route_objects(self) -> None:
        first = self._ingest("drive/files/first", "First generation object")
        second = self._ingest("drive/files/second", "Second generation object")
        for external_id, source in (("first", first), ("second", second)):
            self.os.store.activate_provider_route(
                self.ws.id, "google_drive", "acct", external_id, "folder:root",
                source.source_key, source.id, "v1",
            )
        generation = self.os.store.start_provider_sync_generation(
            self.ws.id, "google_drive", "acct", "stream", "folder:root", "baseline"
        )
        self.assertTrue(self.os.store.mark_provider_object_seen(generation["id"], "first", "folder:root"))
        completed = self.os.store.complete_provider_sync_generation(generation["id"])
        self.assertEqual(completed["retired"], 1)
        self.assertTrue(self.os.store.source_is_active(self.ws.id, first.id))
        self.assertFalse(self.os.store.source_is_active(self.ws.id, second.id))

    def test_staged_route_mutation_is_event_linked_and_idempotent(self) -> None:
        source = self._ingest("drive/files/staged", "Staged mutation")
        repo = ConnectorInboxRepository(self.os.store.conn, new_id)
        event = ConnectorSourceEvent(
            "dedupe-stage", "staged", "file_changed", source.source_key,
            "memory://staged", "Staged mutation", {},
        )
        batch = repo.record_pull(
            self.org.id, self.ws.id, "google_drive", "acct", None, "cursor", [event]
        )
        repo.mark_event_ingested(batch["events"][0]["id"])
        mutation = self.os.store.stage_provider_route_mutation(
            batch["id"], batch["events"][0]["id"], self.ws.id, "google_drive", "acct",
            "staged", "folder:root", source.source_key, source.id, "v1", "activate",
            source.recorded_at,
        )
        duplicate = self.os.store.stage_provider_route_mutation(
            batch["id"], batch["events"][0]["id"], self.ws.id, "google_drive", "acct",
            "staged", "folder:root", source.source_key, source.id, "v1", "activate",
            source.recorded_at,
        )
        self.assertEqual(mutation["id"], duplicate["id"])
        self.assertEqual(len(self.os.store.apply_staged_provider_route_mutations(batch["id"])), 1)
        self.assertEqual(self.os.store.apply_staged_provider_route_mutations(batch["id"]), [])

    def test_late_arriving_earlier_version_becomes_current_without_corrupting_as_of_order(self) -> None:
        later = self.os.ingest_text(
            self.ws.id, self.actor.id, "timeline", "META: valid_from=2026-06-01T00:00:00+00:00\nFACT: Offer | state | later",
            "memory://timeline", observed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ).source
        earlier = self.os.ingest_text(
            self.ws.id, self.actor.id, "timeline", "META: valid_from=2026-05-01T00:00:00+00:00\nFACT: Offer | state | late-arriving-earlier",
            "memory://timeline", observed_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ).source
        self.assertFalse(self.os.store.source_is_active(self.ws.id, later.id))
        self.assertTrue(self.os.store.source_is_active(self.ws.id, earlier.id))
        current = self.os.search(self.ws.id, self.actor.id, "late-arriving-earlier")
        self.assertFalse(current.unknown)
        semantic = self.os.search(
            self.ws.id, self.actor.id, "Offer state", as_of=datetime(2026, 7, 1, tzinfo=timezone.utc)
        )
        self.assertIn("later", {item.payload.get("object") for item in semantic.items if item.kind == "fact"})
        semantic_time = datetime(2026, 7, 1, tzinfo=timezone.utc)
        self.assertTrue(self.os.store.source_is_active(self.ws.id, later.id, semantic_time))
        self.assertFalse(self.os.store.source_is_active(self.ws.id, earlier.id, semantic_time))

    def test_reingesting_identical_retired_content_switches_single_current_version(self) -> None:
        first = self._ingest("repeat", "First exact body")
        second = self._ingest("repeat", "Second replacement body")
        replay = self._ingest("repeat", "First exact body")
        self.assertEqual(replay.id, first.id)
        self.assertTrue(self.os.store.source_is_active(self.ws.id, first.id))
        self.assertFalse(self.os.store.source_is_active(self.ws.id, second.id))
        current = self.os.store.conn.execute(
            """SELECT source_id FROM source_lifecycle_intervals
               WHERE workspace_id=? AND source_key='repeat' AND retired_at IS NULL""",
            (self.ws.id,),
        ).fetchall()
        self.assertEqual([row["source_id"] for row in current], [first.id])

    def test_failed_downstream_storage_keeps_old_current_and_success_supersedes_old_fact(self) -> None:
        old = self.os.ingest_text(
            self.ws.id, self.actor.id, "atomic", "META: valid_from=2026-01-01T00:00:00+00:00\nFACT: Plan | price | 100 USD",
            "memory://atomic", observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ).source
        replacement = (
            "META: valid_from=2026-02-01T00:00:00+00:00\n"
            "FACT: Plan | price | 200 USD\n"
            "REL: Plan | owned_by | Finance"
        )
        with patch.object(self.os.store, "create_relation", side_effect=RuntimeError("downstream failed")):
            with self.assertRaises(RuntimeError):
                self.os.ingest_text(
                    self.ws.id, self.actor.id, "atomic", replacement, "memory://atomic",
                    observed_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
                )
        self.assertTrue(self.os.store.source_is_active(self.ws.id, old.id))
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM sources WHERE workspace_id=? AND source_key='atomic'", (self.ws.id,)).fetchone()[0],
            1,
        )
        old_fact = self.os.store.conn.execute("SELECT * FROM facts WHERE source_id=?", (old.id,)).fetchone()
        self.assertIsNone(old_fact["superseded_by"])
        successful = self.os.ingest_text(
            self.ws.id, self.actor.id, "atomic", replacement, "memory://atomic",
            observed_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        old_fact = self.os.store.conn.execute("SELECT * FROM facts WHERE source_id=?", (old.id,)).fetchone()
        self.assertIn(old_fact["superseded_by"], successful.fact_ids)

    def test_closed_lifecycle_history_cannot_be_rewritten_by_direct_sql(self) -> None:
        source = self._ingest("immutable-close", "Immutable lifecycle evidence")
        closed_at = source.recorded_at + timedelta(seconds=1)
        self.os.store.retire_source(self.ws.id, source.id, closed_at, "first_close")
        interval = self.os.store.conn.execute(
            "SELECT * FROM source_lifecycle_intervals WHERE workspace_id=? AND source_id=?",
            (self.ws.id, source.id),
        ).fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.os.store.conn.execute(
                "UPDATE source_lifecycle_intervals SET retired_at=?,retirement_reason=? WHERE id=?",
                ((closed_at + timedelta(seconds=1)).isoformat(), "rewrite", interval["id"]),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.os.store.conn.execute(
                "UPDATE source_lifecycle_intervals SET effective_until=? WHERE id=?",
                ((closed_at + timedelta(seconds=2)).isoformat(), interval["id"]),
            )
        unchanged = self.os.store.conn.execute(
            "SELECT * FROM source_lifecycle_intervals WHERE id=?", (interval["id"],)
        ).fetchone()
        self.assertEqual(unchanged["retired_at"], closed_at.isoformat())
        self.assertEqual(unchanged["retirement_reason"], "first_close")

    def test_source_is_active_as_of_selects_latest_effective_version(self) -> None:
        first = self.os.ingest_text(
            self.ws.id, self.actor.id, "effective", "First effective version",
            "memory://effective", observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ).source
        second = self.os.ingest_text(
            self.ws.id, self.actor.id, "effective", "Second effective version",
            "memory://effective", observed_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        ).source
        march = datetime(2026, 3, 1, tzinfo=timezone.utc)
        self.assertFalse(self.os.store.source_is_active(self.ws.id, first.id, march))
        self.assertTrue(self.os.store.source_is_active(self.ws.id, second.id, march))


if __name__ == "__main__":
    unittest.main()
