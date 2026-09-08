from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DurableJobType:
    capability: str
    public_api: bool = True


DURABLE_JOB_TYPES: dict[str, DurableJobType] = {
    "report.generate": DurableJobType("workspace_write"),
    "projection.rebuild": DurableJobType("brain_promote"),
    "connector.sync": DurableJobType("integration_sync", public_api=False),
    "proactive_intelligence.refresh": DurableJobType("brain_read"),
    "agent.run": DurableJobType("workspace_write"),
    "agent.think": DurableJobType("brain_read"),
    "automation.execute": DurableJobType("automation_execute"),
}


REGISTERED_JOB_TYPES = frozenset(DURABLE_JOB_TYPES)
PUBLIC_JOB_TYPES = frozenset(
    job_type
    for job_type, definition in DURABLE_JOB_TYPES.items()
    if definition.public_api
)


def job_capability(job_type: str) -> str | None:
    definition = DURABLE_JOB_TYPES.get(job_type)
    if definition is None:
        return None
    return definition.capability


def has_registered_job_handler(job_type: str) -> bool:
    return job_type in DURABLE_JOB_TYPES
