from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from auremgrid.connectors.bus import ConnectorEvent


class ConnectorTransport(Protocol):
    def __call__(self, cursor: str | None) -> tuple[list[ConnectorEvent], str | None]: ...


@dataclass(frozen=True)
class ConnectorDefinition:
    source: str
    label: str
    object_types: tuple[str, ...]
    permission_scopes: tuple[str, ...]
    default_status: str = "not_connected"
    live_enabled: bool = False
    install_mode: str = "manual"
    oauth_provider: str | None = None
    # Human-readable provider boundary. This is intentionally separate from
    # connection ``status`` (which describes an organization's configured
    # installation) so catalogs cannot imply that an import adapter is live.
    boundary_status: str = "DISABLED"


TARGET_CONNECTORS = (
    ConnectorDefinition("slack","Slack",("messages","threads","participants"),("channels:read","channels:history"),live_enabled=True,boundary_status="LIVE READ"),
    ConnectorDefinition("google_drive","Google Drive",("files","documents","permissions"),("https://www.googleapis.com/auth/drive.readonly",),live_enabled=True,install_mode="oauth",oauth_provider="google",boundary_status="LIVE READ"),
    ConnectorDefinition("gmail","Gmail",("threads","messages","participants"),("https://www.googleapis.com/auth/gmail.readonly",),live_enabled=True,install_mode="oauth",oauth_provider="google",boundary_status="LIVE READ"),
    ConnectorDefinition("clickup","ClickUp",("projects","tasks","comments"),("authorized_team",),live_enabled=True,boundary_status="LIVE READ"),
    ConnectorDefinition("figma","Figma",("files",),("current_user:read","file_metadata:read","file_content:read"),live_enabled=True,boundary_status="LIVE READ"),
    ConnectorDefinition("github","GitHub",("repositories","issues","pull_requests"),("repo:read",),boundary_status="DISABLED"),
    ConnectorDefinition("fireflies","Fireflies",("meetings","transcripts","participants"),("transcripts:read",),live_enabled=True,boundary_status="LIVE READ"),
    ConnectorDefinition("meta_ads","Meta Ads",("campaigns","ad_sets","ads","insights"),("ads_read",),boundary_status="IMPORT ONLY"),
    ConnectorDefinition("google_ads","Google Ads",("campaigns","ad_groups","ads","metrics"),("adwords",),boundary_status="IMPORT ONLY"),
    ConnectorDefinition("stripe_accounting","Stripe / accounting",("invoices","payments","revenue","costs"),("finance:read",),boundary_status="IMPORT ONLY"),
    ConnectorDefinition("crm","CRM",("contacts","opportunities"),("import:read",),boundary_status="IMPORT ONLY"),
)


class ConfiguredConnector:
    """Credential-neutral connector interface; transport is injected outside the ledger."""

    def __init__(self, definition: ConnectorDefinition, workspace_mappings: Mapping[str,str],
        transport: ConnectorTransport | None = None, cursor: str | None = None) -> None:
        self.definition=definition;self.name=definition.source;self.workspace_mappings=dict(workspace_mappings)
        self.transport=transport;self.cursor=cursor;self.next_cursor=cursor

    @property
    def status(self) -> str: return "configured" if self.transport else self.definition.default_status

    def pull(self) -> list[ConnectorEvent]:
        if self.transport is None:return []
        events,next_cursor=self.transport(self.cursor)
        allowed=set(self.workspace_mappings)
        if any(event.workspace_id not in allowed for event in events):
            raise ValueError("connector event workspace is not mapped")
        self.next_cursor=next_cursor
        return events


def connector_catalog() -> list[dict[str,object]]:
    return [{"source":item.source,"label":item.label,"object_types":list(item.object_types),
        "permission_scopes":list(item.permission_scopes),"status":item.default_status,
        "live_enabled":item.live_enabled,"install_mode":item.install_mode,
        "oauth_provider":item.oauth_provider,
        "boundary_status": item.boundary_status,
        "oauth": {"provider": item.oauth_provider, "pkce": True, "revoke": True,
                   "health": True} if item.install_mode == "oauth" else None} for item in TARGET_CONNECTORS]
