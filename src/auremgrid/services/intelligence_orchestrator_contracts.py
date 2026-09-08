"""Runbook and expert profile selection helpers."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from auremgrid.domain.errors import AuthorizationError


class IntelligenceOrchestratorContractsMixin:
    def _select_profiles(self, org: str, ws: str, person: str, profile_ids: Sequence[str] | None, runbook: Any) -> list[Any]:
        contracts = self.contracts or getattr(self.os, "intelligence_contracts", None)
        if not contracts:
            return []
        try:
            if runbook is None and not profile_ids:
                return []
            ids = list(profile_ids or self._field(runbook, "profile_ids") or [])
            profiles = self._list_contracts(contracts, "list_profiles", org, ws, person)
            if ids:
                profiles = [p for p in profiles if self._profile_key(p) in {str(x) for x in ids}]
            return list(profiles)[: self.limits.max_specialists]
        except (AuthorizationError, TypeError, AttributeError):
            return []

    def _select_runbook(
        self,
        org: str,
        ws: str,
        person: str,
        runbook_id: str | None,
        profile_ids: Sequence[str] | None,
        situation: Mapping[str, Any],
        query: str | None,
    ) -> Any:
        contracts = self.contracts or getattr(self.os, "intelligence_contracts", None)
        if not contracts:
            return None
        try:
            runbooks = self._list_contracts(contracts, "list_runbooks", org, ws, person, execution_approved=True)
            if runbook_id:
                for runbook in runbooks:
                    if self._contract_key(runbook) == runbook_id:
                        return runbook
                return None
            if profile_ids:
                return next((r for r in runbooks if set(profile_ids).intersection(self._field(r, "profile_ids") or [])), None)
            domains = {str(item).lower() for item in (situation.get("context", {}).get("domains") or [])}
            text = " ".join([
                str(query or ""),
                json.dumps(situation.get("findings", [])[:8], sort_keys=True),
                json.dumps(situation.get("context", {}).get("scenario_inputs", {}), sort_keys=True),
            ]).lower()
            scored: list[tuple[int, str, Any]] = []
            query_terms = {token for token in str(query or "").lower().split() if len(token) >= 3}
            for candidate in runbooks:
                candidate_domains = {str(item).lower() for item in (self._field(candidate, "domains") or [])}
                triggers = [str(item).lower() for item in (self._field(candidate, "activation_sequence") or [])]
                intent = str(self._field(candidate, "intent") or "").lower()
                score = (len(query_terms.intersection(candidate_domains)) * 4) if query_terms else (len(domains.intersection(candidate_domains)) * 4)
                score += sum(3 for trigger in triggers if trigger and (trigger in query_terms or (not query_terms and trigger in text)))
                score += 1 if intent and query_terms and any(token in query_terms for token in intent.split() if len(token) >= 4) else 0
                if score:
                    scored.append((score, self._contract_key(candidate), candidate))
            return max(scored, key=lambda item: (item[0], item[1]))[2] if scored else None
        except (AuthorizationError, TypeError, AttributeError):
            return None

    def _list_contracts(self, contracts: Any, method_name: str, org: str, ws: str, person: str, *, execution_approved: bool | None = None) -> list[Any]:
        method = getattr(contracts, method_name)
        kwargs: dict[str, Any] = {"organization_id": org, "workspace_id": ws, "person_id": person}
        if execution_approved is not None:
            kwargs["execution_approved"] = execution_approved
        # Facades in deployments accept either identity-first or explicit
        # organization/workspace/person scope. Try only those fixed signatures;
        # never pass arbitrary context to a definition provider.
        for args, kwargs in (
            ((), kwargs),
            ((org, person, ws), {}),
            ((org, ws, person), {}),
        ):
            try:
                value = method(*args, **kwargs)
                return list(value or [])
            except TypeError:
                continue
        return []

    @staticmethod
    def _field(value: Any, key: str) -> Any:
        return getattr(value, key, value.get(key) if isinstance(value, Mapping) else None)

    def _contract_key(self, value: Any) -> str:
        return str(self._field(value, "id") or self._field(value, "key") or "")

    def _profile_key(self, value: Any) -> str:
        return self._contract_key(value)

    def _contract_ref(self, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return {"id": self._contract_key(value), "version": self._field(value, "version"), "name": self._field(value, "name")}

