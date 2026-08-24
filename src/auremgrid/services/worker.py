from __future__ import annotations

import json
from typing import Any

from auremgrid.services.brain import CompanyOS
from auremgrid.connectors.http import ConnectorTransportError
from auremgrid.domain.errors import AuthorizationError


JOB_CAPABILITIES = {
    "report.generate": "workspace_write",
    "projection.rebuild": "brain_promote",
    "connector.sync": "integration_sync",
    "proactive_intelligence.refresh": "brain_read",
    "agent.run": "workspace_write",
}


def run_one_job(
    os: CompanyOS,
    organization_id: str,
    workspace_id: str | None,
    worker_id: str,
) -> dict[str, Any]:
    """Claim and execute one safe local job using a worker-owned CompanyOS connection."""

    job = os.jobs.claim_job(organization_id, workspace_id, worker_id)
    if job is None:
        return {"status": "idle"}
    try:
        identity = os.auth.identity_for_principal(job["principal_id"], workspace_id)
        capability = JOB_CAPABILITIES.get(job["type"])
        if capability is None:
            raise ValueError(f"no registered handler for job type: {job['type']}")
        identity.require(capability)
        payload = job["payload"]
        if job["type"] == "report.generate":
            result = os.agent_ops.generate_report(
                organization_id,
                identity.person_id,
                str(payload["report_type"]),
                workspace_id,
            )
        elif job["type"] == "projection.rebuild":
            result = os.rebuild_projections(workspace_id)
        elif job["type"] == "proactive_intelligence.refresh":
            actor_id = None
            if workspace_id is not None:
                try:
                    actor_id = os.auth.actor_for_identity(identity, workspace_id)
                except AuthorizationError:
                    actor_id = None
            snapshot = os.proactive_intelligence.refresh_snapshot(
                organization_id,
                identity.person_id,
                str(payload.get("snapshot_type", "executive")),
                workspace_id,
                actor_id=actor_id,
                runbook_id=payload.get("runbook_id"),
            )
            result = {
                "snapshot_id": snapshot["id"],
                "snapshot_type": snapshot["snapshot_type"],
                "version": snapshot["version"],
                "status": snapshot["status"],
                "generated_at": snapshot["generated_at"],
                "attention_count": len(snapshot["attention"]),
                "unchanged": bool(snapshot.get("unchanged")),
            }
        elif job["type"] == "agent.run":
            action = payload.get("action") or {}
            if action.get("safe") is not True or action.get("one_way") is not False or action.get("action") != "generate_report":
                raise ValueError("unsupported or irreversible agent action")
            run_row = os.store.conn.execute(
                "SELECT r.*,t.approval_request_id,t.workspace_id AS task_workspace FROM agent_runs r JOIN agent_tasks t ON t.id=r.task_id WHERE r.id=? AND r.agent_id=?",
                (payload.get("run_id"), payload.get("agent_id")),
            ).fetchone()
            if run_row is None or run_row["status"] != "running" or run_row["task_workspace"] != workspace_id:
                raise AuthorizationError("agent run scope is invalid")
            approval = os.store.conn.execute(
                "SELECT status,workspace_id,action_type,payload FROM approval_requests WHERE id=? AND organization_id=?",
                (run_row["approval_request_id"], organization_id),
            ).fetchone()
            if approval is None or approval["status"] != "approved" or approval["workspace_id"] != workspace_id or approval["action_type"] != "report.generate":
                raise AuthorizationError("approved same-scope action required")
            try:
                approved_payload = json.loads(approval["payload"] or "{}")
            except (TypeError, ValueError):
                approved_payload = {}
            requested_type = action.get("payload", {}).get("type") or action.get("payload", {}).get("report_type")
            if approved_payload.get("report_type") != requested_type:
                raise AuthorizationError("approved report type does not match action")
            result = os.agent_ops.generate_report(
                organization_id, identity.person_id, str(action.get("payload", {}).get("type") or action.get("payload", {}).get("report_type") or "client_weekly_report"), workspace_id,
            )
            os.agent_ops.complete_run(organization_id, str(payload.get("agent_id")), str(payload.get("run_id")), json.dumps(result, sort_keys=True), source_refs=[str(result.get("id", ""))])
        elif job["type"] == "connector.sync":
            stream_lock=os.integrations.resume_job_stream(job["id"],worker_id,str(payload["mapping_hash"]))
            def connector_progress(value: float) -> None:
                os.jobs.heartbeat_job(
                    organization_id,workspace_id,job["id"],worker_id,job["lease_token"],value
                )
                os.integrations.inbox.heartbeat_stream(
                    stream_lock["id"],stream_lock["reservation_token"],lease_seconds=604800
                )
            result = os.integrations.sync(
                identity, str(payload["integration_id"]), str(payload["external_key"]),
                str(payload["workspace_id"]), str(payload["mapping_hash"]),
                f"{worker_id}:{job['lease_token']}",connector_progress,
                stream_lock["id"],stream_lock["reservation_token"],
            )
        else:
            raise ValueError(f"no registered handler for job type: {job['type']}")
        completed=os.jobs.succeed_job(
            organization_id, workspace_id, job["id"], worker_id, job["lease_token"], result
        )
        if job["type"]=="connector.sync":
            os.integrations.release_job_stream(job["id"])
        return completed
    except Exception as exc:
        retryable = isinstance(exc, ConnectorTransportError) and exc.retryable
        failed=os.jobs.fail_job(
            organization_id,
            workspace_id,
            job["id"],
            worker_id,
            job["lease_token"],
            {"type": exc.__class__.__name__, "message": str(exc)},
            retry=retryable,
            retry_after_seconds=exc.retry_after if isinstance(exc, ConnectorTransportError) else None,
        )
        if job["type"]=="connector.sync" and failed["status"] in {"failed","dead_letter","cancelled"}:
            os.integrations.release_job_stream(job["id"])
        return failed
