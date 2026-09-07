from __future__ import annotations

from typing import Any

from datetime import timedelta

from auremgrid.api.http_shared import _int, _need, _optional_str
from auremgrid.domain.errors import AuthorizationError


class HttpRoutesAuthOrgMixin:
    def _get_auth_org(self, parsed: Any, params: dict[str, list[str]], identity: Any) -> bool:
        if parsed.path == "/auth/me":
            assert identity is not None
            self._json(200, identity.to_dict()); return True
        if parsed.path == "/auth/invites":
            assert identity is not None
            self._json(200, {"invites": self.os.auth.list_invites(identity, params.get("include_inactive") == "true")}); return True
        if parsed.path == "/auth/sessions":
            assert identity is not None
            self._json(200, {"sessions": self.os.auth.list_sessions(identity, params.get("include_revoked") == "true")}); return True
        if parsed.path == "/organizations/workspaces":
            items = self.os.company.list_workspaces(_need(params, "organization_id"))
            self._json(200, {"workspaces": items})
            return True
        if parsed.path == "/onboarding/imports":
            assert identity is not None
            self._json(200, self.os.onboarding.list_import_batches(
                identity.organization_id,
                _optional_str(params.get("workspace_id")),
                identity.person_id,
                _int(params.get("limit", "10"), "limit"),
            )); return True
        if parsed.path == "/people":
            organization_id,person_id=_need(params,"organization_id"),_need(params,"person_id")
            membership = self.os.company.org_membership(organization_id, person_id)
            if membership is None: raise AuthorizationError("organization membership required")
            if membership.role == "client": raise AuthorizationError("people directory requires agency membership")
            items = self.os.company.list_people(organization_id)
            workspace_id = _optional_str(params.get("workspace_id"))
            if workspace_id:
                self.os._require_person_access(organization_id, workspace_id, person_id)
                items = [item for item in items if self.os.company.workspace_membership(workspace_id, item.id) is not None]
            self._json(200, {"people": [item.to_dict() for item in items]})
            return True
        if parsed.path == "/people/detail":
            self._json(200, self.os.dashboard.person_detail(
                _need(params, "organization_id"), _need(params, "person_id"), _need(params, "target_person_id"), params.get("workspace_id"), params.get("week_start")
            ))
            return True
        return False

    def _post_auth_org(self, parsed: Any, params: dict[str, list[str]], identity: Any) -> bool:
        payload = params
        if parsed.path == "/auth/api-tokens":
            item=self.os.auth.create_api_token(identity.principal_id,_need(payload,"name"),
                [str(value) for value in payload.get("scopes",[])])
            self._json(201,{"id":item["id"],"name":item["name"],"token":item["token"],"scopes":item["scopes"],"expires_at":item["expires_at"]}); return True
        if parsed.path == "/auth/invites":
            item=self.os.auth.create_invite(identity,_need(payload,"target_person_id"),_need(payload,"email"),
                _optional_str(payload.get("workspace_id")),_optional_str(payload.get("actor_id")),
                timedelta(seconds=int(payload.get("expires_in_seconds", 604800))))
            self._json(201,item); return True
        if parsed.path == "/auth/invites/revoke":
            self._json(200,self.os.auth.revoke_invite(identity,_need(payload,"invite_id"))); return True
        if parsed.path == "/auth/invites/consume":
            self._json(200,self.os.auth.consume_invite(identity,_need(payload,"token"))); return True
        if parsed.path == "/auth/sessions/rotate":
            token=self.headers.get("Authorization","")[7:].strip(); item=self.os.auth.rotate_session(token)
            self._json(200,{"id":item["id"],"token":item["token"],"expires_at":item["expires_at"]}); return True
        if parsed.path == "/auth/sessions/revoke":
            self._json(200,self.os.auth.revoke_session_by_id(identity,_need(payload,"session_id"))); return True
        if parsed.path == "/auth/revoke":
            token=self.headers.get("Authorization","")[7:].strip()
            if identity.is_api_token:self.os.auth.revoke_api_token(token)
            else:self.os.auth.revoke_session(token)
            self._json(200,{"revoked":True}); return True
        if parsed.path == "/auth/actor-bindings":
            self._json(201,self.os.auth.bind_actor(identity,_need(payload,"workspace_id"),_need(payload,"actor_id"))); return True
        if parsed.path == "/organizations":
            item = self.os.create_organization(_need(payload, "name"), _optional_str(payload.get("id")))
            self._json(201, item.to_dict())
            return True
        if parsed.path == "/workspaces":
            item = self.os.create_organization_workspace(
                _need(payload, "organization_id"), _need(payload, "name"),
                str(payload.get("kind", "client")), _optional_str(payload.get("id")),
            )
            self._json(201, item.to_dict())
            return True
        if parsed.path == "/people":
            item = self.os.create_person(
                _need(payload, "organization_id"), _need(payload, "name"),
                _optional_str(payload.get("email")), _optional_str(payload.get("title")),
                _optional_str(payload.get("department")), _optional_str(payload.get("manager_id")),
                str(payload.get("role", "member")), _optional_str(payload.get("id")),
            )
            self._json(201, item.to_dict())
            return True
        if parsed.path == "/workspace-memberships":
            item = self.os.add_person_to_workspace(
                _need(payload, "organization_id"), _need(payload, "workspace_id"),
                _need(payload, "person_id"), str(payload.get("role", "operator")),
            )
            self._json(201, item.to_dict())
            return True
        if parsed.path == "/onboarding/imports/preview":
            result = self.os.onboarding.preview_csv_import(
                _need(payload, "organization_id"),
                _optional_str(payload.get("workspace_id")),
                _need(payload, "person_id"),
                _need(payload, "import_type"),
                _need(payload, "csv_text"),
                _need(payload, "idempotency_key"),
            )
            self._json(201, result)
            return True
        if parsed.path == "/onboarding/imports/commit":
            result = self.os.onboarding.commit_csv_import(
                _need(payload, "organization_id"),
                _need(payload, "batch_id"),
                _need(payload, "person_id"),
                _need(payload, "idempotency_key"),
            )
            self._json(200, result)
            return True
        return False
