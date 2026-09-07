from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.knowledge_state import validate_knowledge_transition
from auremgrid.domain.models import Citation, Fact

def _now() -> datetime: return datetime.now(timezone.utc)
def _transition_now() -> datetime:
    started=datetime.now(timezone.utc); current=started
    while current <= started: current=datetime.now(timezone.utc)
    return current
def _norm(value: str) -> str: return re.sub(r"[^a-z0-9]+"," ",value.lower()).strip()
_DOMAIN_SUFFIXES=frozenset({"com","net","org","io","co","ca","ai","app","dev","uk","us"})
def _forms(value: str) -> set[str]:
    words=_norm(value).split()
    if not words: return set()
    forms={"".join(words)}
    while words and words[-1] in _DOMAIN_SUFFIXES: words.pop()
    if words: forms.add("".join(words))
    return {item for item in forms if item}
def _variant_score(left: set[str], right: set[str]) -> float:
    score=0.0
    for candidate in left:
        for evidence in right:
            if candidate==evidence: score=max(score,1.0)
            elif min(len(candidate),len(evidence))>=5 and (candidate in evidence or evidence in candidate): score=max(score,0.85*min(len(candidate),len(evidence))/max(len(candidate),len(evidence)))
    return score

class BrainOpsSharedMixin:
    def __init__(self, os: Any) -> None: self.os=os; self.conn=os.store.conn
    def _brain_identity(self, identity: Any, workspace_id: str, capability: str, write: bool) -> tuple[str, str]:
        if not hasattr(identity,"person_id"): raise AuthorizationError("authenticated identity is required")
        if identity.workspace_id not in {None,workspace_id}: raise AuthorizationError("identity is outside requested scope")
        identity.require(capability); self.os._require_person_access(identity.organization_id,workspace_id,identity.person_id,write=write)
        return identity.organization_id,identity.person_id
    def _authorize(self, organization_id: str, workspace_id: str | None, person_id: str, write: bool) -> None:
        if workspace_id: self.os._require_person_access(organization_id,workspace_id,person_id,write=write)
        elif self.os.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("organization membership required")
    def _identity_person(self, organization_id: str, workspace_id: str | None, identity: Any, capability: str) -> str:
        if hasattr(identity,"person_id"):
            if identity.organization_id != organization_id or (workspace_id and identity.workspace_id not in {None,workspace_id}): raise AuthorizationError("identity is outside requested scope")
            identity.require(capability); return identity.person_id
        raise AuthorizationError("authenticated identity is required")
    @staticmethod
    def _id(prefix: str) -> str:
        import uuid
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

__all__ = [
    "BrainOpsSharedMixin", "_now", "_transition_now", "_norm", "_forms", "_variant_score",
    "_DOMAIN_SUFFIXES", "json", "re", "datetime", "timezone", "Any", "AuthorizationError",
    "NotFoundError", "ValidationError", "Citation", "Fact", "validate_knowledge_transition",
]
