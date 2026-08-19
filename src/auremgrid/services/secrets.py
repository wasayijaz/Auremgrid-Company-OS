from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity


SENSITIVE_KEYS = {
    "authorization", "api_key", "apikey", "password", "secret", "token",
    "access_token", "refresh_token", "client_secret", "cookie", "set-cookie",
}
ENV_REFERENCE = re.compile(r"^env:([A-Z][A-Z0-9_]*)$")
BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+\-/]+=*")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SecretStore(Protocol):
    def resolve(self, reference: str) -> str: ...


class EnvironmentSecretStore:
    """Resolve explicit environment references without persisting secret values."""

    def __init__(self, allowed_names: set[str] | None = None) -> None:
        self.allowed_names = frozenset(allowed_names) if allowed_names is not None else None

    def resolve(self, reference: str) -> str:
        match = ENV_REFERENCE.fullmatch(reference)
        if match is None:
            raise ValidationError("secret reference must use env:UPPER_CASE_NAME")
        name = match.group(1)
        if self.allowed_names is not None and name not in self.allowed_names:
            raise AuthorizationError("secret reference is not allowlisted")
        value = os.environ.get(name)
        if not value:
            raise NotFoundError("referenced secret is unavailable")
        return value


def redact(value: Any, known_secrets: tuple[str, ...] = ()) -> Any:
    """Recursively remove common credential material from logs and persisted payloads."""

    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SENSITIVE_KEYS else redact(item, known_secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, known_secrets) for item in value]
    if isinstance(value, str):
        result = BEARER_VALUE.sub("Bearer [REDACTED]", value)
        for secret in known_secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    return value


class SecretBindingService:
    def __init__(self, conn: sqlite3.Connection, new_id: Callable[[str], str], store: SecretStore) -> None:
        self.conn = conn
        self.new_id = new_id
        self.store = store

    @staticmethod
    def _require_scope(identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None, capability: str) -> None:
        if identity.organization_id != organization_id:
            raise AuthorizationError("identity scope mismatch")
        if workspace_id is not None and identity.workspace_id not in {None, workspace_id}:
            raise AuthorizationError("identity workspace mismatch")
        identity.require(capability)

    def create(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        name: str,
        provider: str,
        reference: str,
        scopes: list[str],
        workspace_id: str | None = None,
        integration_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_scope(identity, organization_id, workspace_id, "secret_bind")
        if not name.strip() or not provider.strip() or ENV_REFERENCE.fullmatch(reference) is None:
            raise ValidationError("name, provider, and an env:UPPER_CASE_NAME reference are required")
        if not scopes or any(not isinstance(scope, str) or not scope.strip() for scope in scopes):
            raise ValidationError("at least one non-empty secret scope is required")
        now = _now()
        item = {
            "id": self.new_id("secret_binding"),
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "integration_id": integration_id,
            "name": name.strip(),
            "provider": provider.strip(),
            "reference": reference,
            "scopes": json.dumps(sorted(set(scopes)), separators=(",", ":")),
            "fingerprint": hashlib.sha256(reference.encode("utf-8")).hexdigest()[:16],
            "status": "unverified",
            "last_verified_at": None,
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
        }
        self.conn.execute(
            "INSERT INTO secret_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(item.values())
        )
        self.conn.commit()
        return self._public(item)

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item["id"], "organization_id": item["organization_id"], "workspace_id": item["workspace_id"],
            "integration_id": item["integration_id"], "name": item["name"], "provider": item["provider"],
            "reference": item["reference"], "scopes": json.loads(item["scopes"]), "fingerprint": item["fingerprint"],
            "status": item["status"], "last_verified_at": item["last_verified_at"], "created_at": item["created_at"],
            "updated_at": item["updated_at"], "revoked_at": item["revoked_at"],
        }

    def _get(self, binding_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM secret_bindings WHERE id=?", (binding_id,)).fetchone()
        if row is None:
            raise NotFoundError("secret binding not found")
        return dict(row)

    def resolve_for_use(
        self, identity: AuthenticatedIdentity, binding_id: str, required_scope: str
    ) -> str:
        item = self._get(binding_id)
        self._require_scope(identity, item["organization_id"], item["workspace_id"], "integration_sync")
        if item["status"] == "revoked" or item["revoked_at"] is not None:
            raise AuthorizationError("secret binding is revoked")
        if required_scope not in json.loads(item["scopes"]):
            raise AuthorizationError("secret binding scope denied")
        secret = self.store.resolve(item["reference"])
        now = _now()
        self.conn.execute(
            "UPDATE secret_bindings SET status='active',last_verified_at=?,updated_at=? WHERE id=?",
            (now, now, binding_id),
        )
        self.conn.commit()
        return secret

    def rotate_reference(
        self, identity: AuthenticatedIdentity, binding_id: str, reference: str
    ) -> dict[str, Any]:
        item = self._get(binding_id)
        self._require_scope(identity, item["organization_id"], item["workspace_id"], "secret_rotate")
        if ENV_REFERENCE.fullmatch(reference) is None:
            raise ValidationError("secret reference must use env:UPPER_CASE_NAME")
        now = _now()
        fingerprint = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:16]
        self.conn.execute(
            "UPDATE secret_bindings SET reference=?,fingerprint=?,status='unverified',last_verified_at=NULL,updated_at=? WHERE id=?",
            (reference, fingerprint, now, binding_id),
        )
        self.conn.commit()
        return self._public(self._get(binding_id))

    def revoke(self, identity: AuthenticatedIdentity, binding_id: str) -> dict[str, Any]:
        item = self._get(binding_id)
        self._require_scope(identity, item["organization_id"], item["workspace_id"], "secret_rotate")
        now = _now()
        self.conn.execute(
            "UPDATE secret_bindings SET status='revoked',revoked_at=?,updated_at=? WHERE id=?",
            (now, now, binding_id),
        )
        self.conn.commit()
        return self._public(self._get(binding_id))
