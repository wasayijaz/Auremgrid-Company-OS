"""Read-only loading and querying for the neutral workflow catalog."""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Iterable

from auremgrid.domain.errors import NotFoundError
from auremgrid.domain.workflows import WorkflowTemplate, validate_catalog


DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[3] / "fixtures" / "workflows" / "catalog.json"


class WorkflowCatalog:
    """Immutable catalog facade; no mutation or persistence methods are exposed."""

    __slots__ = ("_templates", "_by_id", "_sealed")

    def __init__(self, templates: Iterable[WorkflowTemplate]) -> None:
        object.__setattr__(self, "_templates", validate_catalog(templates))
        object.__setattr__(self, "_by_id", MappingProxyType({template.id: template for template in self._templates}))
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("workflow catalog is immutable")
        object.__setattr__(self, name, value)

    @property
    def templates(self) -> tuple[WorkflowTemplate, ...]:
        return self._templates

    def all(self) -> tuple[WorkflowTemplate, ...]:
        return self._templates

    def __iter__(self):
        return iter(self._templates)

    def get(self, template_id: str) -> WorkflowTemplate:
        try:
            return self._by_id[template_id]
        except KeyError as exc:
            raise NotFoundError(f"workflow template not found: {template_id}") from exc

    def for_wing(self, wing: str) -> tuple[WorkflowTemplate, ...]:
        return tuple(template for template in self._templates if wing in template.wings)

    def to_dict(self) -> dict[str, object]:
        return {"templates": [template.to_dict() for template in self._templates]}

    @classmethod
    def load(cls, path: str | Path | None = None) -> "WorkflowCatalog":
        return load_workflow_catalog(path)


def load_workflow_catalog(path: str | Path | None = None) -> WorkflowCatalog:
    """Load and validate a catalog fixture without writing or caching it."""

    catalog_path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    with catalog_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_templates = payload.get("templates") if isinstance(payload, dict) else payload
    if not isinstance(raw_templates, list):
        raise ValueError("workflow catalog must contain a templates list")
    if any(not isinstance(raw, dict) for raw in raw_templates):
        raise ValueError("workflow catalog templates must be objects")
    templates = tuple(WorkflowTemplate.from_mapping(raw) for raw in raw_templates)
    return WorkflowCatalog(templates)
