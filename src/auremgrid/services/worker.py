from __future__ import annotations

from typing import Any

from auremgrid.services.brain import CompanyOS


JOB_CAPABILITIES = {
    "report.generate": "workspace_write",
    "projection.rebuild": "brain_promote",
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
        else:
            raise ValueError(f"no registered handler for job type: {job['type']}")
        return os.jobs.succeed_job(
            organization_id, workspace_id, job["id"], worker_id, job["lease_token"], result
        )
    except Exception as exc:
        return os.jobs.fail_job(
            organization_id,
            workspace_id,
            job["id"],
            worker_id,
            job["lease_token"],
            {"type": exc.__class__.__name__, "message": str(exc)},
            retry=False,
        )
