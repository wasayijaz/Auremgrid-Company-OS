from __future__ import annotations

from datetime import datetime
from typing import Any

from auremgrid.adapters.cognee import CogneeAdapter
from auremgrid.adapters.graphiti import GraphitiAdapter
from auremgrid.adapters.graphrag import GraphRAGAdapter
from auremgrid.adapters.letta import LettaAdapter
from auremgrid.adapters.lightrag import LightRAGAdapter
from auremgrid.adapters.mem0 import Mem0Adapter
from auremgrid.adapters.onyx import OnyxAdapter
from auremgrid.adapters.ragflow import RAGFlowAdapter
from auremgrid.domain.models import Document, Fact


class OpenSourceStack:
    """All eight engines, used in-process, never as competing sources of truth."""

    def __init__(self) -> None:
        self.graphiti = GraphitiAdapter()
        self.cognee = CogneeAdapter()
        self.mem0 = Mem0Adapter()
        self.onyx = OnyxAdapter()
        self.ragflow = RAGFlowAdapter()
        self.lightrag = LightRAGAdapter()
        self.graphrag = GraphRAGAdapter()
        self.letta = LettaAdapter()

    def ingest_document(self, document: Document, content: str, observed_at: datetime) -> str:
        cleaned = self.ragflow.clean(content)
        self.graphiti.ingest(document.workspace_id, document.source_id, cleaned, observed_at)
        self.lightrag.index(document)
        return cleaned

    def ingest_fact(self, fact: Fact) -> None:
        self.cognee.ingest_fact(fact.workspace_id, fact)
        self.graphrag.ingest_fact(fact)

    def remember(self, workspace_id: str, actor_id: str, content: str, kind: str) -> dict[str, Any]:
        return self.mem0.remember(workspace_id, actor_id, content, kind)

    def bind_agent(self, workspace_id: str, actor_id: str) -> dict[str, Any]:
        profile = self.letta.bind(workspace_id, actor_id, "read-only agency agent")
        self.letta.teach(workspace_id, actor_id, "cite-or-return-unknown")
        self.letta.teach(workspace_id, actor_id, "never-cross-workspace")
        return profile

    def contributions(self, workspace_id: str, query: str, actor_id: str | None = None) -> list[dict[str, Any]]:
        profile = self.letta.profile(workspace_id, actor_id) if actor_id else None
        return [
            {"name": self.graphiti.name, "role": self.graphiti.role, "license": self.graphiti.license, "hits": self.graphiti.search(workspace_id, query)},
            {"name": self.cognee.name, "role": self.cognee.role, "license": self.cognee.license, "hits": self.cognee.search(workspace_id, query)},
            {"name": self.mem0.name, "role": self.mem0.role, "license": self.mem0.license, "hits": self.mem0.recall(workspace_id, query)},
            {"name": self.onyx.name, "role": self.onyx.role, "license": self.onyx.license, "hits": self.onyx.catalog_for(workspace_id)},
            {"name": self.ragflow.name, "role": self.ragflow.role, "license": self.ragflow.license, "hits": [{"status": "cleaned"}]},
            {"name": self.lightrag.name, "role": self.lightrag.role, "license": self.lightrag.license, "hits": self.lightrag.search(workspace_id, query)},
            {"name": self.graphrag.name, "role": self.graphrag.role, "license": self.graphrag.license, "hits": self.graphrag.search(workspace_id, query)},
            {"name": self.letta.name, "role": self.letta.role, "license": self.letta.license, "hits": [profile or {}]},
        ]
