from __future__ import annotations

from typing import Any


class LettaAdapter:
    """Stateful agent identity. This is the agent's memory, not the client's truth."""

    name = "local_letta_style_projection"
    role = "stateful agent identity"
    license = "Apache-2.0"

    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}

    def bind(self, workspace_id: str, actor_id: str, persona: str) -> dict[str, Any]:
        key = f"{workspace_id}:{actor_id}"
        profile = self.agents.setdefault(
            key,
            {
                "workspace_id": workspace_id,
                "actor_id": actor_id,
                "persona": persona,
                "skills": [],
            },
        )
        profile["persona"] = persona
        return profile

    def teach(self, workspace_id: str, actor_id: str, skill: str) -> dict[str, Any]:
        profile = self.bind(workspace_id, actor_id, "agency operator")
        if skill not in profile["skills"]:
            profile["skills"].append(skill)
        return profile

    def profile(self, workspace_id: str, actor_id: str) -> dict[str, Any] | None:
        return self.agents.get(f"{workspace_id}:{actor_id}")
