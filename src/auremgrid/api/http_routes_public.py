from __future__ import annotations

from typing import Any

class HttpRoutesPublicMixin:
    def _get_public(self, parsed: Any, params: dict[str, list[str]], identity: Any) -> bool:
        if parsed.path == "/health":
            self._json(200, {"ok": True, "schema_version": self.os.store.schema_version})
            return True
        if parsed.path == "/metrics":
            from auremgrid.observability import get_metrics
            self._json(200, get_metrics().snapshot())
            return True
        if parsed.path == "/health/detailed":
            from auremgrid.lifecycle import startup_health
            from auremgrid.observability import get_metrics
            health_warnings = startup_health(self.os.store.raw_connection, getattr(self.os.store, "path", ":memory:"))
            self._json(200, {
                "ok": len(health_warnings) == 0,
                "schema_version": self.os.store.schema_version,
                "warnings": health_warnings,
                "metrics": get_metrics().snapshot(),
            })
            return True
        if parsed.path == "/onboarding/templates":
            self._json(200, self.os.onboarding.templates()); return True
        if parsed.path in {"/", "/dashboard"}:
            from auremgrid.api.http import _dashboard_html
            self._html(200, _dashboard_html())
            return True
        if parsed.path.startswith("/dashboard-assets/"):
            relative_path = parsed.path.removeprefix("/dashboard-assets/")
            self._dashboard_asset(relative_path)
            return True
        return False
