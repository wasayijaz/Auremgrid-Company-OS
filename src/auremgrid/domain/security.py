"""Authentication identities and capability policy for Auremgrid."""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet

from auremgrid.domain.errors import AuthorizationError


CAPABILITIES: tuple[str, ...] = (
    "organization_manage",
    "workspace_read",
    "workspace_write",
    "people_manage",
    "finance_read",
    "finance_write",
    "integration_configure",
    "integration_sync",
    "secret_bind",
    "secret_rotate",
    "workflow_template_manage",
    "workflow_run",
    "workflow_gate",
    "approval_decide",
    "agent_configure",
    "agent_run",
    "automation_manage",
    "automation_execute",
    "brain_read",
    "brain_propose",
    "brain_promote",
    "brain_configure",
    "external_send",
    "external_publish",
    "job_manage",
    "backup_create",
    "backup_restore",
    "auth_manage",
    "client_portal",
)

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "owner": frozenset(CAPABILITIES),
    "admin": frozenset(CAPABILITIES),
    "member": frozenset({"workspace_read", "workspace_write", "workflow_run", "brain_read", "brain_propose"}),
    # An external client contact granted portal access.  Organization role
    # is deliberately as narrow as workspace role: a client principal must
    # never inherit staff capabilities even if a workspace membership is
    # later misconfigured to "admin" by mistake, because the intersection
    # of org and workspace capabilities is what governs access.
    "client": frozenset({"workspace_read", "client_portal"}),
}

WORKSPACE_ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "admin": frozenset(CAPABILITIES),
    "operator": frozenset({
        "workspace_read", "workspace_write", "workflow_run", "workflow_gate",
        "brain_read", "brain_propose", "integration_sync", "agent_run", "automation_execute",
    }),
    "viewer": frozenset({"workspace_read", "brain_read"}),
    # External client-portal identities.  Deliberately excludes
    # "workspace_write" so a client cannot mutate internal work state
    # directly; portal-specific actions (intake submission, client review
    # decisions) are granted through the narrower "client_portal" capability
    # instead, keeping the client role's blast radius bounded even if the
    # portal service layer has a bug.
    "client": frozenset({"workspace_read", "client_portal"}),
}


@dataclass(frozen=True)
class AuthenticatedIdentity:
    """Canonical identity derived from a persisted principal and credential."""

    principal_id: str
    organization_id: str
    person_id: str
    auth_type: str
    capabilities: FrozenSet[str]
    scopes: FrozenSet[str] = frozenset()
    workspace_id: str | None = None

    @property
    def is_session(self) -> bool:
        return self.auth_type == "session"

    @property
    def is_api_token(self) -> bool:
        return self.auth_type == "api_token"

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def require(self, capability: str) -> None:
        if not self.can(capability):
            raise AuthorizationError(f"capability denied: {capability}")

    def to_dict(self) -> dict[str, object]:
        return {
            "principal_id": self.principal_id,
            "organization_id": self.organization_id,
            "person_id": self.person_id,
            "auth_type": self.auth_type,
            "capabilities": sorted(self.capabilities),
            "scopes": sorted(self.scopes),
            "workspace_id": self.workspace_id,
        }


def role_capabilities(org_role: str, workspace_role: str | None = None) -> frozenset[str]:
    """Resolve role policy with workspace scope acting as a restrictive boundary."""

    org_caps = ROLE_CAPABILITIES.get(org_role, frozenset())
    if workspace_role is None:
        return org_caps
    return frozenset(org_caps & WORKSPACE_ROLE_CAPABILITIES.get(workspace_role, frozenset()))


def intersect_token_scopes(capabilities: frozenset[str], scopes: frozenset[str]) -> frozenset[str]:
    """API tokens can only narrow role-derived capabilities."""

    return frozenset(capabilities & scopes)
