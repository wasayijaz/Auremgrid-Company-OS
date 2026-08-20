from __future__ import annotations

"""Optional Graphiti/Neo4j projection.

The dependency is deliberately imported only when this adapter is constructed.
No provider result is canonical: the sidecar metadata maps results back to
workspace/source/document identifiers and CompanyOS re-authorizes them.
"""

import asyncio
import hashlib
import inspect
import os
from datetime import datetime
from typing import Any, Iterable, Mapping


def _run(awaitable: Any) -> Any:
    if not inspect.isawaitable(awaitable):
        return awaitable
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    # CompanyOS is synchronous. Running a nested event loop in a worker thread
    # keeps the adapter usable from async test harnesses too.
    import threading
    result: list[Any] = []
    error: list[BaseException] = []

    def execute() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error.append(exc)

    thread = threading.Thread(target=execute)
    thread.start(); thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


def episode_id(workspace_id: str, generation: str, source_id: str, content: str, observed_at: str,
               document_id: str | None = None) -> str:
    value = "\x1f".join((workspace_id, generation, source_id, document_id or "", observed_at, content))
    return "ag-" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class GraphitiConfigurationError(ValueError):
    pass


class NoopCrossEncoder:
    """Explicitly disables provider cross-encoding; Graphiti must not choose one."""

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        return [(passage, 1.0) for passage in passages]


