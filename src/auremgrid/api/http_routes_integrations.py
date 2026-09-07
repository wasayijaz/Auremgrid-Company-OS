"""Integrations, OAuth, connector catalog, and provider imports route mixin."""
from __future__ import annotations

from typing import Any

from auremgrid.connectors.catalog import connector_catalog
from auremgrid.api.http_shared import (
    _need,
    _optional_str,
    _provider_import_adapter,
)
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


class HttpRoutesIntegrationsMixin:
    def _get_integrations(self, parsed: Any, params: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/integrations":
            assert identity is not None
            self._json(200, {"integrations": self.os.integrations.list(identity)})
            return True
        if parsed.path == "/connectors/catalog":
            self._json(200, {"connectors": connector_catalog()})
            return True
        if parsed.path == "/operator/health":
            assert identity is not None
            worker_id = params.get("worker_id", "default")
            self._json(200, self.os.scheduler(identity.organization_id, params.get("workspace_id"), worker_id).health())
            return True
        if parsed.path == "/provider-imports/status":
            assert identity is not None
            rows = self.os.store.conn.execute(
                "SELECT * FROM provider_import_cursors WHERE organization_id=? ORDER BY updated_at DESC",
                (identity.organization_id,),
            ).fetchall()
            quarantines = self.os.store.conn.execute("SELECT provider,object_type,external_id,reason,evidence_digest,created_at FROM provider_import_quarantines WHERE organization_id=? ORDER BY created_at DESC LIMIT 50", (identity.organization_id,)).fetchall()
            self._json(200, {"imports": [dict(row) for row in rows], "quarantines": [dict(row) for row in quarantines]})
            return True
        if parsed.path == "/webhooks/provider/status":
            assert identity is not None
            from auremgrid.services.integration_security import WebhookIntakeService
            self._json(200, WebhookIntakeService(self.os.store.conn, self.os.jobs.new_id).status(identity))
            return True
        if parsed.path == "/oauth/callback":
            if "code_verifier" in params:
                raise ValidationError("code_verifier is not accepted on OAuth callback")
            item = self.os.oauth_service().complete(_need(params, "state"), _need(params, "code"),
                None, _need(params, "redirect_uri"), _need(params, "provider"))
            self._json(200, item)
            return True
        if parsed.path.startswith("/oauth/install/") and parsed.path.endswith("/health"):
            assert identity is not None
            installation_id = parsed.path.split("/")[3]
            self._json(200, self.os.oauth_service().health(identity, installation_id))
            return True
        return False

    def _post_integrations(self, parsed: Any, payload: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/integrations":
            item = self.os.integrations.configure(identity, _need(payload, "source"), _need(payload, "expected_account_id"), payload.get("workspace_mappings") or {},
                [str(x) for x in payload.get("permissions", [])])
            self._json(201, item)
            return True
        if parsed.path == "/oauth/begin":
            item = self.os.oauth_service().begin(identity, _need(payload, "organization_id"),
                _optional_str(payload.get("workspace_id")), _need(payload, "provider"),
                _need(payload, "client_id"), _need(payload, "redirect_uri"), _need(payload, "scope"),
                _optional_str(payload.get("installation_id")))
            item.pop("code_verifier", None)
            self._json(200, item)
            return True
        if parsed.path == "/oauth/callback":
            if "code_verifier" in payload:
                raise ValidationError("code_verifier is not accepted on OAuth callback")
            item = self.os.oauth_service().complete(_need(payload, "state"), _need(payload, "code"),
                None, _need(payload, "redirect_uri"), _need(payload, "provider"))
            self._json(200, item)
            return True
        if parsed.path == "/oauth/revoke":
            item = self.os.oauth_service().revoke(identity, _need(payload, "installation_id"))
            self._json(200, item)
            return True
        if parsed.path in {"/provider-imports/preview", "/provider-imports/sync"}:
            mappings = payload.get("workspace_mappings") or {}
            provider = _need(payload, "provider")
            adapter = _provider_import_adapter(provider, payload.get("_transport"))
            if parsed.path.endswith("preview"):
                result = self.os.provider_imports.preview(identity, provider, _need(payload, "account_id"), mappings,
                    _need(payload, "resource"), _optional_str(payload.get("cursor")), adapter)
            else:
                result = self.os.provider_imports.pull(identity, provider, _need(payload, "account_id"), mappings,
                    _need(payload, "resource"), _optional_str(payload.get("cursor")), adapter)
            self._json(200, result)
            return True
        if parsed.path in {"/operator/pause", "/operator/resume"}:
            scheduler = self.os.scheduler(identity.organization_id, _optional_str(payload.get("workspace_id")), _need(payload, "worker_id"))
            self._json(200, scheduler.set_paused(parsed.path.endswith("pause")))
            return True
        if parsed.path == "/integrations/credentials":
            item = self.os.integrations.bind_credential(identity, _need(payload, "integration_id"), _need(payload, "name"),
                _need(payload, "reference"), [str(x) for x in payload.get("scopes", [])])
            self._json(201, item)
            return True
        if parsed.path == "/integrations/verify":
            self._json(200, self.os.integrations.verify(identity, _need(payload, "integration_id")))
            return True
        if parsed.path == "/integrations/sync":
            integration_id = _need(payload, "integration_id")
            items = self.os.integrations.enqueue_sync(identity, integration_id, int(payload.get("priority", 0)),
                int(payload.get("max_attempts", 5)), _optional_str(payload.get("idempotency_key")))
            self._json(202, {"jobs": items})
            return True
        return False

