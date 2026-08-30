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
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

# Running a file from ``scripts/`` puts that directory first on sys.path,
# where the legacy ``auremgrid.py`` helper would shadow the package. Ensure
# the source tree wins for direct, copy-paste execution.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS
from auremgrid.storage.backup import create_backup, verify_backup


def _seed(client_count: int) -> tuple[CompanyOS, str, str, str, str, AuthenticatedIdentity]:
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
        os.ingest_text(
            workspace.id,
            actor.id,
            f"perf_source_{index:03d}",
            f"META: confidence=0.98\nFACT: Client {index:03d} | success metric | qualified pipeline\n",
            f"fixture://performance/{index:03d}",
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

    # Workflow board remains a valid read path even when no run is active. The
    # rehearsal intentionally avoids inventing roster assignments or approvals.
    first_ws = workspaces[0]
    first_actor = "perf_actor_000"

    principal = os.auth.create_principal(org_id, owner_id, owner.email)
    session = os.auth.create_session(principal["id"])
    base_identity = os.auth.authenticate_session(session["token"])
    os.auth.bind_actor(base_identity, first_ws, first_actor)
    identity = os.auth.authenticate_session(session["token"], workspace_id=first_ws)
    return os, org_id, owner_id, first_ws, first_actor, identity


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
        "p95_ms": round(sorted(samples)[max(0, int(len(samples) * 0.95) - 1)], 3),
        "samples": len(samples),
    }


def run_rehearsal(client_count: int, repeats: int = 3) -> dict[str, Any]:
    os, org_id, owner_id, workspace_id, actor_id, identity = _seed(client_count)
    try:
        measurements: dict[str, Any] = {}
        paths: dict[str, Callable[[], Any]] = {
            "dashboard_command": lambda: os.dashboard.command(org_id, owner_id),
            "brain_search": lambda: os.search(workspace_id, actor_id, "success metric"),
            "work_list": lambda: os.list_work(workspace_id, actor_id),
            "workflow_query": lambda: os.dashboard.workflow_board(identity, org_id, workspace_id, owner_id),
            "intelligence_brief": lambda: os.intelligence.executive_brief(org_id, owner_id, use_reasoning_provider=False),
        }
        for name, path in paths.items():
            try:
                measurements[name] = {"status": "measured", **_measure(path, repeats)}
            except Exception as exc:  # Keep the rehearsal useful when a surface is unavailable.
                measurements[name] = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}

        with tempfile.TemporaryDirectory(prefix="auremgrid-perf-") as directory:
            backup_path = Path(directory) / "rehearsal.sqlite"
            started = time.perf_counter()
            manifest = create_backup(os.store.conn, backup_path)
            verify = verify_backup(backup_path, manifest["sha256"])
            backup_ms = (time.perf_counter() - started) * 1000
        measurements["backup_verify"] = {
            "status": "measured",
            "elapsed_ms": round(backup_ms, 3),
            "size_bytes": verify["size_bytes"],
            "schema_version": verify["schema_version"],
        }
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
