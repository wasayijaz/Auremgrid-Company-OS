from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from auremgrid.adapters.graphiti_upstream import (
    UnavailableGraphitiProjection,
    UpstreamGraphitiProjection,
    episode_id,
)
from auremgrid.services.brain import CompanyOS
from tests.auth_support import LATEST_SCHEMA_VERSION


class FakeGraphiti:
    """SDK-faithful fake: create rejects uuid and returns a provider UUID."""

    def __init__(self, *, fail_adds: int = 0) -> None:
        self.added: list[dict] = []
        self.groups: list[dict] = []
        self.fail_adds = fail_adds
        self.next_uuid = 1
        self.remote_by_name: dict[tuple[str, str], list[str]] = {}
        self.build_calls = 0

    async def build_indices_and_constraints(self):
        self.build_calls += 1
        return None

    async def add_episode(self, **kwargs):
        if "uuid" in kwargs:
            raise AssertionError("Graphiti create must not receive uuid")
        if self.fail_adds:
            self.fail_adds -= 1
            raise RuntimeError("provider unavailable")
        self.added.append(kwargs)
        remote_uuid = f"remote-{self.next_uuid}"
        self.next_uuid += 1
        self.remote_by_name.setdefault((kwargs["name"], kwargs["group_id"]), []).append(remote_uuid)
        return {"episode": {"uuid": remote_uuid}}

    async def find_episode_by_name(self, name: str, group_id: str):
        return [{"uuid": value} for value in self.remote_by_name.get((name, group_id), [])]

    async def search(self, **kwargs):
        self.groups.append(kwargs)
        group_id = kwargs["group_ids"][0]
        remote = [
            uuid
            for (_name, group), uuids in self.remote_by_name.items()
            if group == group_id
            for uuid in uuids
        ]
        return [{"episodes": [{"uuid": remote[0]}]}] if remote else []


class _EagerResult:
    def __init__(self, records):
        self.records = records


class _DriverOnlyGraphiti:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.graph_driver = self

    async def execute_query(self, query: str, **kwargs):
        self.calls.append((query, kwargs))
        return _EagerResult([{"uuid": "remote-existing"}])


