from __future__ import annotations

import struct
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from auremgrid.adapters.hybrid import cosine, hashed_embedding


class EmbeddingProviderError(RuntimeError):
    """The configured provider could not produce vectors."""


@dataclass(frozen=True)
class EmbeddingHealth:
    provider: str
    model: str
    version: str
    dimensions: int
    status: str = "healthy"
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "version": self.version,
            "dimensions": self.dimensions,
            "status": self.status,
            "detail": self.detail,
        }


class EmbeddingProvider(Protocol):
    name: str
    model: str
    version: str
    dimensions: int
    def health(self) -> EmbeddingHealth: ...
    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]: ...


class DeterministicFallbackEmbeddingProvider:
    """Offline fallback; lexical and deterministic, explicitly not a semantic model."""
    name = "deterministic_lexical_fallback"
    model = "sha256-token-buckets"
    version = "1"
    dimensions = 64
    def health(self) -> EmbeddingHealth:
        return EmbeddingHealth(self.name, self.model, self.version, self.dimensions)
    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [hashed_embedding(text, self.dimensions) for text in texts]


class SentenceTransformerEmbeddingProvider:
    """Optional local-files-only provider. Loading is lazy and never downloads."""

    def __init__(self, model_path: str | Path, *, version: str = "1") -> None:
        self.model_path = Path(model_path)
        self.name = "sentence_transformers_local"
        self.model = self.model_path.name or str(self.model_path)
        self.version = version
        self.dimensions = 0
        self._model: Any | None = None
        self._failure: str | None = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if not self.model_path.is_dir():
            self._failure = f"local embedding model is unavailable: {self.model_path}"
            raise EmbeddingProviderError(self._failure)
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
            self._model = SentenceTransformer(str(self.model_path), local_files_only=True)
            self.dimensions = int(self._model.get_sentence_embedding_dimension())
            return self._model
        except Exception as exc:
            self._failure = str(exc)
            raise EmbeddingProviderError(f"local embedding provider unavailable: {exc}") from exc

    def health(self) -> EmbeddingHealth:
        if self._failure:
            return EmbeddingHealth(self.name, self.model, self.version, self.dimensions, "degraded", self._failure)
        return EmbeddingHealth(self.name, self.model, self.version, self.dimensions, "healthy")

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        model = self._load()
        vectors = model.encode(list(texts), normalize_embeddings=True)
        return [tuple(float(value) for value in vector) for vector in vectors]


class VectorIndex(Protocol):
    def upsert(self, workspace_id: str, key: str, vector: tuple[float, ...]) -> None: ...
    def search(self, workspace_id: str, vector: tuple[float, ...], allowed_document_ids: Sequence[str], limit: int = 8) -> list[tuple[str, float]]: ...


class LocalVectorIndex:
    def __init__(self) -> None: self._vectors: dict[tuple[str,str],tuple[float,...]] = {}
    def upsert(self, workspace_id: str, key: str, vector: tuple[float, ...]) -> None: self._vectors[(workspace_id,key)] = vector
    def search(self, workspace_id: str, vector: tuple[float, ...], allowed_document_ids: Sequence[str], limit: int = 8) -> list[tuple[str,float]]:
        allowed = set(allowed_document_ids)
        hits=[(key,cosine(vector,candidate)) for (ws,key),candidate in self._vectors.items() if ws==workspace_id and key in allowed]
        return sorted((hit for hit in hits if hit[1]>0),key=lambda hit:hit[1],reverse=True)[:limit]


def pack_float32(vector: Sequence[float], dimensions: int) -> bytes:
    if len(vector) != dimensions:
        raise ValueError("embedding dimensions do not match provider metadata")
    values = tuple(float(value) for value in vector)
    if dimensions <= 0 or not all(math.isfinite(value) for value in values):
        raise ValueError("embedding vector contains invalid values")
    return struct.pack(f"<{dimensions}f", *values)


def unpack_float32(blob: bytes, dimensions: int) -> tuple[float, ...]:
    if dimensions <= 0 or len(blob) != dimensions * 4:
        raise ValueError("embedding blob has invalid float32 length")
    values = tuple(struct.unpack(f"<{dimensions}f", blob))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("embedding blob contains invalid values")
    return values


class SqliteVectorIndex:
    """Durable, tenant-scoped float32 vector index over canonical documents."""

    def __init__(self, store: Any, provider: EmbeddingProvider) -> None:
        self.store = store
        self.provider = provider

    def upsert(self, workspace_id: str, key: str, vector: tuple[float, ...]) -> None:
        blob = pack_float32(vector, self.provider.dimensions)
        now = self.store.now_iso()
        self.store.conn.execute(
            """INSERT INTO document_embedding_projection(
                   workspace_id,document_id,provider,model,provider_version,dimensions,
                   vector,health,last_error,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(workspace_id,document_id,provider,model) DO UPDATE SET
                   provider_version=excluded.provider_version,dimensions=excluded.dimensions,
                   vector=excluded.vector,health=excluded.health,last_error=excluded.last_error,
                   updated_at=excluded.updated_at""",
            (workspace_id,key,self.provider.name,self.provider.model,self.provider.version,
             self.provider.dimensions,blob,"healthy",None,now,now),
        )

    def search(self, workspace_id: str, vector: tuple[float, ...], allowed_document_ids: Sequence[str], limit: int = 8) -> list[tuple[str, float]]:
        if not allowed_document_ids:
            return []
        # Validate dimensions before querying; callers cannot accidentally mix models.
        pack_float32(vector, self.provider.dimensions)
        placeholders = ",".join("?" for _ in allowed_document_ids)
        rows = self.store.conn.execute(
            f"""SELECT document_id,vector,dimensions FROM document_embedding_projection
                WHERE workspace_id=? AND provider=? AND model=? AND provider_version=?
                  AND dimensions=? AND document_id IN ({placeholders})""",
            (workspace_id,self.provider.name,self.provider.model,self.provider.version,
             self.provider.dimensions,*allowed_document_ids),
        ).fetchall()
        hits=[]
        for row in rows:
            candidate = unpack_float32(row["vector"], int(row["dimensions"]))
            score = cosine(vector, candidate)
            if score > 0:
                hits.append((row["document_id"], score))
        return sorted(hits, key=lambda hit: hit[1], reverse=True)[:limit]
