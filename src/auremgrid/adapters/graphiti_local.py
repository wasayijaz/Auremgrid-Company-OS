from __future__ import annotations

from datetime import datetime
from typing import Any

from auremgrid.domain.models import Fact, Relation
from auremgrid.extract.deterministic import extract_claims
from auremgrid.adapters.hybrid import STOPWORDS, tokens


class LocalTemporalGraph:
    """Graphiti-shaped temporal graph that stays inside Auremgrid.

    This is the local baseline. A networked Graphiti adapter can replace it later
    if it beats this implementation on temporal accuracy, citations, and ACL safety.
    """

    name = "graphiti-local"

    def __init__(self) -> None:
        self.episodes: list[dict[str, Any]] = []

    def upsert_episode(self, workspace_id: str, source_id: str, content: str, observed_at: str) -> None:
        observed = datetime.fromisoformat(observed_at)
        extraction = extract_claims(content, observed)
        self.episodes.append(
            {
                "workspace_id": workspace_id,
                "source_id": source_id,
                "content": content,
                "observed_at": observed_at,
                "facts": extraction.facts,
                "relations": extraction.relations,
            }
        )

    def search(self, workspace_id: str, query: str, as_of: str | None = None) -> list[dict[str, Any]]:
        needle = query.lower()
        hits: list[dict[str, Any]] = []
        for episode in self.episodes:
            if episode["workspace_id"] != workspace_id:
                continue
            if as_of and episode["observed_at"] > as_of:
                continue
            if needle and needle not in episode["content"].lower():
                continue
            hits.append(
                {
                    "source_id": episode["source_id"],
                    "observed_at": episode["observed_at"],
                    "fact_count": len(episode["facts"]),
                    "relation_count": len(episode["relations"]),
                }
            )
        return hits

    def neighbors(
        self,
        workspace_id: str,
        entity: str,
        relations: list[Relation],
        as_of: datetime | None = None,
    ) -> list[Relation]:
        target = entity.lower()
        found: list[Relation] = []
        for relation in relations:
            if relation.workspace_id != workspace_id:
                continue
            if as_of and (relation.valid_from > as_of or (relation.valid_until and relation.valid_until <= as_of)):
                continue
            if target in {relation.from_entity.lower(), relation.to_entity.lower()}:
                found.append(relation)
        return found

    def related_fact_boost(self, fact: Fact, query: str) -> float:
        haystack = f"{fact.subject} {fact.predicate} {fact.object}".lower()
        score = 0.0
        haystack_tokens = set(tokens(haystack))
        for token in tokens(query):
            if token in STOPWORDS:
                continue
            if token in haystack_tokens:
                score += 0.15
        return score
