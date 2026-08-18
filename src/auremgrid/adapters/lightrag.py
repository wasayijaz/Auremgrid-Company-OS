from __future__ import annotations

from typing import Any

from auremgrid.adapters.hybrid import cosine, hashed_embedding
from auremgrid.domain.models import Document


class LightRAGAdapter:
    """Lightweight graph RAG over relatively static approved documents."""

    name = "lightrag"
    role = "static corpus graph RAG"
    license = "MIT"

    def __init__(self) -> None:
        self.nodes: list[dict[str, Any]] = []

    def index(self, document: Document) -> None:
        self.nodes.append(
            {
                "workspace_id": document.workspace_id,
                "document_id": document.id,
                "source_id": document.source_id,
                "embedding": hashed_embedding(document.content),
                "preview": document.content[:180],
            }
        )

    def search(self, workspace_id: str, query: str) -> list[dict[str, Any]]:
        query_vec = hashed_embedding(query)
        hits: list[dict[str, Any]] = []
        for node in self.nodes:
            if node["workspace_id"] != workspace_id:
                continue
            score = cosine(query_vec, node["embedding"])
            if score > 0:
                hits.append(
                    {
                        "document_id": node["document_id"],
                        "score": round(score, 4),
                        "preview": node["preview"],
                    }
                )
        hits.sort(key=lambda item: item["score"], reverse=True)
        return hits[:5]