class UpstreamGraphitiProjection:
    """Graphiti Core adapter; construction requires explicit Neo4j and model config."""

    name = "graphiti-upstream-neo4j"
    requires_full_workspace_access = True
    uses_current_time_search = True

    def __init__(self, *, client: Any | None = None, uri: str | None = None,
                 username: str | None = None, password: str | None = None,
                 llm_model: str | None = None, small_model: str | None = None,
                 embedder_model: str | None = None,
                 openai_api_key: str | None = None, llm_base_url: str | None = None,
                 embedder_base_url: str | None = None, embedding_dim: int | None = None,
                 neo4j_database: str | None = None) -> None:
        small_model = small_model or llm_model
        self._configured = all((uri, username, password, llm_model, small_model, embedder_model, openai_api_key,
                                llm_base_url, embedder_base_url, embedding_dim, neo4j_database))
        self._uri, self._username = uri, username
        self._episodes: dict[str, dict[str, Any]] = {}
        self._indices_ready = False
        self._store: Any | None = None
        self._health: dict[str, Any] = {"status": "unavailable", "generation": None, "detail": None}
        if client is not None:
            self._client = client
            self._configured = True
        else:
            if not self._configured:
                raise GraphitiConfigurationError(
                    "Graphiti requires explicit Neo4j URI/username/password/database, LLM and embedder models/base URLs, embedding dimension, and OpenAI API key"
                )
            try:
                from graphiti_core import Graphiti  # type: ignore
                from graphiti_core.driver.neo4j_driver import Neo4jDriver  # type: ignore
                from graphiti_core.llm_client.config import LLMConfig  # type: ignore
                from graphiti_core.llm_client.openai_client import OpenAIClient  # type: ignore
                from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig  # type: ignore
            except ImportError as exc:
                raise GraphitiConfigurationError("install the optional graphiti dependency") from exc
            driver = Neo4jDriver(uri=uri, user=username, password=password, database=neo4j_database)
            llm = OpenAIClient(config=LLMConfig(
                model=llm_model, small_model=small_model,
                api_key=openai_api_key, base_url=llm_base_url,
            ))
            embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(
                embedding_model=embedder_model, api_key=openai_api_key,
                base_url=embedder_base_url, embedding_dim=embedding_dim,
            ))
            self._client = Graphiti(graph_driver=driver, llm_client=llm, embedder=embedder,
                                    cross_encoder=NoopCrossEncoder())
        self._health = {"status": "configured", "generation": None, "detail": None}

    @classmethod
    def from_environment(cls) -> "UpstreamGraphitiProjection | None":
        if os.getenv("AUREMGRID_GRAPHITI_ENABLED", "").lower() not in {"1", "true", "yes", "on"}:
            return None
        raw_dim = os.getenv("AUREMGRID_GRAPHITI_EMBEDDING_DIM")
        try:
            embedding_dim = int(raw_dim) if raw_dim is not None else None
        except ValueError as exc:
            raise GraphitiConfigurationError("AUREMGRID_GRAPHITI_EMBEDDING_DIM must be a positive integer") from exc
        if embedding_dim is not None and embedding_dim <= 0:
            raise GraphitiConfigurationError("AUREMGRID_GRAPHITI_EMBEDDING_DIM must be a positive integer")
        values = {"uri": os.getenv("AUREMGRID_GRAPHITI_NEO4J_URI"),
                  "username": os.getenv("AUREMGRID_GRAPHITI_NEO4J_USERNAME"),
                  "password": os.getenv("AUREMGRID_GRAPHITI_NEO4J_PASSWORD"),
                  "llm_model": os.getenv("AUREMGRID_GRAPHITI_LLM_MODEL"),
                  "small_model": os.getenv("AUREMGRID_GRAPHITI_SMALL_MODEL") or os.getenv("AUREMGRID_GRAPHITI_LLM_MODEL"),
                  "embedder_model": os.getenv("AUREMGRID_GRAPHITI_EMBEDDER_MODEL"),
                  "openai_api_key": os.getenv("AUREMGRID_GRAPHITI_OPENAI_API_KEY"),
                  "llm_base_url": os.getenv("AUREMGRID_GRAPHITI_LLM_BASE_URL"),
                  "embedder_base_url": os.getenv("AUREMGRID_GRAPHITI_EMBEDDER_BASE_URL"),
                  "embedding_dim": embedding_dim,
                  "neo4j_database": os.getenv("AUREMGRID_GRAPHITI_NEO4J_DATABASE")}
        return cls(**values)

    def bind_store(self, store: Any) -> None:
        self._store = store
        self._episodes = {
            str(row["remote_episode_uuid"]): dict(row)
            for row in store.conn.execute(
                "SELECT * FROM graphiti_episode_mappings ORDER BY created_at,episode_key"
            ).fetchall()
        }

    @staticmethod
    def _group(workspace_id: str, generation: str) -> str:
        return "auremgrid-" + hashlib.sha256(f"{workspace_id}\x1f{generation}".encode()).hexdigest()

    def _ensure_ready(self) -> None:
        if not self._indices_ready and hasattr(self._client, "build_indices_and_constraints"):
            _run(self._client.build_indices_and_constraints())
            self._indices_ready = True

    def _find_episode_by_name(self, name: str, group_id: str) -> list[str]:
        """Recover a create whose remote commit preceded the SQLite mapping."""

        finder = getattr(self._client, "find_episode_by_name", None)
        if finder is not None:
            raw = _run(finder(name=name, group_id=group_id))
        else:
            driver = getattr(self._client, "graph_driver", None) or getattr(self._client, "driver", None)
            if driver is None or not hasattr(driver, "execute_query"):
                return []
            raw = _run(driver.execute_query(
                "MATCH (e:Episodic {name: $name, group_id: $group_id}) "
                "RETURN e.uuid AS uuid LIMIT 2",
                name=name, group_id=group_id, routing_="r",
            ))
        return _episode_uuids(raw)

    def upsert_episode(self, workspace_id: str, source_id: str, content: str,
                       observed_at: str, generation: str | None = None,
                       document_id: str | None = None, recorded_at: str | None = None) -> None:
        self._ensure_ready()
        generation = generation or "live"
        if self._store is None:
            raise GraphitiConfigurationError("Graphiti projection requires a bound durable store")
        if not document_id or not recorded_at:
            raise GraphitiConfigurationError("Graphiti episodes require canonical document metadata")
        key = episode_id(workspace_id, generation, source_id, content, observed_at, document_id)
        existing = self._store.get_graphiti_episode_mapping(key)
        if existing is not None:
            self._episodes[str(existing["remote_episode_uuid"])] = existing
            return
        group_id = self._group(workspace_id, generation)
        recovered = self._find_episode_by_name(key, group_id)
        if len(recovered) > 1:
            raise RuntimeError("Graphiti contains multiple episodes for one Auremgrid episode key")
        if recovered:
            mapping = self._store.record_graphiti_episode_mapping(
                episode_key=key, remote_episode_uuid=recovered[0], workspace_id=workspace_id,
                generation=generation, source_id=source_id, document_id=document_id,
                observed_at=observed_at, recorded_at=recorded_at,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
            self._episodes[recovered[0]] = mapping
            return
        kwargs: dict[str, Any] = dict(name=key, episode_body=content,
            reference_time=datetime.fromisoformat(observed_at), group_id=group_id,
            source_description="auremgrid canonical evidence", update_communities=False)
        try:
            from graphiti_core.nodes import EpisodeType  # type: ignore
            kwargs["source"] = EpisodeType.text
        except ImportError:
            kwargs["source"] = "text"
        result = _run(self._client.add_episode(**kwargs))
        remote_uuid = _episode_uuid(result)
        if not remote_uuid:
            raise RuntimeError("Graphiti add_episode returned no episode UUID")
        mapping = self._store.record_graphiti_episode_mapping(
            episode_key=key, remote_episode_uuid=remote_uuid, workspace_id=workspace_id,
            generation=generation, source_id=source_id, document_id=document_id,
            observed_at=observed_at, recorded_at=recorded_at,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        self._episodes[remote_uuid] = mapping

    def rebuild_workspace(self, generation: str, episodes: Iterable[dict[str, Any]]) -> None:
        for item in episodes:
            self.upsert_episode(item["workspace_id"], item["source_id"], item["content"], item["observed_at"], generation,
                                item.get("document_id"), item.get("recorded_at"))
        self._health = {"status": "building", "generation": generation, "detail": None}

    def restore_generation(self, workspace_id: str, generation: str, episodes: Iterable[dict[str, Any]]) -> None:
        """Restore provider UUID identity from the durable sidecar, without remote writes."""
        if self._store is None:
            raise GraphitiConfigurationError("Graphiti projection requires a bound durable store")
        self._episodes.update({
            str(row["remote_episode_uuid"]): row
            for row in self._store.list_graphiti_episode_mappings(workspace_id, generation)
        })

    def generation_is_complete(
        self, workspace_id: str, generation: str, episodes: Iterable[dict[str, Any]]
    ) -> bool:
        """Prove the durable sidecar covers the canonical generation exactly."""
        if self._store is None:
            return False
        expected: dict[str, dict[str, str]] = {}
        for item in episodes:
            key = episode_id(
                workspace_id, generation, item["source_id"], item["content"],
                item["observed_at"], item.get("document_id"),
            )
            expected[key] = {
                "workspace_id": workspace_id,
                "generation": generation,
                "source_id": item["source_id"],
                "document_id": item.get("document_id") or "",
                "observed_at": item["observed_at"],
                "recorded_at": item.get("recorded_at") or "",
                "content_hash": hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
            }
        rows = self._store.list_graphiti_episode_mappings(workspace_id, generation)
        if {str(row["episode_key"]) for row in rows} != set(expected):
            return False
        return all(
            bool(row["remote_episode_uuid"])
            and all(str(row[field]) == value for field, value in expected[str(row["episode_key"])].items())
            for row in rows
        )

    def activate_generation(self, workspace_id: str, generation: str) -> None:
        self._health = {"status": "healthy", "generation": generation, "detail": None}

    def mark_degraded(self, generation: str | None, detail: str) -> None:
        self._health = {"status": "degraded", "generation": generation, "detail": detail}

    def search(self, workspace_id: str, query: str, allowed_source_ids: Iterable[str],
               as_of: datetime | None = None, limit: int = 8,
               generation: str | None = None) -> list[dict[str, Any]]:
        allowed = set(allowed_source_ids)
        if as_of is not None:
            return []
        if not allowed or generation is None:
            return []
        result = _run(self._client.search(query=query, group_ids=[self._group(workspace_id, generation)], num_results=limit))
        rows = result if isinstance(result, list) else getattr(result, "facts", None) or getattr(result, "edges", None) or []
        hits: list[dict[str, Any]] = []
        for row in rows:
            data = row if isinstance(row, Mapping) else getattr(row, "__dict__", {})
            metadata = data.get("metadata", {}) if isinstance(data, Mapping) else {}
            # Graphiti edges expose source episodes as `episodes`; do not trust
            # arbitrary edge names or provider-generated metadata as identity.
            episodes = data.get("episodes") or getattr(row, "episodes", None) or []
            candidates = episodes if isinstance(episodes, (list, tuple, set)) else [episodes]
            eid = None
            for episode in candidates:
                candidate = episode if isinstance(episode, str) else getattr(episode, "uuid", None) or (episode.get("uuid") if isinstance(episode, Mapping) else None)
                if candidate in self._episodes:
                    eid = candidate; break
            if eid is None:
                candidate = data.get("uuid") or metadata.get("uuid")
                eid = candidate if candidate in self._episodes else None
            item = self._episodes.get(str(eid))
            if not item or item["workspace_id"] != workspace_id or item["generation"] != generation or item["source_id"] not in allowed:
                continue
            if as_of and datetime.fromisoformat(item["observed_at"]) > as_of:
                continue
            hit = {"source_id": item["source_id"], "episode_id": str(eid), "observed_at": item["observed_at"]}
            if item.get("document_id"):
                hit["document_id"] = item["document_id"]
            hits.append(hit)
        return hits[:limit]

    def health(self) -> dict[str, Any]:
        return dict(self._health)

    def close(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None and hasattr(client, "close"):
            _run(client.close())


GraphitiNeo4jAdapter = UpstreamGraphitiProjection


class UnavailableGraphitiProjection(UpstreamGraphitiProjection):
    """Configured-but-unavailable provider; canonical operations remain local."""

    def __init__(self, detail: str) -> None:
        self._client = None
        self._episodes = {}
        self._store = None
        self._health = {"status": "unavailable", "generation": None, "detail": detail}

    def upsert_episode(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("graphiti provider unavailable")

    def rebuild_workspace(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("graphiti provider unavailable")

    def search(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("graphiti provider unavailable")

    def restore_generation(self, *args: Any, **kwargs: Any) -> None:
        return None

    def activate_generation(self, *args: Any, **kwargs: Any) -> None:
        return None


def graph_projection_from_environment() -> UpstreamGraphitiProjection | UnavailableGraphitiProjection | None:
    if os.getenv("AUREMGRID_GRAPHITI_ENABLED", "").lower() not in {"1", "true", "yes", "on"}:
        return None
    try:
        return UpstreamGraphitiProjection.from_environment()
    except (GraphitiConfigurationError, ImportError) as exc:
        return UnavailableGraphitiProjection(str(exc))


def _episode_uuid(value: Any) -> str | None:
    """Extract the created episode UUID across Graphiti 0.29 result wrappers."""

    seen: set[int] = set()

    def visit(item: Any, depth: int = 0) -> str | None:
        if item is None or depth > 4:
            return None
        marker = id(item)
        if marker in seen:
            return None
        seen.add(marker)
        if isinstance(item, Mapping):
            direct = item.get("uuid")
            if isinstance(direct, str) and direct:
                return direct
            for key in ("episode", "episodes", "node", "nodes", "result", "records"):
                found = visit(item.get(key), depth + 1)
                if found:
                    return found
            return None
        if isinstance(item, (list, tuple)):
            for child in item:
                found = visit(child, depth + 1)
                if found:
                    return found
            return None
        direct = getattr(item, "uuid", None)
        if isinstance(direct, str) and direct:
            return direct
        for key in ("episode", "episodes", "node", "nodes", "result", "records"):
            found = visit(getattr(item, key, None), depth + 1)
            if found:
                return found
        return None

    return visit(value)


def _episode_uuids(value: Any) -> list[str]:
    """Normalize exact-name lookup results without trusting generated metadata."""

    if value is None:
        return []
    rows = value
    if isinstance(value, tuple) and value:
        # Neo4j's async driver returns ``(records, summary, keys)``.
        rows = value[0]
    elif hasattr(value, "records"):
        # Newer driver wrappers may expose the same records as an eager result.
        rows = value.records
    if not isinstance(rows, (list, tuple)):
        rows = [rows]
    found: list[str] = []
    for row in rows:
        candidate = _episode_uuid(row)
        if candidate and candidate not in found:
            found.append(candidate)
    return found
