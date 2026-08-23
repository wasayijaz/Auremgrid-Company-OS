from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity


_KINDS = {"instructions", "policy", "settings"}
_SCOPES = {"organization", "workspace"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _decode(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


class BrainCustomizationService:
    """Versioned, scope-fenced custom Brain controls.

    The service depends on migration-owned tables. It intentionally does not
    create them on demand, so schema history remains explicit and reviewable.
    """

    def __init__(
        self,
        os: Any,
        new_id: Callable[[str], str],
    ) -> None:
        self.os = os
        self.conn = os.store.conn
        self.new_id = new_id

    def create_version(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        scope_type: str,
        kind: str,
        name: str,
        body: str,
        payload: dict[str, Any] | None = None,
        workspace_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        scope_type = self._scope(scope_type)
        kind = self._kind(kind)
        payload = self._payload(payload)
        body = self._text(body, "body")
        name = self._text(name, "name")
        reason = reason.strip() or "version created"
        self._authorize(identity, organization_id, workspace_id, scope_type, write=True)
        now = _now()
        version_number = self._next_version(organization_id, workspace_id, scope_type, kind)
        item = {
            "id": self.new_id("braincfgver"),
            "organization_id": organization_id,
            "workspace_id": workspace_id if scope_type == "workspace" else None,
            "scope_type": scope_type,
            "kind": kind,
            "version": version_number,
            "name": name,
            "body": body,
            "payload_json": _canonical_json(payload),
            "content_hash": _hash({"body": body, "payload": payload}),
            "created_by_person_id": identity.person_id,
            "created_at": now,
        }
        self.conn.execute(
            """INSERT INTO brain_customization_versions(
                id,organization_id,workspace_id,scope_type,kind,version,name,body,payload_json,
                content_hash,created_by_person_id,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(item.values()),
        )
        self._event(identity, organization_id, item["workspace_id"], scope_type, kind, item["id"], "created", reason, None, None)
        self.conn.commit()
        return self._version_dict(item)

    def activate_version(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        version_id: str,
        reason: str,
    ) -> dict[str, Any]:
        reason = self._text(reason, "reason")
        row = self._version_row(organization_id, version_id)
        scope_type, workspace_id = str(row["scope_type"]), row["workspace_id"]
        self._authorize(identity, organization_id, workspace_id, scope_type, write=True)
        prior = self._active_row(organization_id, workspace_id, scope_type, str(row["kind"]))
        if prior is not None and prior["id"] == row["id"]:
            return self._active_dict(prior, prior_event_id=prior["activation_event_id"])
        self._event(
            identity,
            organization_id,
            workspace_id,
            scope_type,
            str(row["kind"]),
            str(row["id"]),
            "activated",
            reason,
            str(row["id"]),
            str(prior["id"]) if prior else None,
        )
        self.conn.commit()
        active = self._active_row(organization_id, workspace_id, scope_type, str(row["kind"]))
        assert active is not None
        return self._active_dict(active, prior_event_id=active["activation_event_id"])

    def rollback(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        target_version_id: str,
        reason: str,
    ) -> dict[str, Any]:
        reason = self._text(reason, "reason")
        row = self._version_row(organization_id, target_version_id)
        active = self._active_row(organization_id, row["workspace_id"], str(row["scope_type"]), str(row["kind"]))
        if active is not None and active["id"] == target_version_id:
            return self._active_dict(active, prior_event_id=active["activation_event_id"])
        self._authorize(identity, organization_id, row["workspace_id"], str(row["scope_type"]), write=True)
        self._event(
            identity,
            organization_id,
            row["workspace_id"],
            str(row["scope_type"]),
            str(row["kind"]),
            str(row["id"]),
            "rolled_back",
            reason,
            str(row["id"]),
            str(active["id"]) if active else None,
        )
        self.conn.commit()
        current = self._active_row(organization_id, row["workspace_id"], str(row["scope_type"]), str(row["kind"]))
        assert current is not None
        return self._active_dict(current, prior_event_id=current["activation_event_id"])

    def active(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str | None,
        kind: str | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        if identity.organization_id != organization_id:
            raise AuthorizationError("identity scope mismatch")
        identity.require("brain_read")
        if workspace_id:
            self.os._require_person_access(organization_id, workspace_id, identity.person_id)
        elif self.os.company.org_membership(organization_id, identity.person_id) is None:
            raise AuthorizationError("organization membership required")
        kinds = [self._kind(kind)] if kind else sorted(_KINDS)
        return {
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "as_of": as_of.isoformat() if as_of else None,
            "effective": [item for scope in self._read_scopes(workspace_id) for item in self._active_for_scope(
                organization_id, scope[1], scope[0], kinds, as_of
            )],
        }

    def surface(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str | None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        active = self.active(identity, organization_id, workspace_id, as_of=as_of)["effective"]
        versions = self._visible_versions(identity, organization_id, workspace_id)
        events = self._visible_events(identity, organization_id, workspace_id, limit=30)
        return {
            "status": "configured" if active else "not_configured",
            "active": active,
            "versions": versions,
            "events": events,
            "can_manage": identity.can("brain_configure"),
            "allowed_actions": self._management_actions(identity, organization_id, workspace_id) if identity.can("brain_configure") else [],
        }

    def _visible_versions(
        self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None
    ) -> list[dict[str, Any]]:
        if workspace_id:
            clause = "(scope_type='organization' OR workspace_id=?)"
            params: tuple[Any, ...] = (organization_id, workspace_id)
        else:
            clause = "scope_type='organization'"
            params = (organization_id,)
        rows = self.conn.execute(
            f"""SELECT * FROM brain_customization_versions
                WHERE organization_id=? AND {clause}
                ORDER BY created_at DESC,id DESC LIMIT 50""",
            params,
        ).fetchall()
        return [self._version_dict(row) for row in rows]

    def _visible_events(
        self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        if workspace_id:
            clause = "(scope_type='organization' OR workspace_id=?)"
            params: tuple[Any, ...] = (organization_id, workspace_id, limit)
        else:
            clause = "scope_type='organization'"
            params = (organization_id, limit)
        rows = self.conn.execute(
            f"""SELECT * FROM brain_customization_events
                WHERE organization_id=? AND {clause}
                ORDER BY created_at DESC,event_sequence DESC,id DESC LIMIT ?""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def _active_for_scope(
        self,
        organization_id: str,
        workspace_id: str | None,
        scope_type: str,
        kinds: list[str],
        as_of: datetime | None,
    ) -> list[dict[str, Any]]:
        results = []
        moment_clause = "AND e.created_at<=?" if as_of else ""
        moment_args: list[Any] = [as_of.astimezone(timezone.utc).isoformat()] if as_of else []
        for kind in kinds:
            row = self.conn.execute(
                f"""SELECT v.*,e.id AS activation_event_id,e.action AS activation_action,e.created_at AS activated_at,
                           e.previous_version_id
                    FROM brain_customization_events e
                    JOIN brain_customization_versions v ON v.id=e.active_version_id
                    WHERE e.organization_id=? AND e.scope_type=? AND e.kind=?
                      AND ((? IS NULL AND e.workspace_id IS NULL) OR e.workspace_id=?)
                      AND e.action IN ('activated','rolled_back') {moment_clause}
                    ORDER BY e.event_sequence DESC LIMIT 1""",
                (organization_id, scope_type, kind, workspace_id, workspace_id, *moment_args),
            ).fetchone()
            if row is not None:
                results.append(self._active_dict(row, prior_event_id=row["activation_event_id"]))
        return results

    def _read_scopes(self, workspace_id: str | None) -> list[tuple[str, str | None]]:
        scopes: list[tuple[str, str | None]] = [("organization", None)]
        if workspace_id:
            scopes.append(("workspace", workspace_id))
        return scopes

    def _active_row(
        self, organization_id: str, workspace_id: str | None, scope_type: str, kind: str
    ) -> Any:
        return self.conn.execute(
            """SELECT v.*,e.id AS activation_event_id,e.action AS activation_action,e.created_at AS activated_at,
                      e.previous_version_id
               FROM brain_customization_events e
               JOIN brain_customization_versions v ON v.id=e.active_version_id
               WHERE e.organization_id=? AND e.scope_type=? AND e.kind=?
                 AND ((? IS NULL AND e.workspace_id IS NULL) OR e.workspace_id=?)
                 AND e.action IN ('activated','rolled_back')
               ORDER BY e.event_sequence DESC LIMIT 1""",
            (organization_id, scope_type, kind, workspace_id, workspace_id),
        ).fetchone()

    def _version_row(self, organization_id: str, version_id: str) -> Any:
        row = self.conn.execute(
            "SELECT * FROM brain_customization_versions WHERE organization_id=? AND id=?",
            (organization_id, version_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("brain customization version not found")
        return row

    def _event(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str | None,
        scope_type: str,
        kind: str,
        target_version_id: str,
        action: str,
        reason: str,
        active_version_id: str | None,
        previous_version_id: str | None,
    ) -> None:
        event = {
            "id": self.new_id("braincfgevent"),
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "scope_type": scope_type,
            "kind": kind,
            "target_version_id": target_version_id,
            "action": action,
            "reason": reason,
            "active_version_id": active_version_id,
            "previous_version_id": previous_version_id,
            "actor_principal_id": identity.principal_id,
            "actor_person_id": identity.person_id,
            "event_sequence": self._next_event_sequence(organization_id, workspace_id, scope_type, kind),
            "created_at": _now(),
        }
        self.conn.execute(
            """INSERT INTO brain_customization_events(
                id,organization_id,workspace_id,scope_type,kind,target_version_id,action,reason,
                active_version_id,previous_version_id,actor_principal_id,actor_person_id,event_sequence,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(event.values()),
        )
        self.conn.execute(
            """INSERT INTO ledger_audit(
                id,organization_id,workspace_id,principal_type,principal_id,action,entity_type,entity_id,detail,recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                self.new_id("audit"),
                organization_id,
                workspace_id,
                "person",
                identity.person_id,
                action,
                "brain_customization",
                target_version_id,
                reason,
                event["created_at"],
            ),
        )

    def _authorize(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str | None,
        scope_type: str,
        write: bool,
    ) -> None:
        if identity.organization_id != organization_id:
            raise AuthorizationError("identity scope mismatch")
        identity.require("brain_configure" if write else "brain_read")
        if scope_type == "workspace":
            if not workspace_id:
                raise ValidationError("workspace scope requires workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            scoped.require("brain_configure" if write else "brain_read")
            self.os._require_person_access(organization_id, workspace_id, scoped.person_id, write=write)
        elif self.os.company.org_membership(organization_id, identity.person_id) is None:
            raise AuthorizationError("organization membership required")

    def _management_actions(
        self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None
    ) -> list[dict[str, Any]]:
        actions = [{
            "action": "create_version",
            "label": "Create Brain version",
            "method": "POST",
            "route": "/brain/customizations",
            "payload": {"organization_id": organization_id, "workspace_id": workspace_id},
            "required_fields": ["scope_type", "kind", "name", "body", "reason"],
        }]
        for item in self._visible_versions(identity, organization_id, workspace_id)[:10]:
            actions.append({
                "action": "activate_version",
                "label": f"Activate v{item['version']}",
                "method": "POST",
                "route": "/brain/customizations/activate",
                "payload": {"organization_id": organization_id, "version_id": item["id"]},
                "required_fields": ["reason"],
            })
        return actions

    def _next_version(self, organization_id: str, workspace_id: str | None, scope_type: str, kind: str) -> int:
        row = self.conn.execute(
            """SELECT COALESCE(MAX(version),0)+1 AS next_version FROM brain_customization_versions
               WHERE organization_id=? AND scope_type=? AND kind=?
                 AND ((? IS NULL AND workspace_id IS NULL) OR workspace_id=?)""",
            (organization_id, scope_type, kind, workspace_id, workspace_id),
        ).fetchone()
        return int(row["next_version"] if row else 1)

    def _next_event_sequence(self, organization_id: str, workspace_id: str | None, scope_type: str, kind: str) -> int:
        row = self.conn.execute(
            """SELECT COALESCE(MAX(event_sequence),0)+1 AS next_sequence FROM brain_customization_events
               WHERE organization_id=? AND scope_type=? AND kind=?
                 AND ((? IS NULL AND workspace_id IS NULL) OR workspace_id=?)""",
            (organization_id, scope_type, kind, workspace_id, workspace_id),
        ).fetchone()
        return int(row["next_sequence"] if row else 1)

    def _version_dict(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = _decode(data.pop("payload_json", "{}"), {})
        return data

    def _active_dict(self, row: Any, prior_event_id: str | None) -> dict[str, Any]:
        data = self._version_dict(row)
        data["activation_event_id"] = prior_event_id
        data["activated_at"] = row["activated_at"] if "activated_at" in row.keys() else None
        data["activation_action"] = row["activation_action"] if "activation_action" in row.keys() else None
        data["previous_version_id"] = row["previous_version_id"] if "previous_version_id" in row.keys() else None
        return data

    @staticmethod
    def _kind(value: str | None) -> str:
        if value not in _KINDS:
            raise ValidationError("customization kind must be instructions, policy, or settings")
        return str(value)

    @staticmethod
    def _scope(value: str) -> str:
        if value not in _SCOPES:
            raise ValidationError("scope_type must be organization or workspace")
        return value

    @staticmethod
    def _payload(value: dict[str, Any] | None) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValidationError("payload must be an object")
        return value

    @staticmethod
    def _text(value: str, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{label} is required")
        return value.strip()
