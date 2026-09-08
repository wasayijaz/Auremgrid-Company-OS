from __future__ import annotations

from typing import Any

from auremgrid.api.http_shared import _need
from auremgrid.domain.errors import AuthorizationError
from auremgrid.services.intelligence_intake import IntelligenceIntakeStore


class HttpRoutesIntelligenceIntakeMixin:
    def _get_intelligence_intake(self, parsed: Any, params: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/dashboard/intelligence/success-definition":
            assert identity is not None
            organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
            if identity.organization_id != organization_id or identity.person_id != person_id:
                raise AuthorizationError("identity scope mismatch")
            self.os._require_scope_access(organization_id, person_id)
            store = IntelligenceIntakeStore(self.os.store.conn, self.os.jobs.new_id)
            self._json(200, store.questions(organization_id))
            return True
        return False

    def _post_intelligence_intake(self, parsed: Any, payload: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/dashboard/intelligence/success-definition":
            assert identity is not None
            organization_id, person_id = _need(payload, "organization_id"), _need(payload, "person_id")
            if identity.organization_id != organization_id or identity.person_id != person_id:
                raise AuthorizationError("identity scope mismatch")
            self.os._require_scope_access(organization_id, person_id, write=True)
            store = IntelligenceIntakeStore(self.os.store.conn, self.os.jobs.new_id)
            self._json(201, {"answer": store.answer_question(
                organization_id, _need(payload, "question_key"), _need(payload, "answer"), person_id,
            )})
            return True
        return False
