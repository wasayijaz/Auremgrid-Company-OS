from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

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
        self._generations: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._active_generation: dict[str, str] = {}
        self._health: dict[str, Any] = {"status": "healthy", "generation": None, "detail": None}

    def upsert_episode(self, workspace_id: str, source_id: str, content: str, observed_at: str, generation: str | None = None) -> None:
        observed = datetime.fromisoformat(observed_at)
        extraction = extract_claims(content, observed)
        episode = {
            "workspace_id": workspace_id, "source_id": source_id, "content": content,
            "observed_at": observed_at, "facts": extraction.facts, "relations": extraction.relations,
        }
        target_generation = generation or self._active_generation.get(workspace_id, "live")
        self._generations.setdefault((workspace_id, target_generation), []).append(episode)
        if generation is None:
            self._active_generation.setdefault(workspace_id, target_generation)
        self.episodes = [item for (ws, gen), values in self._generations.items() if self._active_generation.get(ws) == gen for item in values]

    def rebuild_workspace(self, generation: str, episodes: Iterable[dict[str, Any]]) -> None:
        staged: list[dict[str, Any]] = []
        for item in episodes:
            self.upsert_episode(item["workspace_id"], item["source_id"], item["content"], item["observed_at"], generation)
            staged.append(self._generations[(item["workspace_id"], generation)][-1])
        self._health = {"status": "building", "generation": generation, "detail": None}

    def activate_generation(self, workspace_id: str, generation: str) -> None:
        if (workspace_id, generation) not in self._generations:
            raise ValueError("graph generation is not staged")
        self._active_generation[workspace_id] = generation
        self.episodes = [item for (ws, gen), values in self._generations.items() if self._active_generation.get(ws) == gen for item in values]
        self._health = {"status": "healthy", "generation": generation, "detail": None}

    def health(self) -> dict[str, Any]:
        return dict(self._health)

    def search(self, workspace_id: str, query: str, allowed_source_ids: Iterable[str] = (), as_of: datetime | None = None, limit: int = 8, generation: str | None = None) -> list[dict[str, Any]]:
        allowed = set(allowed_source_ids)
        if not allowed:
            return []
        needle = query.lower()
        hits: list[dict[str, Any]] = []
        active_generation = generation or self._active_generation.get(workspace_id)
        episodes = self._generations.get((workspace_id, active_generation), []) if active_generation else []
        for episode in episodes:
            if episode["workspace_id"] != workspace_id:
                continue
            if allowed and episode["source_id"] not in allowed:
                continue
            if as_of and datetime.fromisoformat(episode["observed_at"]) > as_of:
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
        return hits[:limit]

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
