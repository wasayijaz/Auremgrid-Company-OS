from __future__ import annotations

from pathlib import Path

from auremgrid.connectors.bus import ConnectorEvent


class LocalMarkdownConnector:
    name = "local-markdown"

    def __init__(self, workspace_id: str, root: str | Path, allowed_actor_ids: list[str] | None = None) -> None:
        self.workspace_id = workspace_id
        self.root = Path(root)
        self.allowed_actor_ids = tuple(allowed_actor_ids or ())

    def pull(self) -> list[ConnectorEvent]:
        events: list[ConnectorEvent] = []
        if not self.root.exists():
            return events
        for path in sorted(self.root.rglob("*.md")):
            allowed = list(self.allowed_actor_ids)
            if "restricted" in path.name and not allowed:
                allowed = []
            events.append(
                ConnectorEvent(
                    workspace_id=self.workspace_id,
                    source_key=path.name,
                    locator=str(path),
                    content=path.read_text(encoding="utf-8"),
                    connector=self.name,
                    allowed_actor_ids=tuple(allowed),
                )
            )
        return events
