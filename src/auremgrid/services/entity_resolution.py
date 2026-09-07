"""Conservative, organization-scoped entity resolution helpers."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain_ops import _forms, _norm, _variant_score


class EntityResolutionService:
    """Resolve aliases and create human-gated merge proposals."""

    MERGE_THRESHOLD = 0.90

    def __init__(self, company_os: Any) -> None:
        self.os = company_os
        self.conn = company_os.store.conn

    def group_aliases(self, os_scope: Any, names: list[str], *, workspace_id: str | None = None) -> list[dict[str, Any]]:
        organization_id, scope_workspace, person_id = self._scope(os_scope, workspace_id)
        self._authorize(organization_id, scope_workspace, person_id)
        if not names:
            return []
        rows = self._entities(organization_id, scope_workspace)
        groups: dict[str, dict[str, Any]] = {}
        for name in names:
            forms = _forms(name)
            best: tuple[float, Any] | None = None
            for row in rows:
                candidates = {str(row["canonical_name"])}
                aliases = self.conn.execute(
                    "SELECT alias FROM entity_aliases WHERE entity_id=? AND status IN ('approved','proposed') AND retired_at IS NULL ORDER BY created_at,id",
                    (row["id"],),
                ).fetchall()
                candidates.update(str(alias["alias"]) for alias in aliases)
                score = max((_variant_score(forms, _forms(candidate)) for candidate in candidates), default=0.0)
                if best is None or score > best[0]:
                    best = (score, row)
            if best is None or best[0] < self.MERGE_THRESHOLD:
                continue
            row = best[1]
            group = groups.setdefault(str(row["id"]), {
                "entity_id": row["id"], "canonical_name": row["canonical_name"],
                "matched_names": [], "confidence": best[0],
            })
            group["matched_names"].append(name)
            group["confidence"] = max(group["confidence"], best[0])
        return sorted(groups.values(), key=lambda item: str(item["entity_id"]))

    def propose_merge(
        self, os_scope: Any, source_entity_id: str, target_entity_id: str,
        *, evidence: str, confidence: float, evidence_refs: Mapping[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        organization_id, workspace_id, person_id = self._scope(os_scope, None)
        self._authorize(organization_id, workspace_id, person_id, write=True)
        if source_entity_id == target_entity_id or confidence < self.MERGE_THRESHOLD or not evidence.strip():
            raise ValidationError("merge requires distinct, high-confidence entities and evidence")
        refs = {str(key): sorted({str(value) for value in values}) for key, values in (evidence_refs or {}).items()}
        if not any(refs.values()):
            raise ValidationError("merge evidence references are required")
        rows = self.conn.execute(
            "SELECT id,status FROM entities WHERE organization_id=? AND workspace_id=? AND id IN (?,?) ORDER BY id",
            (organization_id, workspace_id, source_entity_id, target_entity_id),
        ).fetchall()
        if len(rows) != 2:
            raise AuthorizationError("entity candidate is outside organization scope")
        if any(row["status"] != "active" for row in rows):
            raise NotFoundError("entity candidate not found")
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "id": self.os._new_id("resolution") if hasattr(self.os, "_new_id") else f"resolution_{int(datetime.now().timestamp() * 1000000)}",
            "organization_id": organization_id, "workspace_id": workspace_id,
            "kind": "merge", "alias": None, "source_entity_id": source_entity_id,
            "target_entity_id": target_entity_id, "candidate_entity_ids": json.dumps([source_entity_id, target_entity_id]),
            "score": float(confidence), "rationale": evidence, "status": "pending",
            "proposed_by_person_id": person_id, "reviewed_by_person_id": None,
            "evidence_source_id": None, "evidence": evidence, "evidence_refs": json.dumps(refs, sort_keys=True),
            "created_at": now, "reviewed_at": None,
        }
        self.conn.execute(
            """INSERT INTO entity_resolution_proposals(
                id,organization_id,workspace_id,kind,alias,source_entity_id,target_entity_id,candidate_entity_ids,
                score,rationale,status,proposed_by_person_id,reviewed_by_person_id,evidence_source_id,evidence,
                evidence_refs,created_at,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(item.values()),
        )
        self.conn.commit()
        return item

    def merge_history(self, os_scope: Any, *, workspace_id: str | None = None) -> list[dict[str, Any]]:
        organization_id, scope_workspace, person_id = self._scope(os_scope, workspace_id)
        self._authorize(organization_id, scope_workspace, person_id)
        query = "SELECT * FROM entity_merge_history WHERE organization_id=?"
        params: list[Any] = [organization_id]
        if scope_workspace:
            query += " AND source_entity_id IN (SELECT id FROM entities WHERE workspace_id=?)"
            params.append(scope_workspace)
        return [dict(row) for row in self.conn.execute(query + " ORDER BY merged_at,id", params).fetchall()]

    def _entities(self, organization_id: str, workspace_id: str | None) -> list[Any]:
        query = "SELECT id,canonical_name FROM entities WHERE organization_id=? AND status='active' AND (workspace_id IS NULL OR workspace_id=?)"
        return self.conn.execute(query + " ORDER BY id", (organization_id, workspace_id)).fetchall()

    def _authorize(self, organization_id: str, workspace_id: str | None, person_id: str | None, write: bool = False) -> None:
        if person_id and workspace_id and hasattr(self.os, "_require_person_access"):
            self.os._require_person_access(organization_id, workspace_id, person_id, write=write)
        elif person_id and self.os.company.org_membership(organization_id, person_id) is None:
            raise AuthorizationError("organization membership required")

    @staticmethod
    def _scope(os_scope: Any, workspace_id: str | None) -> tuple[str, str | None, str | None]:
        def value(key: str, default: Any = None) -> Any:
            return os_scope.get(key, default) if isinstance(os_scope, Mapping) else getattr(os_scope, key, default)
        organization_id = str(value("organization_id", "") or "").strip()
        scoped_workspace = str(value("workspace_id", workspace_id) or workspace_id or "").strip() or None
        person_id = str(value("person_id", "") or "").strip() or None
        if not organization_id:
            raise ValidationError("organization is required")
        if workspace_id and scoped_workspace != workspace_id:
            raise AuthorizationError("entity scope is outside workspace scope")
        return organization_id, scoped_workspace, person_id


EntityResolution = EntityResolutionService
