from __future__ import annotations

from typing import Any

from auremgrid.services.brain import CompanyOS
from auremgrid.connectors.http import ConnectorTransportError


JOB_CAPABILITIES = {
    "report.generate": "workspace_write",
    "projection.rebuild": "brain_promote",
    "connector.sync": "integration_sync",
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