class UpstreamGraphitiTests(unittest.TestCase):
    def _build(self, database: str | Path = ":memory:", *, fake: FakeGraphiti | None = None):
        fake = fake or FakeGraphiti()
        graph = UpstreamGraphitiProjection(client=fake)
        os = CompanyOS(database, graph_projection=graph)
        org = os.create_organization("Agency")
        ws = os.create_organization_workspace(org.id, "Client", "client")
        person = os.create_person(org.id, "Owner", role="owner")
        os.add_person_to_workspace(org.id, ws.id, person.id, "admin")
        actor = os.create_actor(ws.id, "Admin", "admin")
        result = os.ingest_text(ws.id, actor.id, "source", "launch", "memory://launch")
        os.rebuild_projections()
        return os, graph, fake, org, ws, actor, result

    def test_create_uses_deterministic_name_but_provider_generated_uuid(self) -> None:
        os, graph, fake, _org, ws, _actor, result = self._build()
        document = os.store.get_document(ws.id, result.document_id)
        generation = str(os.store.graph_generation_state(ws.id)["active_generation"])
        expected = episode_id(
            ws.id, generation, result.source.id, "launch",
            document.observed_at.isoformat(), document.id,
        )
        self.assertNotIn("uuid", fake.added[0])
        self.assertEqual(fake.added[0]["name"], expected)
        mapping = os.store.get_graphiti_episode_mapping(expected)
        self.assertEqual(mapping["remote_episode_uuid"], "remote-1")
        hits = graph.search(ws.id, "launch", [result.source.id], generation=generation)
        self.assertEqual(hits[0]["document_id"], result.document_id)
        self.assertEqual(fake.groups[0]["group_ids"][0], graph._group(ws.id, generation))
        os.close()

    def test_exact_driver_lookup_recovers_eager_result_by_name_and_group(self) -> None:
        client = _DriverOnlyGraphiti()
        graph = UpstreamGraphitiProjection(client=client)
        self.assertEqual(
            graph._find_episode_by_name("episode-key", "generation-group"),
            ["remote-existing"],
        )
        query, parameters = client.calls[0]
        self.assertIn("MATCH (e:Episodic {name: $name, group_id: $group_id})", query)
        self.assertIn("LIMIT 2", query)
        self.assertEqual(
            parameters,
            {"name": "episode-key", "group_id": "generation-group", "routing_": "r"},
        )

    def test_unknown_disallowed_and_historical_provider_refs_are_dropped(self) -> None:
        os, graph, fake, _org, ws, _actor, result = self._build()
        generation = str(os.store.graph_generation_state(ws.id)["active_generation"])
        fake.search = lambda **kwargs: [{"episodes": [{"uuid": "forged"}]}]
        self.assertEqual(graph.search(ws.id, "launch", [result.source.id], generation=generation), [])
        self.assertEqual(graph.search(ws.id, "launch", [], generation=generation), [])
        calls = len(fake.groups)
        self.assertEqual(graph.search(
            ws.id, "launch", [result.source.id], datetime.now(timezone.utc), generation=generation
        ), [])
        self.assertEqual(len(fake.groups), calls)
        os.close()

    def test_provider_failure_isolated_then_retry_and_idempotent_replay(self) -> None:
        fake = FakeGraphiti(fail_adds=1)
        graph = UpstreamGraphitiProjection(client=fake)
        os = CompanyOS(":memory:", graph_projection=graph)
        org = os.create_organization("Agency")
        ws = os.create_organization_workspace(org.id, "Client", "client")
        person = os.create_person(org.id, "Owner", role="owner")
        os.add_person_to_workspace(org.id, ws.id, person.id, "admin")
        actor = os.create_actor(ws.id, "Admin", "admin")
        result = os.ingest_text(ws.id, actor.id, "source", "launch", "memory://launch")
        self.assertIsNotNone(os.store.get_document(ws.id, result.document_id))
        os.rebuild_projections()
        self.assertEqual(fake.added, [])
        os.rebuild_projections()
        self.assertEqual(len(fake.added), 1)
        generation = str(os.store.graph_generation_state(ws.id)["active_generation"])
        document = os.store.get_document(ws.id, result.document_id)
        graph.upsert_episode(
            ws.id, result.source.id, document.content, document.observed_at.isoformat(),
            generation, document.id, document.recorded_at.isoformat(),
        )
        self.assertEqual(len(fake.added), 1)
        os.close()

    def test_restart_restores_remote_uuid_from_sidecar_without_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.sqlite"
            first, _graph, first_fake, _org, ws, _actor, result = self._build(path)
            generation = str(first.store.graph_generation_state(ws.id)["active_generation"])
            first.close()
            second_fake = FakeGraphiti()
            restarted = CompanyOS(path, graph_projection=UpstreamGraphitiProjection(client=second_fake))
            second_fake.search = lambda **kwargs: [{"episodes": [{"uuid": "remote-1"}]}]
            hits = restarted.graph.search(ws.id, "launch", [result.source.id], generation=generation)
            self.assertEqual(hits[0]["document_id"], result.document_id)
            self.assertEqual(second_fake.added, [])
            self.assertEqual(len(first_fake.added), 1)
            restarted.close()

    def test_local_generation_is_rebuilt_when_configured_upstream_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.sqlite"
            local = CompanyOS(path)
            org = local.create_organization("Agency")
            ws = local.create_organization_workspace(org.id, "Client", "client")
            person = local.create_person(org.id, "Owner", role="owner")
            local.add_person_to_workspace(org.id, ws.id, person.id, "admin")
            actor = local.create_actor(ws.id, "Admin", "admin")
            ingested = local.ingest_text(
                ws.id, actor.id, "launch", "canonical launch", "memory://launch"
            )
            local.rebuild_projections()
            local_generation = local.store.graph_generation_state(ws.id)["active_generation"]
            local.close()

            fake = FakeGraphiti()
            restarted = CompanyOS(path, graph_projection=UpstreamGraphitiProjection(client=fake))
            upstream_generation = restarted.store.graph_generation_state(ws.id)["active_generation"]
            self.assertNotEqual(upstream_generation, local_generation)
            self.assertEqual(len(fake.added), 1)
            self.assertEqual(
                len(restarted.store.list_graphiti_episode_mappings(ws.id, upstream_generation)), 1
            )
            hits = restarted.graph.search(
                ws.id, "launch", [ingested.source.id], generation=upstream_generation
            )
            self.assertEqual(hits[0]["document_id"], ingested.document_id)
            self.assertEqual(restarted.graph_health["status"], "healthy")
            restarted.close()

    def test_empty_workspace_starts_healthy_without_remote_episode_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.sqlite"
            local = CompanyOS(path)
            org = local.create_organization("Agency")
            ws = local.create_organization_workspace(org.id, "Empty", "client")
            local.close()

            fake = FakeGraphiti()
            restarted = CompanyOS(path, graph_projection=UpstreamGraphitiProjection(client=fake))
            self.assertEqual(fake.added, [])
            self.assertEqual(fake.build_calls, 0)
            self.assertIsNotNone(
                restarted.store.graph_generation_state(ws.id)["active_generation"]
            )
            self.assertEqual(restarted.graph_health["status"], "healthy")
            restarted.close()

    def test_startup_completeness_rebuilds_each_workspace_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.sqlite"
            local = CompanyOS(path)
            org = local.create_organization("Agency")
            person = local.create_person(org.id, "Owner", role="owner")
            workspace_ids: list[str] = []
            old_generations: dict[str, str] = {}
            for index in range(2):
                ws = local.create_organization_workspace(org.id, f"Client {index}", "client")
                local.add_person_to_workspace(org.id, ws.id, person.id, "admin")
                actor = local.create_actor(ws.id, f"Admin {index}", "admin")
                local.ingest_text(
                    ws.id, actor.id, f"source-{index}", f"launch {index}",
                    f"memory://launch-{index}",
                )
                workspace_ids.append(ws.id)
            local.rebuild_projections()
            for workspace_id in workspace_ids:
                old_generations[workspace_id] = str(
                    local.store.graph_generation_state(workspace_id)["active_generation"]
                )
            local.close()

            fake = FakeGraphiti()
            restarted = CompanyOS(path, graph_projection=UpstreamGraphitiProjection(client=fake))
            self.assertEqual(len(fake.added), 2)
            for workspace_id in workspace_ids:
                active = str(
                    restarted.store.graph_generation_state(workspace_id)["active_generation"]
                )
                self.assertNotEqual(active, old_generations[workspace_id])
                self.assertEqual(
                    len(restarted.store.list_graphiti_episode_mappings(workspace_id, active)), 1
                )
            restarted.close()

    def test_startup_provider_outage_preserves_local_active_and_is_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.sqlite"
            local = CompanyOS(path)
            org = local.create_organization("Agency")
            ws = local.create_organization_workspace(org.id, "Client", "client")
            person = local.create_person(org.id, "Owner", role="owner")
            local.add_person_to_workspace(org.id, ws.id, person.id, "admin")
            actor = local.create_actor(ws.id, "Admin", "admin")
            local.ingest_text(ws.id, actor.id, "launch", "canonical launch", "memory://launch")
            local.rebuild_projections()
            old_generation = local.store.graph_generation_state(ws.id)["active_generation"]
            local.close()

            fake = FakeGraphiti(fail_adds=1)
            restarted = CompanyOS(path, graph_projection=UpstreamGraphitiProjection(client=fake))
            self.assertEqual(
                restarted.store.graph_generation_state(ws.id)["active_generation"], old_generation
            )
            self.assertEqual(restarted.graph_health["status"], "degraded")
            self.assertEqual(restarted.graph.health()["status"], "degraded")
            restarted.close()

    def test_unavailable_projection_cannot_promote_existing_generation_to_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.sqlite"
            local = CompanyOS(path)
            org = local.create_organization("Agency")
            ws = local.create_organization_workspace(org.id, "Client", "client")
            local.rebuild_projections()
            active = local.store.graph_generation_state(ws.id)["active_generation"]
            local.close()

            restarted = CompanyOS(
                path, graph_projection=UnavailableGraphitiProjection("missing dependency")
            )
            self.assertEqual(
                restarted.store.graph_generation_state(ws.id)["active_generation"], active
            )
            self.assertEqual(restarted.graph_health["status"], "unavailable")
            self.assertEqual(restarted.graph.health()["status"], "unavailable")
            restarted.close()

    def test_mapping_write_failure_recovers_exact_remote_episode_without_duplicate(self) -> None:
        os, graph, fake, _org, ws, actor, _result = self._build()
        original = os.store.record_graphiti_episode_mapping
        failures = {"remaining": 1}

        def fail_once(**kwargs):
            if failures["remaining"]:
                failures["remaining"] -= 1
                raise sqlite3.OperationalError("simulated crash before sidecar commit")
            return original(**kwargs)

        os.store.record_graphiti_episode_mapping = fail_once
        second = os.ingest_text(ws.id, actor.id, "second", "second launch", "memory://second")
        document = os.store.get_document(ws.id, second.document_id)
        generation = str(os.store.graph_generation_state(ws.id)["active_generation"])
        key = episode_id(
            ws.id, generation, second.source.id, document.content,
            document.observed_at.isoformat(), document.id,
        )
        self.assertIsNone(os.store.get_graphiti_episode_mapping(key))
        self.assertEqual(len(fake.remote_by_name[(key, graph._group(ws.id, generation))]), 1)
        graph.upsert_episode(
            ws.id, second.source.id, document.content, document.observed_at.isoformat(),
            generation, document.id, document.recorded_at.isoformat(),
        )
        self.assertEqual(len(fake.remote_by_name[(key, graph._group(ws.id, generation))]), 1)
        self.assertIsNotNone(os.store.get_graphiti_episode_mapping(key))
        os.close()

    def test_sidecar_is_append_only_scope_checked_and_migration_replay_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.sqlite"
            os, _graph, _fake, _org, _ws, _actor, _result = self._build(path)
            mapping = dict(os.store.conn.execute("SELECT * FROM graphiti_episode_mappings").fetchone())
            with self.assertRaises(sqlite3.IntegrityError):
                os.store.conn.execute(
                    "UPDATE graphiti_episode_mappings SET content_hash='bad' WHERE episode_key=?",
                    (mapping["episode_key"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                os.store.conn.execute(
                    "DELETE FROM graphiti_episode_mappings WHERE episode_key=?",
                    (mapping["episode_key"],),
                )
            invalid_scopes = (
                ("workspace", {"workspace_id": "missing"}, "workspace scope mismatch"),
                ("source", {"source_id": "missing"}, "source scope mismatch"),
                ("document", {"document_id": "missing"}, "document scope mismatch"),
                ("generation", {"generation": "missing"}, "generation scope mismatch"),
            )
            for suffix, replacements, message in invalid_scopes:
                invalid = mapping | replacements | {
                    "episode_key": f"wrong-{suffix}",
                    "remote_episode_uuid": f"remote-wrong-{suffix}",
                }
                with self.assertRaisesRegex(sqlite3.IntegrityError, message):
                    os.store.conn.execute(
                        "INSERT INTO graphiti_episode_mappings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        tuple(invalid[key] for key in (
                            "episode_key", "remote_episode_uuid", "organization_id",
                            "workspace_id", "generation", "source_id", "document_id",
                            "observed_at", "recorded_at", "content_hash", "created_at",
                        )),
                    )
            os.store.conn.execute("DELETE FROM schema_migrations WHERE version=21")
            os.store.conn.commit()
            os.close()
            replayed = CompanyOS(path, graph_projection=UpstreamGraphitiProjection(client=FakeGraphiti()))
            self.assertEqual(replayed.store.schema_version, LATEST_SCHEMA_VERSION)
            self.assertEqual(
                replayed.store.conn.execute("SELECT COUNT(*) FROM graphiti_episode_mappings").fetchone()[0], 1
            )
            replayed.close()


if __name__ == "__main__":
    unittest.main()
