from __future__ import annotations

from typing import Protocol, Sequence

from auremgrid.adapters.hybrid import cosine, hashed_embedding


class EmbeddingProvider(Protocol):
    name: str
    dimensions: int
    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...


class DeterministicFallbackEmbeddingProvider:
    """Offline fallback; lexical and deterministic, explicitly not a semantic model."""
    name = "deterministic_lexical_fallback"
    dimensions = 64
    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [hashed_embedding(text, self.dimensions) for text in texts]


class VectorIndex(Protocol):
    def upsert(self, workspace_id: str, key: str, vector: tuple[float, ...]) -> None: ...
    def search(self, workspace_id: str, vector: tuple[float, ...], limit: int = 8) -> list[tuple[str, float]]: ...


class LocalVectorIndex:
    def __init__(self) -> None: self._vectors: dict[tuple[str,str],tuple[float,...]] = {}
    def upsert(self, workspace_id: str, key: str, vector: tuple[float, ...]) -> None: self._vectors[(workspace_id,key)] = vector
    def search(self, workspace_id: str, vector: tuple[float, ...], limit: int = 8) -> list[tuple[str,float]]:
        hits=[(key,cosine(vector,candidate)) for (ws,key),candidate in self._vectors.items() if ws==workspace_id]
        return sorted((hit for hit in hits if hit[1]>0),key=lambda hit:hit[1],reverse=True)[:limit]
