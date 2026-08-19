"""Stdlib-only opaque-token authentication and capability enforcement."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from auremgrid.domain.errors import AuthenticationError, AuthorizationError, ValidationError
from auremgrid.domain.security import (
    AuthenticatedIdentity,
    CAPABILITIES,
    intersect_token_scopes,
    role_capabilities,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def hash_token(token: str) -> str:
    """Hash a credential for storage; plaintext credentials never enter SQL."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_value(row: Any, key: str, index: int | None = None) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return row[index] if index is not None else None


class AuthService:
    """Persistence-backed authentication service over the schema-v11 tables."""

    def __init__(self, conn: sqlite3.Connection, new_id: Callable[[str], str] | None = None) -> None:
        self.conn = conn
        self.new_id = new_id or (lambda prefix: f"{prefix}_{secrets.token_hex(12)}")

    def _id(self, prefix: str) -> str:
        return self.new_id(prefix)

    def _person(self, organization_id: str, person_id: str) -> Any:
        row = self.conn.execute(
            "SELECT id, organization_id, email, status FROM people WHERE id=? AND organization_id=?",
            (person_id, organization_id),
        ).fetchone()
        if row is None:
            raise AuthorizationError("person does not belong to organization")
        status = _row_value(row, "status", 3)
        if status is not None and status != "active":
            raise AuthorizationError("person is disabled")
        return row

    def create_principal(self, organization_id: str, person_id: str, email: str) -> dict[str, Any]:
        if not isinstance(email, str) or not email.strip():
            raise ValidationError("email is required")
        person = self._person(organization_id, person_id)
        person_email = _row_value(person, "email", 2)
        if person_email and person_email.strip().lower() != email.strip().lower():
            raise AuthorizationError("principal email does not match person")
        existing = self.conn.execute(
            "SELECT * FROM auth_principals WHERE organization_id=? AND person_id=?",
            (organization_id, person_id),
        ).fetchone()
        if existing is not None:
            return self._principal_dict(existing)
        now = _iso(_now())
        item = {
            "id": self._id("principal"),
            "organization_id": organization_id,
            "person_id": person_id,
            "email": email.strip(),
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        self.conn.execute(
            "INSERT INTO auth_principals(id,organization_id,person_id,email,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            tuple(item.values()),
        )
        self.conn.commit()
        return item

    @staticmethod
    def _principal_dict(row: Any) -> dict[str, Any]:
        keys = ("id", "organization_id", "person_id", "email", "status", "created_at", "updated_at")
        return {key: _row_value(row, key, index) for index, key in enumerate(keys)}

    def _principal(self, principal_id: str, organization_id: str | None = None) -> Any:
        row = self.conn.execute("SELECT * FROM auth_principals WHERE id=?", (principal_id,)).fetchone()
        if row is None:
            raise AuthorizationError("principal not found")
        if organization_id is not None and _row_value(row, "organization_id", 1) != organization_id:
            raise AuthorizationError("principal organization mismatch")
        if _row_value(row, "status", 4) != "active":
            raise AuthorizationError("principal is disabled")
        self._person(_row_value(row, "organization_id", 1), _row_value(row, "person_id", 2))
        return row

    def _workspace_role(self, organization_id: str, person_id: str, workspace_id: str) -> str:
        row = self.conn.execute(
            """SELECT wm.role FROM workspace_memberships wm
               JOIN workspace_organization wo ON wo.workspace_id=wm.workspace_id
              WHERE wm.workspace_id=? AND wm.person_id=? AND wo.organization_id=?""",
            (workspace_id, person_id, organization_id),
        ).fetchone()
        if row is None:
            raise AuthorizationError("person is not a member of this organization workspace")
        return str(_row_value(row, "role", 0))

    def _identity(
        self,
        principal: Any,
        auth_type: str,
        scopes: frozenset[str] = frozenset(),
        workspace_id: str | None = None,
    ) -> AuthenticatedIdentity:
        organization_id = _row_value(principal, "organization_id", 1)
        person_id = _row_value(principal, "person_id", 2)
        org_membership = self.conn.execute(
            "SELECT role FROM organization_memberships WHERE organization_id=? AND person_id=?",
            (organization_id, person_id),
        ).fetchone()
        if org_membership is None:
            raise AuthorizationError("principal has no organization membership")
        workspace_role = self._workspace_role(organization_id, person_id, workspace_id) if workspace_id else None
        capabilities = role_capabilities(str(_row_value(org_membership, "role", 0)), workspace_role)
        if auth_type == "api_token":
            capabilities = intersect_token_scopes(capabilities, scopes)
        return AuthenticatedIdentity(
            principal_id=_row_value(principal, "id", 0),
            organization_id=organization_id,
            person_id=person_id,
            auth_type=auth_type,
            capabilities=frozenset(capabilities),
            scopes=scopes,
            workspace_id=workspace_id,
        )

    @staticmethod
    def _new_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def _valid_lifetime(expires_in: timedelta) -> datetime:
        if not isinstance(expires_in, timedelta) or expires_in.total_seconds() <= 0:
            raise ValidationError("expires_in must be positive")
        return _now() + expires_in

    def create_session(self, principal_id: str, expires_in: timedelta = timedelta(days=7)) -> dict[str, Any]:
        principal = self._principal(principal_id)
        expires_at = self._valid_lifetime(expires_in)
        token = self._new_token()
        item = {
            "id": self._id("session"),
            "principal_id": _row_value(principal, "id", 0),
            "token_hash": hash_token(token),
            "created_at": _iso(_now()),
            "expires_at": _iso(expires_at),
            "revoked_at": None,
            "last_seen_at": None,
        }
        self.conn.execute(
            "INSERT INTO auth_sessions(id,principal_id,token_hash,created_at,expires_at,revoked_at,last_seen_at) VALUES (?,?,?,?,?,?,?)",
            tuple(item.values()),
        )
        self.conn.commit()
        return {**item, "token": token}

    def create_api_token(
        self,
        principal_id: str,
        name: str,
        scopes: Iterable[str],
        expires_in: timedelta = timedelta(days=90),
    ) -> dict[str, Any]:
        principal = self._principal(principal_id)
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("token name is required")
        if isinstance(scopes, (str, bytes)):
            raise ValidationError("scopes must be an iterable of capability names")
        scope_set = frozenset(str(scope) for scope in scopes)
        if not scope_set or not scope_set <= set(CAPABILITIES):
            raise ValidationError("scopes must contain known capabilities")
        expires_at = self._valid_lifetime(expires_in)
        token = self._new_token()
        item = {
            "id": self._id("api_token"),
            "principal_id": _row_value(principal, "id", 0),
            "name": name.strip(),
            "token_hash": hash_token(token),
            "scopes": json.dumps(sorted(scope_set), separators=(",", ":")),
            "created_at": _iso(_now()),
            "expires_at": _iso(expires_at),
            "revoked_at": None,
            "last_used_at": None,
        }
        self.conn.execute(
            "INSERT INTO api_tokens(id,principal_id,name,token_hash,scopes,created_at,expires_at,revoked_at,last_used_at) VALUES (?,?,?,?,?,?,?,?,?)",
            tuple(item.values()),
        )
        self.conn.commit()
        return {**item, "token": token, "scopes": sorted(scope_set)}

    def _credential_row(self, table: str, token: str) -> Any:
        if not isinstance(token, str) or not token:
            raise AuthorizationError("invalid credential")
        digest = hash_token(token)
        row = self.conn.execute(f"SELECT * FROM {table} WHERE token_hash=?", (digest,)).fetchone()
        stored = _row_value(row, "token_hash", 2 if table == "auth_sessions" else 3) if row is not None else ""
        if row is None or not hmac.compare_digest(str(stored), digest):
            raise AuthorizationError("invalid credential")
        revoked = _row_value(row, "revoked_at", 5 if table == "auth_sessions" else 7)
        expires = _parse_dt(_row_value(row, "expires_at", 4 if table == "auth_sessions" else 6))
        if revoked is not None:
            raise AuthorizationError("credential revoked")
        if expires is None or expires <= _now():
            raise AuthorizationError("credential expired")
        return row

    def authenticate_session(
        self, token: str, organization_id: str | None = None, workspace_id: str | None = None
    ) -> AuthenticatedIdentity:
        row = self._credential_row("auth_sessions", token)
        principal_id = _row_value(row, "principal_id", 1)
        principal = self._principal(principal_id, organization_id)
        now = _iso(_now())
        self.conn.execute("UPDATE auth_sessions SET last_seen_at=? WHERE id=?", (now, _row_value(row, "id", 0)))
        self.conn.commit()
        return self._identity(principal, "session", workspace_id=workspace_id)

    @staticmethod
    def _scopes(value: Any) -> frozenset[str]:
        try:
            parsed = json.loads(value or "[]")
            if isinstance(parsed, list):
                return frozenset(str(item) for item in parsed)
        except (TypeError, ValueError):
            pass
        return frozenset(part.strip() for part in str(value or "").split(",") if part.strip())

    def authenticate_api_token(
        self, token: str, organization_id: str | None = None, workspace_id: str | None = None
    ) -> AuthenticatedIdentity:
        row = self._credential_row("api_tokens", token)
        principal = self._principal(_row_value(row, "principal_id", 1), organization_id)
        scopes = self._scopes(_row_value(row, "scopes", 4))
        now = _iso(_now())
        self.conn.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now, _row_value(row, "id", 0)))
        self.conn.commit()
        return self._identity(principal, "api_token", scopes=scopes, workspace_id=workspace_id)

    def authenticate(
        self,
        token: str,
        auth_type: str = "session",
        organization_id: str | None = None,
        workspace_id: str | None = None,
    ) -> AuthenticatedIdentity:
        if auth_type == "session":
            return self.authenticate_session(token, organization_id, workspace_id)
        if auth_type in {"api_token", "api-token", "token"}:
            return self.authenticate_api_token(token, organization_id, workspace_id)
        raise ValidationError("auth_type must be session or api_token")

    def authenticate_bearer(
        self, token: str, organization_id: str | None = None, workspace_id: str | None = None
    ) -> AuthenticatedIdentity:
        try:
            if not isinstance(token, str) or not token:
                raise AuthorizationError("invalid credential")
            digest = hash_token(token)
            if self.conn.execute("SELECT 1 FROM auth_sessions WHERE token_hash=?", (digest,)).fetchone():
                return self.authenticate_session(token, organization_id, workspace_id)
            if self.conn.execute("SELECT 1 FROM api_tokens WHERE token_hash=?", (digest,)).fetchone():
                return self.authenticate_api_token(token, organization_id, workspace_id)
            raise AuthorizationError("invalid credential")
        except AuthorizationError as exc:
            raise AuthenticationError("authentication failed") from exc

    def bind_actor(self, identity: AuthenticatedIdentity, workspace_id: str, actor_id: str) -> dict[str, Any]:
        identity.require("auth_manage")
        workspace_role = self._workspace_role(identity.organization_id, identity.person_id, workspace_id)
        if workspace_role not in {"admin", "operator", "viewer"}:
            raise AuthorizationError("workspace binding denied")
        actor = self.conn.execute(
            "SELECT id,workspace_id FROM actors WHERE id=? AND workspace_id=?", (actor_id, workspace_id)
        ).fetchone()
        if actor is None:
            raise AuthorizationError("actor does not belong to workspace")
        now = _iso(_now())
        self.conn.execute(
            "INSERT INTO principal_actor_bindings(principal_id,workspace_id,actor_id,created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(principal_id,workspace_id) DO UPDATE SET actor_id=excluded.actor_id,created_at=excluded.created_at",
            (identity.principal_id, workspace_id, actor_id, now),
        )
        self.conn.commit()
        return {"principal_id": identity.principal_id, "workspace_id": workspace_id, "actor_id": actor_id, "created_at": now}

    def actor_for_identity(self, identity: AuthenticatedIdentity, workspace_id: str) -> str:
        if identity.workspace_id not in {None, workspace_id}:
            raise AuthorizationError("identity workspace mismatch")
        row = self.conn.execute(
            "SELECT actor_id FROM principal_actor_bindings WHERE principal_id=? AND workspace_id=?",
            (identity.principal_id, workspace_id),
        ).fetchone()
        if row is None:
            raise AuthorizationError("principal has no actor binding for this workspace")
        return str(_row_value(row, "actor_id", 0))

    def revoke_session(self, token: str) -> None:
        row = self._credential_row("auth_sessions", token)
        self.conn.execute("UPDATE auth_sessions SET revoked_at=? WHERE id=?", (_iso(_now()), _row_value(row, "id", 0)))
        self.conn.commit()

    def revoke_api_token(self, token: str) -> None:
        row = self._credential_row("api_tokens", token)
        self.conn.execute("UPDATE api_tokens SET revoked_at=? WHERE id=?", (_iso(_now()), _row_value(row, "id", 0)))
        self.conn.commit()

    def rotate_session(self, token: str, expires_in: timedelta = timedelta(days=7)) -> dict[str, Any]:
        row = self._credential_row("auth_sessions", token)
        principal_id = _row_value(row, "principal_id", 1)
        self.conn.execute("UPDATE auth_sessions SET revoked_at=? WHERE id=?", (_iso(_now()), _row_value(row, "id", 0)))
        self.conn.commit()
        return self.create_session(principal_id, expires_in)

    def authorize(
        self,
        identity: AuthenticatedIdentity,
        capability: str,
        organization_id: str,
        workspace_id: str | None = None,
    ) -> AuthenticatedIdentity:
        if capability not in CAPABILITIES:
            raise ValidationError(f"unknown capability: {capability}")
        if identity.organization_id != organization_id or (workspace_id is not None and identity.workspace_id != workspace_id):
            raise AuthorizationError("identity scope mismatch")
        identity.require(capability)
        return identity

    def scope_identity(self, identity: AuthenticatedIdentity, workspace_id: str) -> AuthenticatedIdentity:
        principal = self._principal(identity.principal_id, identity.organization_id)
        return self._identity(principal, identity.auth_type, identity.scopes, workspace_id)

    def identity_for_principal(
        self, principal_id: str, workspace_id: str | None = None
    ) -> AuthenticatedIdentity:
        """Re-authorize a persisted job principal at execution time."""

        principal = self._principal(principal_id)
        return self._identity(principal, "service", workspace_id=workspace_id)
