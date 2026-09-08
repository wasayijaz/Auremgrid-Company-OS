from __future__ import annotations

from typing import Any

from auremgrid.api.http_shared import _need
from auremgrid.services.intelligence_governance import IntelligenceGovernanceService


class HttpRoutesIntelligenceGovernanceMixin:
    def _governance(self) -> IntelligenceGovernanceService:
        return IntelligenceGovernanceService(self.os, self.os.jobs.new_id)

    def _get_intelligence_governance(self, parsed: Any, params: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/dashboard/intelligence/governance/runbooks":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, self._governance().list_runbooks_with_states(
                scoped.organization_id, workspace_id, scoped.person_id,
            ))
            return True
        if parsed.path == "/dashboard/intelligence/governance/lessons":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, self._governance().proposed_lessons(
                scoped.organization_id, workspace_id, scoped.person_id,
            ))
            return True
        return False

    def _post_intelligence_governance(self, parsed: Any, payload: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/dashboard/intelligence/governance/runbooks/approve":
            assert identity is not None
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, {"approval": self._governance().decide_runbook(
                scoped.organization_id, workspace_id, scoped.person_id,
                _need(payload, "runbook_id"),
                int(_need(payload, "runbook_version")),
                _need(payload, "action"),
            )})
            return True
        if parsed.path == "/dashboard/intelligence/governance/runbooks/customize":
            assert identity is not None
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(201, {"runbook": self._governance().customize_runbook(
                scoped.organization_id, workspace_id, scoped.person_id,
                _need(payload, "runbook_id"), payload.get("patch"),
            )})
            return True
        if parsed.path == "/dashboard/intelligence/governance/lessons/decide":
            assert identity is not None
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, {"decision": self._governance().decide_lesson(
                scoped.organization_id, workspace_id, scoped.person_id,
                _need(payload, "lesson_id"), _need(payload, "decision"),
            )})
            return True
        return False
