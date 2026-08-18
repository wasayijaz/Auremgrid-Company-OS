from __future__ import annotations

from typing import Any


class OnyxAdapter:
    """Company-knowledge shell: connector catalog and permissioned source index."""

    name = "onyx"
    role = "connector catalog and knowledge UI contract"
    license = "MIT"

    catalog = (
        {"id": "slack", "label": "Slack / chat", "status": "simulated"},
        {"id": "drive", "label": "Drive / files", "status": "simulated"},
        {"id": "tasks", "label": "Task tracker", "status": "simulated"},
        {"id": "design", "label": "Design files", "status": "simulated"},
        {"id": "email", "label": "Email / meetings", "status": "planned"},
    )

    def catalog_for(self, workspace_id: str) -> list[dict[str, Any]]:
        return [{"workspace_id": workspace_id, **item} for item in self.catalog]
