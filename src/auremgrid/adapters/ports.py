from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Protocol


class GraphProjectionPort(Protocol):
    """Rebuildable graph projection boundary; Auremgrid remains canonical truth."""

    name: str

    def upsert_episode(
        self, workspace_id: str, source_id: str, content: str, observed_at: str, generation: str | None = None
    ) -> None: ...

    def search(
        self,
        workspace_id: str,
        query: str,
        allowed_source_ids: Iterable[str],
        as_of: datetime | None = None,
        limit: int = 8,
        generation: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def rebuild_workspace(self, generation: str, episodes: Iterable[dict[str, Any]]) -> None: ...

    def health(self) -> dict[str, Any]: ...


class GraphAdapter(Protocol):
    """Temporal graph backend. Graphiti is the first intended implementation."""

    name: str

    def upsert_episode(self, workspace_id: str, source_id: str, content: str, observed_at: str) -> None:
        ...

    def search(self, workspace_id: str, query: str, as_of: str | None = None) -> list[dict[str, Any]]:
        ...


class RetrievalAdapter(Protocol):
    """Document retrieval backend. LightRAG or RAGFlow can implement this later."""

    name: str

    def index(self, workspace_id: str, document_id: str, content: str) -> None:
        ...

    def search(self, workspace_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        ...


class MemoryAdapter(Protocol):
    """Subjective memory backend. Mem0 can implement this later."""

    name: str

    def remember(self, workspace_id: str, actor_id: str, content: str, kind: str) -> None:
        ...

    def recall(self, workspace_id: str, actor_id: str, query: str) -> list[dict[str, Any]]:
        ...


class NullGraphAdapter:
    name = "null-graph"

    def upsert_episode(self, workspace_id: str, source_id: str, content: str, observed_at: str) -> None:
        return None

    def search(self, workspace_id: str, query: str, as_of: str | None = None) -> list[dict[str, Any]]:
        return []


class NullRetrievalAdapter:
    name = "sqlite-fts"

    def index(self, workspace_id: str, document_id: str, content: str) -> None:
        return None

    def search(self, workspace_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        return []


class NullMemoryAdapter:
    name = "auremgrid-memory"

    def remember(self, workspace_id: str, actor_id: str, content: str, kind: str) -> None:
        return None

    def recall(self, workspace_id: str, actor_id: str, query: str) -> list[dict[str, Any]]:
        return []
