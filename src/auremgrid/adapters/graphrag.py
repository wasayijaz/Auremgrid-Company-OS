from __future__ import annotations

from collections import defaultdict
from typing import Any

from auremgrid.adapters.hybrid import tokens
from auremgrid.domain.models import Fact


class GraphRAGAdapter:
    """Community-level questions over a whole workspace corpus."""

    name = "graphrag"
    role = "corpus community summaries"
    license = "MIT"

    def __init__(self) -> None:
        self.communities: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    def ingest_fact(self, fact: Fact) -> None:
        bucket = fact.predicate.lower()
        self.communities[fact.workspace_id][bucket].add(f"{fact.subject} {fact.predicate} {fact.object}")

    def search(self, workspace_id: str, query: str) -> list[dict[str, Any]]:
        query_tokens = set(tokens(query))
        hits: list[dict[str, Any]] = []
        for community, members in self.communities.get(workspace_id, {}).items():
            joined = " ".join(members)
            if query_tokens & set(tokens(community)) or query_tokens & set(tokens(joined)):
                hits.append(
                    {
                        "community": community,
                        "size": len(members),
                        "examples": sorted(members)[:3],
                    }
                )
        return hits
