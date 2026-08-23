from __future__ import annotations

"""Bounded, read-only orchestration over the native Intelligence projection.

The orchestrator deliberately owns no canonical write path.  It prepares a
small ACL-scoped situation, selects immutable expert/runbook definitions, and
turns independently produced specialist observations into a validated brief.
Specialists are deterministic by default; callers may inject pure functions
for tests or an application-owned model adapter.  Every injected result is
bounded and provenance checked before it can influence the synthesis.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import json
import uuid
from typing import Any, Callable, Mapping, Sequence

from auremgrid.domain.errors import AuthorizationError, ValidationError


MAX_ITEMS = 64
MAX_SPECIALISTS = 8
MAX_ITERATIONS = 3
MAX_TEXT = 2000


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _text(value: Any, default: str = "") -> str:
    value = str(value or default).strip()
    return value[:MAX_TEXT]


def _bounded_list(value: Any, limit: int = MAX_ITEMS) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_json(item) for item in value[:limit]]


def _score(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, result))


def _ref_id(value: Any) -> str | None:
    """Extract a citation identifier from a bounded evidence descriptor."""
    if not isinstance(value, Mapping):
        return None
    ref = value.get("ref") or value.get("object_ref") or value.get("source")
    if isinstance(ref, Mapping):
        ref = ref.get("id")
    return str(ref) if ref not in (None, "") else None


REQUIRED_RESULT_FIELDS = (
    "finding", "evidence_for", "evidence_against", "assumptions", "unknowns",
    "hypothesis", "confidence", "analogues", "risks", "options",
    "recommendation", "expected_impact", "needs_review",
)


def validate_expert_result(value: Mapping[str, Any], *, allowed_refs: set[str] | None = None) -> dict[str, Any]:
    """Normalize one specialist or final result and fail closed on bad shape."""
    if not isinstance(value, Mapping):
        raise ValidationError("expert result must be an object")
    missing = [key for key in REQUIRED_RESULT_FIELDS if key not in value]
    if missing:
        raise ValidationError("expert result missing required fields: " + ",".join(missing))
    result: dict[str, Any] = {
        "finding": _text(value.get("finding"), "No finding returned."),
        "evidence_for": _bounded_list(value.get("evidence_for")),
        "evidence_against": _bounded_list(value.get("evidence_against")),
        "assumptions": [_text(x) for x in _bounded_list(value.get("assumptions"))],
        "unknowns": [_text(x) for x in _bounded_list(value.get("unknowns"))],
        "hypothesis": _text(value.get("hypothesis"), "No causal hypothesis established."),
        "confidence": round(_score(value.get("confidence")), 3),
        "analogues": _bounded_list(value.get("analogues")),
        "risks": _bounded_list(value.get("risks")),
        "options": _bounded_list(value.get("options")),
        "recommendation": _json(value.get("recommendation")),
        "expected_impact": _json(value.get("expected_impact")),
        "needs_review": bool(value.get("needs_review")),
    }
    if allowed_refs is not None:
        dropped_citations = 0
        for key in ("evidence_for", "evidence_against", "analogues"):
            checked: list[Any] = []
            for item in result[key]:
                ref = _ref_id(item)
                if ref is None or ref not in allowed_refs:
                    dropped_citations += 1
                    continue
                checked.append(item)
            result[key] = checked
        if dropped_citations:
            result["needs_review"] = True
            result["unknowns"] = result["unknowns"][: MAX_ITEMS - 1] + [
                f"{dropped_citations} evidence item(s) lacked an allowed citation and were dropped."
            ]
    return result


@dataclass(frozen=True)
class OrchestrationLimits:
    max_items: int = MAX_ITEMS
    max_specialists: int = MAX_SPECIALISTS
    max_iterations: int = MAX_ITERATIONS
    timeout_seconds: float = 20.0


class IntelligenceOrchestrator:
    """Compose native Intelligence and immutable expert/runbook contracts."""

    def __init__(
        self,
        os: Any,
        contracts: Any | None = None,
        *,
        limits: OrchestrationLimits | None = None,
        specialist_handlers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
    ) -> None:
        self.os = os
        self.contracts = contracts
        self.limits = limits or OrchestrationLimits()
        self.specialist_handlers = dict(specialist_handlers or {})
        self._runs: list[dict[str, Any]] = []

    def get_run(self, trace_id: str, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any] | None:
        self.os._require_person_access(organization_id, workspace_id, person_id)
        for item in reversed(self._runs):
            scope = item.get("scope") or {}
            if item["trace_id"] == trace_id and scope.get("organization_id") == organization_id and scope.get("workspace_id") == workspace_id and scope.get("person_id") == person_id:
                return _json(item)
        return None

    def run(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        *,
        actor_id: str | None = None,
        runbook_id: str | None = None,
        profile_ids: Sequence[str] | None = None,
        query: str | None = None,
        as_of: datetime | None = None,
        capabilities: Sequence[str] | None = None,
        iterations: int = 1,
    ) -> dict[str, Any]:
        self.os._require_person_access(organization_id, workspace_id, person_id)
        trace_id = "inteltrace_" + uuid.uuid4().hex
        trace: list[dict[str, Any]] = []
        started = _now()
        trace.append({"stage": "situation_builder", "status": "started", "at": started})
        situation = self.os.intelligence.workspace(
            organization_id, workspace_id, person_id, actor_id=actor_id,
            as_of=as_of, query=query, capabilities=capabilities,
            use_reasoning_provider=False,
        )
        allowed_refs = self._visible_refs(situation)
        context = self._bounded_context(situation)
        trace.append({"stage": "situation_builder", "status": "completed", "evidence_count": len(allowed_refs)})

        iteration_budget = min(self.limits.max_iterations, max(1, int(iterations)))
        runbook = self._select_runbook(
            organization_id, workspace_id, person_id, runbook_id, profile_ids,
            situation, query,
        )
        profiles = self._select_profiles(organization_id, workspace_id, person_id, profile_ids, runbook)
        route_reason = "matched" if runbook else "no_match"
        trace.append({"stage": "runbook_router", "status": "completed" if runbook else "degraded", "reason": route_reason, "runbook": self._contract_ref(runbook)})

        specialists: list[dict[str, Any]] = []
        errors: list[str] = []
        for iteration in range(iteration_budget):
            batch, batch_errors = self._run_specialists(profiles, context, allowed_refs)
            specialists.extend(batch)
            errors.extend(batch_errors)
            trace.append({"stage": "specialist_fanout", "iteration": iteration + 1, "status": "completed" if batch else "degraded", "count": len(batch), "errors": batch_errors[:8]})
            if iterations <= 1 or not batch:
                # One successful pass is enough; additional passes are only
                # requested explicitly for bounded refinement.
                if iterations <= 1:
                    break
        # A profile may be evaluated in multiple bounded passes, but synthesis
        # consumes one latest result per profile to keep item budgets strict.
        latest: dict[str, dict[str, Any]] = {}
        for item in specialists:
            latest[str((item.get("profile") or {}).get("id"))] = item
        specialists = list(latest.values())[: self.limits.max_specialists]
        if errors:
            # Keep a stable summary marker for consumers that do not inspect
            # every per-iteration trace event.
            trace.append({"stage": "specialist_errors", "status": "degraded", "errors": errors[:8]})

        contradictions = self._contradictions(specialists)
        trace.append({"stage": "contradiction_detector", "status": "completed", "count": len(contradictions)})
        final = self._synthesize(situation, specialists, contradictions, errors, allowed_refs)
        final = self._reality_check(final, situation, allowed_refs)
        trace.extend([
            {"stage": "synthesizer", "status": "completed"},
            {"stage": "reality_checker", "status": "completed" if not final["needs_review"] else "review"},
        ])
        result = {
            **final,
            "trace_id": trace_id,
            "status": "degraded" if errors or not specialists else "ready",
            "contradictions": contradictions,
            "runbook_route": {"status": "matched" if runbook else "no_match", "reason": route_reason},
            "runbook": self._contract_ref(runbook),
            "profiles": [self._contract_ref(p) for p in profiles[: self.limits.max_specialists]],
            "trace": trace,
            "limits": {"max_items": self.limits.max_items, "max_specialists": self.limits.max_specialists, "max_iterations": self.limits.max_iterations},
            "scope": {"organization_id": organization_id, "workspace_id": workspace_id, "person_id": person_id},
            "generated_at": _now(),
        }
        self._runs.append(result)
        return _json(result)

    def _invoke_specialist(self, key: str, profile: Any, context: Mapping[str, Any]) -> Mapping[str, Any]:
        handler = self.specialist_handlers.get(key)
        if handler is not None:
            return handler(context)
        findings = context.get("findings") or []
        first = findings[0] if findings else {}
        evidence = first.get("evidence", [])[:4] if isinstance(first, Mapping) else []
        return {
            "finding": first.get("summary") if isinstance(first, Mapping) else "No visible finding.",
            "evidence_for": evidence,
            "evidence_against": first.get("opposing_evidence", [])[:2] if isinstance(first, Mapping) else [],
            "assumptions": ["Only ACL-visible canonical records were considered."],
            "unknowns": ["Unobserved external causes remain unknown."],
            "hypothesis": (first.get("hypotheses") or [{"text": "No hypothesis established."}])[0].get("text", "No hypothesis established.") if isinstance(first, Mapping) else "No hypothesis established.",
            "confidence": ((first.get("confidence") or {}).get("score", 0.35) if isinstance(first, Mapping) else 0.35),
            "analogues": context.get("historical_analogues", [])[:4],
            "risks": [item.get("summary") for item in (first.get("scenarios", [])[:2] if isinstance(first, Mapping) else [])],
            "options": [first.get("recommendation")] if isinstance(first, Mapping) else [],
            "recommendation": first.get("recommendation", {}) if isinstance(first, Mapping) else {},
            "expected_impact": first.get("impact", {}) if isinstance(first, Mapping) else {},
            "needs_review": not bool(findings),
        }

    def _run_specialists(
        self,
        profiles: Sequence[Any],
        context: Mapping[str, Any],
        allowed_refs: set[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Run each specialist in an isolated future with a hard wall timeout."""
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        for profile in profiles[: self.limits.max_specialists]:
            profile_key = self._profile_key(profile)
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="intel-specialist")
            future = executor.submit(self._invoke_specialist, profile_key, profile, context)
            try:
                raw = future.result(timeout=max(0.01, float(self.limits.timeout_seconds)))
                normalized = validate_expert_result(raw, allowed_refs=allowed_refs)
                normalized["profile"] = self._contract_ref(profile)
                results.append(normalized)
            except FutureTimeoutError:
                future.cancel()
                errors.append(f"{profile_key}:timeout")
            except Exception as exc:
                errors.append(f"{profile_key}:{type(exc).__name__}")
            finally:
                # Do not wait for a runaway handler after its deadline. The
                # handler receives no store and therefore cannot mutate OS state.
                executor.shutdown(wait=False, cancel_futures=True)
        return results, errors

    def _synthesize(self, situation: Mapping[str, Any], specialists: list[dict[str, Any]], contradictions: list[dict[str, Any]], errors: list[str], allowed_refs: set[str]) -> dict[str, Any]:
        if not specialists:
            return validate_expert_result({
                "finding": "No specialist produced a bounded result.", "evidence_for": [], "evidence_against": [],
                "assumptions": [], "unknowns": ["Specialist output unavailable."], "hypothesis": "No hypothesis established.",
                "confidence": 0.0, "analogues": [], "risks": errors, "options": [], "recommendation": {"summary": "Review available evidence manually."},
                "expected_impact": {"level": "unknown"}, "needs_review": True,
            }, allowed_refs=allowed_refs)
        best = max(specialists, key=lambda item: item.get("confidence", 0.0))
        combined = dict(best)
        combined["evidence_for"] = [item for specialist in specialists for item in specialist.get("evidence_for", [])][: self.limits.max_items]
        combined["evidence_against"] = [item for specialist in specialists for item in specialist.get("evidence_against", [])][: self.limits.max_items]
        combined["analogues"] = [item for specialist in specialists for item in specialist.get("analogues", [])][: self.limits.max_items]
        combined["risks"] = [item for specialist in specialists for item in specialist.get("risks", [])][: self.limits.max_items]
        combined["needs_review"] = bool(combined.get("needs_review") or contradictions or errors)
        if contradictions:
            combined["unknowns"] = list(combined.get("unknowns", [])) + ["Independent specialists disagree on the visible signal."]
        return validate_expert_result(combined, allowed_refs=allowed_refs)

    def _reality_check(self, result: dict[str, Any], situation: Mapping[str, Any], allowed_refs: set[str]) -> dict[str, Any]:
        checked = validate_expert_result(result, allowed_refs=allowed_refs)
        if not checked["evidence_for"] and situation.get("status") == "insufficient_evidence":
            checked["needs_review"] = True
        return checked

    @staticmethod
    def _contradictions(specialists: list[dict[str, Any]]) -> list[dict[str, Any]]:
        contradictions = []
        for left_index, left in enumerate(specialists):
            for right in specialists[left_index + 1:]:
                if left.get("hypothesis") and right.get("hypothesis") and left["hypothesis"].strip().lower() != right["hypothesis"].strip().lower() and left.get("confidence", 0) >= 0.55 and right.get("confidence", 0) >= 0.55:
                    contradictions.append({"left": left.get("profile"), "right": right.get("profile"), "reason": "Independent hypotheses differ."})
        return contradictions[:MAX_ITEMS]

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
            runbooks = self._list_contracts(contracts, "list_runbooks", org, ws, person)
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

    def _list_contracts(self, contracts: Any, method_name: str, org: str, ws: str, person: str) -> list[Any]:
        method = getattr(contracts, method_name)
        # Facades in deployments accept either identity-first or explicit
        # organization/workspace/person scope. Try only those fixed signatures;
        # never pass arbitrary context to a definition provider.
        for args, kwargs in (
            ((), {"organization_id": org, "workspace_id": ws, "person_id": person}),
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

    @staticmethod
    def _visible_refs(situation: Mapping[str, Any]) -> set[str]:
        refs: set[str] = set()
        def visit(value: Any) -> None:
            if isinstance(value, Mapping):
                ref = _ref_id(value)
                if ref:
                    refs.add(ref)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(situation)
        return refs

    def _bounded_context(self, situation: Mapping[str, Any]) -> dict[str, Any]:
        context = {
            "scope": situation.get("scope"), "status": situation.get("status"),
            "findings": situation.get("findings", [])[: self.limits.max_items],
            "historical_analogues": situation.get("historical_analogues", [])[: self.limits.max_items],
            "decision_action_outcome_learning": situation.get("decision_action_outcome_learning", [])[: self.limits.max_items],
            "scenario_inputs": situation.get("context", {}).get("scenario_inputs", {}),
        }
        encoded = json.dumps(_json(context), separators=(",", ":"), sort_keys=True)
        if len(encoded) > 256 * 1024:
            context["findings"] = context["findings"][:8]
            context["historical_analogues"] = context["historical_analogues"][:8]
        return context
