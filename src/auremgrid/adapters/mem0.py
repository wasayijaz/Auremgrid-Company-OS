from __future__ import annotations

from typing import Any

from auremgrid.adapters.hybrid import tokens


class Mem0Adapter:
    """Subjective preference and interaction memory. Never canonical client truth."""

    name = "local_mem0_style_projection"
    role = "preference and interaction memory"
    license = "Apache-2.0"

    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []

    def remember(self, workspace_id: str, actor_id: str, content: str, kind: str) -> dict[str, Any]:
        note = {
            "workspace_id": workspace_id,
            "actor_id": actor_id,
            "kind": kind,
            "content": content,
        }
        self.notes.append(note)
        return note

    def recall(self, workspace_id: str, query: str) -> list[dict[str, Any]]:
        query_tokens = set(tokens(query))
        hits: list[dict[str, Any]] = []
        for note in self.notes:
            if note["workspace_id"] != workspace_id:
                continue
            if query_tokens & set(tokens(note["content"])):
                hits.append(note)
        return hits
