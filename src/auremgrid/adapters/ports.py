from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Protocol


class GraphProjectionPort(Protocol):
    """Rebuildable graph projection boundary; Auremgrid remains canonical truth."""

    name: str
    requires_full_workspace_access: bool
    uses_current_time_search: bool

    def upsert_episode(
        self, workspace_id: str, source_id: str, content: str, observed_at: str,
        generation: str | None = None, document_id: str | None = None,
        recorded_at: str | None = None
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

    def generation_is_complete(
        self, workspace_id: str, generation: str, episodes: Iterable[dict[str, Any]]
    ) -> bool: ...

    def health(self) -> dict[str, Any]: ...


class UpstreamGraphitiClient(Protocol):
    """Async subset Auremgrid needs from an upstream Graphiti-compatible client."""

    async def build_indices_and_constraints(self) -> None: ...

    async def add_episode(self, **kwargs: Any) -> Any: ...

    async def search(self, **kwargs: Any) -> Any: ...

    async def find_episode_by_name(self, name: str, group_id: str) -> Any: ...

    async def close(self) -> None: ...


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
