from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
try:
    sys.path.remove(str(SRC_ROOT))
except ValueError:
    pass
sys.path.insert(0, str(SRC_ROOT))

from auremgrid.api.http import serve
from auremgrid.lifecycle import startup_health
from auremgrid.services.brain import CompanyOS
from auremgrid.services.worker import run_one_job
from auremgrid.storage.backup import create_backup, restore_backup, verify_backup


def _get_json(host: str, port: int, path: str) -> dict[str, Any]:
    connection = HTTPConnection(host, port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = json.loads(response.read())
        if response.status >= 400:
            raise RuntimeError(f"{path} returned HTTP {response.status}: {body}")
        return body
    finally:
        connection.close()


def run_smoke(work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    live_path = work_dir / "auremgrid-smoke.sqlite"
    backup_path = work_dir / "backups" / "auremgrid-smoke.sqlite"
    restored_path = work_dir / "restored.sqlite"

    os = CompanyOS(live_path)
    try:
        organization = os.create_organization("Auremgrid Smoke", "org_private_host_smoke")
        workspace = os.create_organization_workspace(
            organization.id, "Smoke Client", "client", "ws_private_host_smoke"
        )
        person = os.create_person(
            organization.id,
            "Smoke Owner",
            "owner@private-host-smoke.test",
            role="owner",
            person_id="person_private_host_smoke",
        )
        os.add_person_to_workspace(organization.id, workspace.id, person.id, "admin")
        principal = os.auth.create_principal(organization.id, person.id, "owner@private-host-smoke.test")
        job = os.jobs.enqueue_job(
            organization.id,
            workspace.id,
            principal["id"],
            "report.generate",
            {"report_type": "client_weekly_report"},
        )
        os.jobs.add_outbox_event(
            organization.id,
            workspace.id,
            "smoke",
            "restore-boundary",
            "smoke.ready",
            {"status": "ready"},
        )

        server = serve(os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            health = _get_json(str(host), int(port), "/health")
            detailed_health = _get_json(str(host), int(port), "/health/detailed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    finally:
        os.close()

    worker_os = CompanyOS(live_path)
    try:
        worker_result = run_one_job(
            worker_os,
            "org_private_host_smoke",
            "ws_private_host_smoke",
            "worker-private-host-smoke",
        )
        backup_manifest = create_backup(worker_os.store.conn, backup_path)
    finally:
        worker_os.close()

    verified_backup = verify_backup(backup_path)
    restored = restore_backup(backup_path, restored_path)
    restored_os = CompanyOS(restored_path)
    try:
        restored_projection = restored_os.rebuild_projections()
        state = dict(restored_os.store.conn.execute("SELECT key,value FROM system_state").fetchall())
        outbound_claims = restored_os.jobs.claim_outbox_events(
            "org_private_host_smoke",
            "ws_private_host_smoke",
            "publisher-private-host-smoke",
        )
        restored_warnings = startup_health(restored_os.store.raw_connection, restored_path)
    finally:
        restored_os.close()

    checks = {
        "health_ok": health.get("ok") is True,
        "detailed_health_reachable": "schema_version" in detailed_health,
        "worker_succeeded": worker_result.get("id") == job["id"] and worker_result.get("status") == "succeeded",
        "backup_verified": verified_backup.get("integrity") == "ok",
        "restore_recovery_mode": restored.get("recovery_mode") is True and state.get("recovery_mode") == "1",
        "restore_outbound_disabled": restored.get("outbound_dispatch") == "disabled"
        and state.get("outbound_dispatch") == "disabled"
        and outbound_claims == [],
        "restore_projection_healthy": restored_projection.get("status") == "healthy",
    }
    return {
        "status": "ok" if all(checks.values()) else "failed",
        "docker_required": False,
        "checks": checks,
        "health": health,
        "detailed_health_warnings": detailed_health.get("warnings", []),
        "worker": {"id": worker_result.get("id"), "status": worker_result.get("status")},
        "backup": {
            "path": backup_manifest["path"],
            "schema_version": backup_manifest["schema_version"],
            "integrity": backup_manifest["integrity"],
        },
        "restore": {
            "path": restored["path"],
            "recovery_mode": restored["recovery_mode"],
            "outbound_dispatch": restored["outbound_dispatch"],
            "startup_warnings": restored_warnings,
            "projection": restored_projection,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Python-only private-host smoke rehearsal.")
    parser.add_argument("--work-dir", type=Path, help="directory for temporary smoke databases")
    parser.add_argument("--keep", action="store_true", help="keep temporary files when --work-dir is omitted")
    args = parser.parse_args(argv)

    temp_root: Path | None = None
    if args.work_dir is None:
        temp_root = Path(tempfile.mkdtemp(prefix="auremgrid-private-host-"))
        work_dir = temp_root
    else:
        work_dir = args.work_dir
    try:
        result = run_smoke(work_dir)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "ok" else 1
    finally:
        if temp_root is not None and not args.keep:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
