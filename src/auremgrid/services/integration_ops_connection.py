"""Connection and credential configuration operations for integrations."""

from __future__ import annotations

import json
from typing import Any

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.integration_ops_shared import BOUNDARY_STATUSES, CONFIGURABLE_SOURCES, _now


class IntegrationOpsConnectionMixin:
    def configure(
        self,
        identity: AuthenticatedIdentity,
        source: str,
        expected_account_id: str,
        workspace_mappings: dict[str, str],
        permissions: list[str],
    ) -> dict[str, Any]:
        identity.require("integration_configure")
        source = source.strip().lower()
        if source not in CONFIGURABLE_SOURCES:
            raise ValidationError("unsupported connector")
        expected_account_id = self._normalize_expected_account_id(source, expected_account_id)
        if not expected_account_id:
            raise ValidationError("expected provider account ID is required")
        workspace_mappings = self._canonicalize_mappings(workspace_mappings)
        requested_permissions = self._canonicalize_permissions(permissions)
        self._validate_permissions(source, requested_permissions)
        self._validate_mapping_keys(source, workspace_mappings)
        for workspace_id in workspace_mappings.values():
            scope = self.os.company.workspace_scope(workspace_id)
            if scope is None or scope["organization_id"] != identity.organization_id:
                raise AuthorizationError("workspace mapping is outside the organization")
            if identity.workspace_id not in {None, workspace_id}:
                raise AuthorizationError("workspace mapping is outside the credential scope")
        existing = self.conn.execute(
            "SELECT id FROM integrations WHERE organization_id=? AND source=?",
            (identity.organization_id, source),
        ).fetchone()
        integration_id = existing["id"] if existing else self.os.jobs.new_id("integration")
        if existing and self._has_active_sync(integration_id):
            raise ValidationError("integration cannot be reconfigured while a sync job is active")
        now = _now()
        values = (
            integration_id,
            identity.organization_id,
            source,
            "not_connected",
            json.dumps(workspace_mappings, sort_keys=True, separators=(",", ":")),
            json.dumps(sorted(requested_permissions), separators=(",", ":")),
            None,
            None,
            None,
            0,
            "never_synced",
            now,
        )
        self.conn.execute(
            """INSERT INTO integrations(
              id,organization_id,source,status,workspace_mappings,permissions,sync_cursor,
              last_sync_at,last_error,object_count,health,created_at,expected_account_id,
              provider_account_id,provider_account_name,granted_permissions,credential_verified_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(organization_id,source) DO UPDATE SET
              status='not_connected',workspace_mappings=excluded.workspace_mappings,
              permissions=excluded.permissions,sync_cursor=NULL,last_sync_at=NULL,
              last_error=NULL,object_count=0,health='never_synced',
              expected_account_id=excluded.expected_account_id,provider_account_id=NULL,
              provider_account_name=NULL,granted_permissions='[]',credential_verified_at=NULL""",
            values + (expected_account_id,None,None,"[]",None),
        )
        self.conn.commit()
        return self.get(identity, integration_id)

    def get(self, identity: AuthenticatedIdentity, integration_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM integrations WHERE id=? AND organization_id=?",
            (integration_id, identity.organization_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("integration not found")
        item = dict(row)
        item["workspace_mappings"] = json.loads(item["workspace_mappings"])
        item["permissions"] = json.loads(item["permissions"])
        item["granted_permissions"] = json.loads(item.get("granted_permissions") or "[]")
        item["live_enabled"] = item["source"] in self._live_sources()
        item["boundary_status"] = BOUNDARY_STATUSES.get(item["source"], "NOT IMPLEMENTED")
        if identity.workspace_id is not None and set(item["workspace_mappings"].values()) != {identity.workspace_id}:
            raise AuthorizationError("integration is outside the credential workspace scope")
        item["credential"] = self._credential_metadata(integration_id)
        return item

    def list(self, identity: AuthenticatedIdentity) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT id FROM integrations WHERE organization_id=? ORDER BY source", (identity.organization_id,)
        ).fetchall():
            try:
                result.append(self.get(identity, row["id"]))
            except AuthorizationError:
                continue
        return result

    def bind_credential(
        self,
        identity: AuthenticatedIdentity,
        integration_id: str,
        name: str,
        reference: str,
        scopes: list[str],
    ) -> dict[str, Any]:
        integration = self.get(identity, integration_id)
        existing = self.conn.execute(
            "SELECT id FROM secret_bindings WHERE integration_id=? AND revoked_at IS NULL", (integration_id,)
        ).fetchone()
        if existing:
            raise ValidationError("integration already has an active credential binding; rotate or revoke it")
        return self.os.secrets.create(
            identity, identity.organization_id, name, integration["source"], reference,
            scopes, integration_id=integration_id,
        )

    def verify(self, identity: AuthenticatedIdentity, integration_id: str) -> dict[str, Any]:
        identity.require("integration_sync")
        integration = self.get(identity, integration_id)
        if integration["source"] not in self._live_sources():
            raise ValidationError("connector provider verification is not enabled")
        secret = self._resolve(identity, integration)
        if self._is_google(integration["source"]):
            secret, granted = self._refresh_google_access(integration, secret)
            integration = {**integration, "_runtime_granted_permissions": granted}
        verified = self._provider_verify(integration,secret)
        account_id, account_name, granted = self._verified_account(integration["source"], verified)
        if not self._account_ids_match(integration["source"], account_id, integration["expected_account_id"]):
            raise AuthorizationError("provider account identity mismatch")
        missing = self._missing_permissions(integration["source"], set(integration["permissions"]), set(granted))
        if missing:
            raise AuthorizationError("provider credential is missing required permissions")
        now = _now()
        self.conn.execute(
            """UPDATE integrations SET status='authorized',health='never_synced',last_error=NULL,
            provider_account_id=?,provider_account_name=?,granted_permissions=?,credential_verified_at=? WHERE id=?""",
            (account_id,account_name,json.dumps(sorted(set(granted)),separators=(",",":")),now,integration_id),
        )
        self.conn.commit()
        binding=self.conn.execute(
            "SELECT id FROM secret_bindings WHERE integration_id=? AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (integration_id,),
        ).fetchone()
        if binding is not None:
            self.os.secrets.mark_verified(identity,binding["id"])
        return {"integration": self.get(identity, integration_id), "provider_identity": self._public_identity(verified)}

