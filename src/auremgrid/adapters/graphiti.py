from __future__ import annotations

from datetime import datetime
from typing import Any

from auremgrid.adapters.hybrid import tokens
from auremgrid.extract.deterministic import extract_claims


class GraphitiAdapter:
    """Temporal client-brain engine. Local stand-in for Graphiti by Zep."""

    name = "local_graphiti_style_projection"
    role = "temporal client knowledge graph"
    license = "Apache-2.0"

    def __init__(self) -> None:
        self.episodes: list[dict[str, Any]] = []

    def ingest(self, workspace_id: str, source_id: str, content: str, observed_at: datetime) -> None:
        extraction = extract_claims(content, observed_at)
        self.episodes.append(
            {
                "workspace_id": workspace_id,
                "source_id": source_id,
                "observed_at": observed_at.isoformat(),
                "facts": extraction.facts,
                "relations": extraction.relations,
            }
        )

    def search(self, workspace_id: str, query: str) -> list[dict[str, Any]]:
        query_tokens = set(tokens(query))
        hits: list[dict[str, Any]] = []
        for episode in self.episodes:
            if episode["workspace_id"] != workspace_id:
                continue
            for fact in episode["facts"]:
                haystack = set(tokens(f"{fact.subject} {fact.predicate} {fact.object}"))
                overlap = query_tokens & haystack
                if overlap:
                    hits.append(
                        {
                            "kind": "temporal_fact",
                            "subject": fact.subject,
                            "predicate": fact.predicate,
                            "object": fact.object,
                            "overlap": sorted(overlap),
                            "source_id": episode["source_id"],
                        }
                    )
        return hits
