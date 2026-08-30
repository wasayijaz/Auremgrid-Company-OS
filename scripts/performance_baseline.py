"""Deterministic single-host performance rehearsal for agency adoption.

The rehearsal uses only the local CompanyOS services and an in-memory SQLite
store.  It creates N small, isolated client workspaces (N=10,25,50), then
times the canonical read paths used by the dashboard/brain/work/workflow and
intelligence surfaces.  No network, provider, or browser is involved.

Usage::

    python scripts/performance_baseline.py
    python scripts/performance_baseline.py --clients 10 25 50 --repeats 3
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Running a file from ``scripts/`` puts that directory first on sys.path,
# where the legacy ``auremgrid.py`` helper would shadow the package. Ensure
# the source tree wins for direct, copy-paste execution.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS
from auremgrid.services.worker import run_one_job
from auremgrid.storage.backup import create_backup, verify_backup


@dataclass(frozen=True)
class RehearsalFixture:
    os: CompanyOS
    org_id: str
    owner_id: str
    operator_id: str
    principal_id: str
    workspace_ids: tuple[str, ...]
    primary_workspace_id: str
    primary_actor_id: str
    identity: AuthenticatedIdentity


def _seed(client_count: int) -> RehearsalFixture:
    os = CompanyOS(":memory:")
    org_id = f"perf_org_{client_count}"
    owner_id = "perf_owner"
    operator_id = "perf_operator"
    os.create_organization("Performance rehearsal", org_id)
    owner = os.create_person(org_id, "Performance Owner", "owner@perf.invalid", role="owner", person_id=owner_id)
    operator = os.create_person(org_id, "Performance Operator", "operator@perf.invalid", role="admin", person_id=operator_id)
    workspaces: list[str] = []
    for index in range(client_count):
        workspace_id = f"perf_ws_{index:03d}"
        workspace = os.create_organization_workspace(org_id, f"Client {index:03d}", "client", workspace_id)
        workspaces.append(workspace.id)
        os.add_person_to_workspace(org_id, workspace.id, owner.id, "admin")
        os.add_person_to_workspace(org_id, workspace.id, operator.id, "operator")
        actor = os.create_actor(workspace.id, f"Client {index:03d} operator", "admin", f"perf_actor_{index:03d}")
        os.client_ops.create_client_roster(
            org_id,
            workspace.id,
            owner.id,
            [
                {"role_key": "client_success_dri", "person_id": owner.id},
                {"role_key": "client_success_backup", "person_id": operator.id},
                {"role_key": "wing_lead", "wing": "strategy", "person_id": operator.id},
                {"role_key": "wing_executive", "wing": "strategy", "person_id": owner.id},
            ],
        )
        os.ingest_text(
            workspace.id,
            actor.id,
            f"perf_source_{index:03d}",
            f"META: confidence=0.98\nFACT: Client {index:03d} | success metric | qualified pipeline\n",
            f"fixture://performance/{index:03d}",
        )
        os.upsert_client_brain(
            workspace.id,
            actor.id,
            snapshot=f"Client {index:03d} retainer. Success is qualified pipeline.",
            brand_rules="Clear proof-led messaging. Use only verified operational claims.",
            ads="Prioritize measurable pipeline offers.",
            dos=["Cite the current success metric"],
            donts=["Do not invent performance claims"],
            open_loops=["Weekly brief needs fresh evidence"],
        )
        os.capture_work(
            workspace.id,
            actor.id,
            f"Client {index:03d} weekly brief",
            "Prepare the weekly evidence-backed client brief.",
            "Performance Operator",
            needed_by="2099-12-31",
            work_item_id=f"perf_work_{index:03d}",
        )
        if index == 0:
            for extra in range(max(25, client_count * 2)):
                os.capture_work(
                    workspace.id,
                    actor.id,
                    f"Client {index:03d} backlog item {extra:03d}",
                    "Triage a deterministic workload item for the large work-list baseline.",
                    "Performance Operator",
                    needed_by="2099-12-31",
                    work_item_id=f"perf_work_{index:03d}_{extra:03d}",
                )
        if index == 0:
            try:
                os.workflow_ops.create_run(
                    org_id,
                    workspace.id,
                    owner.id,
                    os.workflow_catalog.get("client_request"),
                    idempotency_key=f"perf_workflow_{index:03d}",
                )
            except Exception:
                # The board query itself reports availability; seeding should
                # not make unrelated baseline paths unusable if the catalog
                # contract changes.
                pass

    first_ws = workspaces[0]
    first_actor = "perf_actor_000"

    principal = os.auth.create_principal(org_id, owner_id, owner.email)
    session = os.auth.create_session(principal["id"])
    base_identity = os.auth.authenticate_session(session["token"])
    os.auth.bind_actor(base_identity, first_ws, first_actor)
    identity = os.auth.authenticate_session(session["token"], workspace_id=first_ws)
    return RehearsalFixture(
        os=os,
        org_id=org_id,
        owner_id=owner_id,
        operator_id=operator_id,
        principal_id=principal["id"],
        workspace_ids=tuple(workspaces),
        primary_workspace_id=first_ws,
        primary_actor_id=first_actor,
        identity=identity,
    )


def _measure(fn: Callable[[], Any], repeats: int) -> dict[str, float | int]:
    # Warm one call to exclude one-time SQLite statement setup from the sample.
    fn()
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(sorted(samples)[max(0, math.ceil(len(samples) * 0.95) - 1)], 3),
        "samples": len(samples),
    }


def _measure_backup(os: CompanyOS, backup_path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    manifest = create_backup(os.store.conn, backup_path)
    verify = verify_backup(backup_path, manifest["sha256"])
    return {
        "status": "measured",
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "size_bytes": verify["size_bytes"],
        "schema_version": verify["schema_version"],
    }


def _measure_migration_open(db_path: Path, repeats: int) -> dict[str, Any]:
    schema_version = None

    def open_once() -> int:
        opened = CompanyOS(db_path)
        try:
            return opened.store.schema_version
        finally:
            opened.close()

    open_once()
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        schema_version = open_once()
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "status": "measured",
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(sorted(samples)[max(0, math.ceil(len(samples) * 0.95) - 1)], 3),
        "samples": len(samples),
        "schema_version": schema_version,
    }


def _measure_worker_queue(fixture: RehearsalFixture) -> dict[str, Any]:
    os = fixture.os
    jobs = []
    for index, workspace_id in enumerate(fixture.workspace_ids):
        jobs.append(
            os.jobs.enqueue_job(
                fixture.org_id,
                workspace_id,
                fixture.principal_id,
                "proactive_intelligence.refresh",
                {"snapshot_type": "workspace", "workspace_id": workspace_id, "runbook_id": None},
                idempotency_key=f"perf-worker-refresh-{workspace_id}-{index}",
            )
        )
    started = time.perf_counter()
    results = [
        run_one_job(os, fixture.org_id, workspace_id, f"perf-worker-{index:03d}")
        for index, workspace_id in enumerate(fixture.workspace_ids)
    ]
    elapsed_ms = (time.perf_counter() - started) * 1000
    succeeded = sum(1 for result in results if result.get("status") == "succeeded")
    return {
        "status": "measured" if succeeded == len(jobs) else "degraded",
        "elapsed_ms": round(elapsed_ms, 3),
        "jobs": len(jobs),
        "succeeded": succeeded,
        "jobs_per_second": round((succeeded / elapsed_ms) * 1000, 3) if elapsed_ms else float(succeeded),
    }


def run_rehearsal(client_count: int, repeats: int = 3) -> dict[str, Any]:
    fixture = _seed(client_count)
    os = fixture.os
    try:
        measurements: dict[str, Any] = {}
        paths: dict[str, Callable[[], Any]] = {
            "dashboard_command": lambda: os.dashboard.command(fixture.org_id, fixture.owner_id),
            "brain_search": lambda: os.search(fixture.primary_workspace_id, fixture.primary_actor_id, "success metric"),
            "intelligence_workspace": lambda: os.intelligence.workspace(
                fixture.org_id,
                fixture.primary_workspace_id,
                fixture.owner_id,
                actor_id=fixture.primary_actor_id,
                use_reasoning_provider=False,
            ),
            "intelligence_portfolio": lambda: os.intelligence.portfolio(
                fixture.org_id,
                fixture.owner_id,
                actor_id=fixture.primary_actor_id,
                use_reasoning_provider=False,
            ),
            "proactive_refresh_snapshot": lambda: os.proactive_intelligence.refresh_snapshot(
                fixture.org_id,
                fixture.owner_id,
                "workspace",
                fixture.primary_workspace_id,
                actor_id=fixture.primary_actor_id,
            ),
            "workflow_query": lambda: os.dashboard.workflow_board(
                fixture.identity,
                fixture.org_id,
                fixture.primary_workspace_id,
                fixture.owner_id,
            ),
            "large_work_list": lambda: os.list_work(fixture.primary_workspace_id, fixture.primary_actor_id),
            "projection_rebuild": lambda: os.rebuild_projections(),
        }
        for name, path in paths.items():
            try:
                measurements[name] = {"status": "measured", **_measure(path, repeats)}
            except Exception as exc:  # Keep the rehearsal useful when a surface is unavailable.
                measurements[name] = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}

        with tempfile.TemporaryDirectory(prefix="auremgrid-perf-") as directory:
            backup_path = Path(directory) / "rehearsal.sqlite"
            measurements["backup_verify"] = _measure_backup(os, backup_path)
            measurements["migration_open"] = _measure_migration_open(backup_path, repeats)
        measurements["worker_queue_throughput"] = _measure_worker_queue(fixture)
        return {"clients": client_count, "measurements": measurements}
    finally:
        os.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic single-host agency performance rehearsal.")
    parser.add_argument("--clients", nargs="+", type=int, default=[10, 25, 50], help="client counts to rehearse")
    parser.add_argument("--repeats", type=int, default=3, help="timed samples per read path")
    args = parser.parse_args()
    if args.repeats < 1 or any(count < 1 for count in args.clients):
        parser.error("clients and repeats must be positive")
    started = time.perf_counter()
    results = [run_rehearsal(count, args.repeats) for count in args.clients]
    print(json.dumps({"runtime_ms": round((time.perf_counter() - started) * 1000, 3), "results": results}, indent=2))


if __name__ == "__main__":
    main()
