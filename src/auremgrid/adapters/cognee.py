from __future__ import annotations

from collections import defaultdict
from typing import Any

from auremgrid.adapters.hybrid import tokens
from auremgrid.domain.models import Fact


class CogneeAdapter:
    """Memory control plane: current beliefs per workspace."""

    name = "local_cognee_style_projection"
    role = "memory control plane"
    license = "Apache-2.0"

    def __init__(self) -> None:
        self.beliefs: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)

    def ingest_fact(self, workspace_id: str, fact: Fact) -> None:
        if fact.superseded_by:
            return
        self.beliefs[workspace_id][(fact.subject.lower(), fact.predicate.lower())] = fact.object

    def search(self, workspace_id: str, query: str) -> list[dict[str, Any]]:
        query_tokens = set(tokens(query))
        hits: list[dict[str, Any]] = []
        for (subject, predicate), value in self.beliefs.get(workspace_id, {}).items():
            haystack = set(tokens(f"{subject} {predicate} {value}"))
            if query_tokens & haystack:
                hits.append({"subject": subject, "predicate": predicate, "object": value})
        return hits
