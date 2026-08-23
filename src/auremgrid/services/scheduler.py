"""Durable worker loop and operator-facing scheduler health."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable



def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _scope_key(workspace_id: str | None) -> str:
    return workspace_id or "__organization__"


class DurableScheduler:
    def __init__(self, os: Any, organization_id: str, workspace_id: str | None, worker_id: str,
                 poll_seconds: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.os, self.organization_id, self.workspace_id, self.worker_id = os, organization_id, workspace_id, worker_id
        self.poll_seconds, self.clock = max(0.05, float(poll_seconds)), clock

    @property
    def paused(self) -> bool:
        row = self.os.store.conn.execute(
            "SELECT paused FROM scheduler_controls WHERE organization_id=? AND scope_key=?",
            (self.organization_id, _scope_key(self.workspace_id)),
        ).fetchone()
        return row is not None and bool(row[0])

    def set_paused(self, paused: bool) -> dict[str, Any]:
        now = _now()
        self.os.store.conn.execute(
            "INSERT INTO scheduler_controls(organization_id,workspace_id,scope_key,paused,reason,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(organization_id,scope_key) DO UPDATE SET workspace_id=excluded.workspace_id,paused=excluded.paused,reason=excluded.reason,updated_at=excluded.updated_at",
            (self.organization_id,self.workspace_id,_scope_key(self.workspace_id),int(paused),"operator_pause" if paused else None,now),
        )
        self.os.store.conn.commit()
        return self.health()

    def _heartbeat(self, status: str, result: Any | None = None, error: BaseException | str | None = None) -> None:
        now = _now(); payload = result if isinstance(result, dict) else ({"status": str(result)} if result is not None else None)
        last_error = str(error)[:500] if error is not None else None
        self.os.store.conn.execute(
            "INSERT INTO scheduler_heartbeats(worker_id,organization_id,workspace_id,scope_key,status,heartbeat_at,last_result,last_error,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(worker_id,organization_id,scope_key) DO UPDATE SET workspace_id=excluded.workspace_id,status=excluded.status,heartbeat_at=excluded.heartbeat_at,last_result=excluded.last_result,last_error=excluded.last_error,updated_at=excluded.updated_at",
            (self.worker_id,self.organization_id,self.workspace_id,_scope_key(self.workspace_id),status,now,
             json.dumps(payload,sort_keys=True) if payload is not None else None,last_error,now),
        )
        self.os.store.conn.commit()

    def run_once(self) -> dict[str, Any]:
        if self.paused:
            self._heartbeat("paused")
            return {"status": "paused"}
        self._heartbeat("running")
        from auremgrid.services.worker import run_one_job
        try:
            result = run_one_job(self.os, self.organization_id, self.workspace_id, self.worker_id)
            self._heartbeat("idle" if result.get("status") == "idle" else "completed", result)
            return result
        except Exception as exc:
            self._heartbeat("degraded", {"status": "error"}, exc)
            raise

    def run_forever(self, stop: Callable[[], bool] | None = None, max_iterations: int | None = None) -> None:
        iterations = 0
        try:
            while (stop is None or not stop()) and (max_iterations is None or iterations < max_iterations):
                result = self.run_once()
                iterations += 1
                if result.get("status") in {"idle", "paused"}:
                    time.sleep(self.poll_seconds)
        finally:
            self._heartbeat("stopped")

    def health(self) -> dict[str, Any]:
        row = self.os.store.conn.execute(
            "SELECT * FROM scheduler_heartbeats WHERE worker_id=? AND organization_id=? AND scope_key=?",
            (self.worker_id, self.organization_id, _scope_key(self.workspace_id)),
        ).fetchone()
        heartbeat = dict(row) if row is not None else None
        if heartbeat and heartbeat.get("last_result"):
            try: heartbeat["last_result"] = json.loads(heartbeat["last_result"])
            except (TypeError, ValueError): heartbeat["status"] = "degraded"
        status = heartbeat.get("status", "never_started") if heartbeat else "never_started"
        degraded = bool(heartbeat and status == "degraded")
        if heartbeat and heartbeat.get("heartbeat_at"):
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat["heartbeat_at"])).total_seconds()
                if age > max(30.0, self.poll_seconds * 10):
                    status, degraded = "degraded", True
                    heartbeat["detail"] = "heartbeat_stale"
            except (TypeError, ValueError):
                status, degraded = "degraded", True
        return {"worker_id": self.worker_id, "paused": self.paused,
                "status": status, "heartbeat": heartbeat, "degraded": degraded}
