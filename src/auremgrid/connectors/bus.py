from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from auremgrid.domain.models import IngestResult
if TYPE_CHECKING:
    from auremgrid.services.brain import CompanyOS


@dataclass(frozen=True)
class ConnectorEvent:
    workspace_id: str
    source_key: str
    locator: str
    content: str
    connector: str
    observed_at: datetime | None = None
    allowed_actor_ids: tuple[str, ...] = ()
    media_type: str = "text/markdown"
    trust_level: str = "internal"


class Connector(Protocol):
    name: str

    def pull(self) -> list[ConnectorEvent]:
        ...


class ConnectorBus:
    """Ingestion bus. Slack, Drive, ClickUp, and Figma become connectors later."""

    def __init__(self, os: CompanyOS, actor_id: str) -> None:
        self.os = os
        self.actor_id = actor_id
        self.connectors: list[Connector] = []

    def register(self, connector: Connector) -> None:
        self.connectors.append(connector)

    def sync(self) -> list[IngestResult]:
        results: list[IngestResult] = []
        for connector in self.connectors:
            for event in connector.pull():
                results.append(
                    self.os.ingest_text(
                        workspace_id=event.workspace_id,
                        actor_id=self.actor_id,
                        source_key=event.source_key,
                        content=event.content,
                        locator=event.locator,
                        allowed_actor_ids=list(event.allowed_actor_ids),
                        observed_at=event.observed_at,
                        media_type=event.media_type,
                        trust_level=event.trust_level,
                    )
                )
        return results
