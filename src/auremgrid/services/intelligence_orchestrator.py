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
import time
import uuid
from typing import Any, Callable, Mapping, Sequence

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.adapters.reasoning import invoke_reasoning_provider


MAX_ITEMS = 64
MAX_SPECIALISTS = 13
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
    # JSON permits neither NaN nor Infinity in the persisted contract.
    if result != result or result in (float("inf"), float("-inf")):
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
    # ``historical_analogues`` was the contract spelling before the runtime
    # specialist field was shortened to ``analogues``. Normalize either wire
    # spelling before strict validation.
    normalized_value = dict(value)
    if "analogues" not in normalized_value and "historical_analogues" in normalized_value:
        normalized_value["analogues"] = normalized_value["historical_analogues"]
    missing = [key for key in REQUIRED_RESULT_FIELDS if key not in normalized_value]
    if missing:
        raise ValidationError("expert result missing required fields: " + ",".join(missing))
    result: dict[str, Any] = {
        "status": _text(value.get("status"), "available"),
        "scope": _json(value.get("scope") or {}),
        "finding": _text(value.get("finding"), "No finding returned."),
        "evidence_for": _bounded_list(value.get("evidence_for")),
        "evidence_against": _bounded_list(value.get("evidence_against")),
        "assumptions": [_text(x) for x in _bounded_list(value.get("assumptions"))],
        "unknowns": [_text(x) for x in _bounded_list(value.get("unknowns"))],
        "hypothesis": _text(value.get("hypothesis"), "No causal hypothesis established."),
        "confidence": round(_score(value.get("confidence")), 3),
        "analogues": _bounded_list(normalized_value.get("analogues")),
        "risks": _bounded_list(value.get("risks")),
        "options": _bounded_list(value.get("options")),
        "recommendation": _json(value.get("recommendation")),
        "expected_impact": _json(value.get("expected_impact")),
        "needs_review": bool(value.get("needs_review")),
        "dissent": _bounded_list(value.get("dissent")),
    }
    result["historical_analogues"] = list(result["analogues"])
    if isinstance(value.get("context_budget"), Mapping):
        result["context_budget"] = _json(value.get("context_budget"))
    if allowed_refs is not None:
        dropped_citations = 0
        for key in ("evidence_for", "evidence_against", "analogues", "dissent"):
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
        specialist_provider: Any | Mapping[str, Any] | None = None,
    ) -> None:
        self.os = os
        self.contracts = contracts
        self.limits = limits or OrchestrationLimits()
        self.specialist_handlers = dict(specialist_handlers or {})
        self.specialist_provider = specialist_provider

    def get_run(self, trace_id: str, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any] | None:
        self.os._require_person_access(organization_id, workspace_id, person_id)
        row = self.os.store.conn.execute(
            "SELECT result_json FROM intelligence_orchestrator_runs WHERE trace_id=? AND organization_id=? AND workspace_id=? AND person_id=?",
            (trace_id, organization_id, workspace_id, person_id),
        ).fetchone()
        if row is None:
            return None
        try:
            return _json(json.loads(row[0]))
        except (TypeError, ValueError):
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
        # Carry the durable correlation id through every specialist context.
        context["trace_id"] = trace_id
        trace.append({"stage": "situation_builder", "status": "completed", "evidence_count": len(allowed_refs)})

        runbook = self._select_runbook(
            organization_id, workspace_id, person_id, runbook_id, profile_ids,
            situation, query,
        )
        profiles = self._select_profiles(organization_id, workspace_id, person_id, profile_ids, runbook)
        requested_iterations = max(1, int(iterations))
        runbook_iterations = self._bounded_iterations(self._field(runbook, "max_iterations"))
        iteration_budget = min(self.limits.max_iterations, requested_iterations, runbook_iterations)
        route_reason = "matched" if runbook else "no_match"
        trace.append({"stage": "runbook_router", "status": "completed" if runbook else "degraded", "reason": route_reason, "runbook": self._contract_ref(runbook)})

        specialists: list[dict[str, Any]] = []
        errors: list[str] = []
        for iteration in range(iteration_budget):
            active_profiles = [
                profile for profile in profiles
                if iteration < self._bounded_iterations(self._field(profile, "max_iterations"))
            ]
            if not active_profiles:
                break
            batch, batch_errors = self._run_specialists(active_profiles, context, allowed_refs)
            specialists.extend(batch)
            errors.extend(batch_errors)
            trace.append({"stage": "specialist_fanout", "iteration": iteration + 1, "status": "completed" if batch else "degraded", "count": len(batch), "errors": batch_errors[:8]})
            if requested_iterations <= 1 or not batch:
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
        context_overflows = [
            item for item in specialists
            if isinstance(item.get("context_budget"), Mapping)
            and item["context_budget"].get("status") == "overflow"
        ]
        specialist_degradations = [
            item for item in specialists
            if item.get("status") == "degraded"
        ]
        if context_overflows:
            trace.append({
                "stage": "context_budget",
                "status": "overflow",
                "count": len(context_overflows),
                "specialists": [item.get("specialist_id") for item in context_overflows[:8]],
            })
        if errors:
            # Keep a stable summary marker for consumers that do not inspect
            # every per-iteration trace event.
            trace.append({"stage": "specialist_errors", "status": "degraded", "errors": errors[:8]})
        if specialist_degradations:
            trace.append({
                "stage": "specialist_degradation",
                "status": "degraded",
                "count": len(specialist_degradations),
                "specialists": [item.get("specialist_id") for item in specialist_degradations[:8]],
            })

        contradictions = self._contradictions(specialists)
        trace.append({"stage": "contradiction_detector", "status": "completed", "count": len(contradictions)})
        final = self._synthesize(situation, specialists, contradictions, errors, allowed_refs)
        final = self._reality_check(final, situation, allowed_refs)
        # Keep disagreement and historical/scenario learning explicit at the
        # orchestration boundary.  These are derived read models: they make
        # the specialist debate inspectable without promoting a weighted view
        # into canonical truth or executing a recommendation.
        disagreement = self._disagreement_summary(specialists, contradictions)
        historical_learning = self._historical_learning(context.get("historical_analogues", []))
        scenario_analysis = self._scenario_analysis(context.get("scenario_inputs", {}))
        if disagreement["status"] == "contested":
            final["needs_review"] = True
        gate_events, gate_review = self._runbook_gates(runbook, situation, specialists, contradictions, errors)
        trace.extend(gate_events)
        if gate_review:
            final["needs_review"] = True
        trace.extend([
            {"stage": "synthesizer", "status": "completed"},
            {"stage": "reality_checker", "status": "completed" if not final["needs_review"] else "review"},
        ])
        result = {
            **final,
            "trace_id": trace_id,
            "status": "degraded" if errors or not specialists or context_overflows or specialist_degradations else "ready",
            "context_budget": {
                "status": "overflow" if context_overflows else "within_budget",
                "overflow_count": len(context_overflows),
                "specialists": [item.get("specialist_id") for item in context_overflows[:8]],
            },
            "contradictions": contradictions,
            "disagreement": disagreement,
            "historical_learning": historical_learning,
            "scenario_analysis": scenario_analysis,
            "specialists": specialists,
            "dissent": [item for specialist in specialists for item in specialist.get("dissent", [])][: self.limits.max_items],
            "runbook_route": {"status": "matched" if runbook else "no_match", "reason": route_reason},
            "runbook": self._contract_ref(runbook),
            "profiles": [self._contract_ref(p) for p in profiles[: self.limits.max_specialists]],
            "trace": trace,
            "limits": {"max_items": self.limits.max_items, "max_specialists": self.limits.max_specialists, "max_iterations": iteration_budget, "runbook_max_iterations": runbook_iterations},
            "scope": {"organization_id": organization_id, "workspace_id": workspace_id, "person_id": person_id},
            "generated_at": _now(),
        }
        # Every emitted stage event is correlated to the persisted run.
        for event in trace:
            event.setdefault("trace_id", trace_id)
        self._persist_run(result)
        return _json(result)

    def _persist_run(self, result: Mapping[str, Any]) -> None:
        scope = result.get("scope") or {}
        now = str(result.get("generated_at") or _now())
        payload = json.dumps(_json(result), separators=(",", ":"), sort_keys=True)
        with self.os.store.atomic(immediate=True):
            self.os.store.conn.execute(
                "INSERT INTO intelligence_orchestrator_runs(trace_id,organization_id,workspace_id,person_id,status,result_json,generated_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    result.get("trace_id"), scope.get("organization_id"), scope.get("workspace_id"),
                    scope.get("person_id"), result.get("status"), payload, now, now,
                ),
            )

    @staticmethod
    def _profile_payload(profile: Any) -> dict[str, Any]:
        keys = ("id", "version", "name", "specialty", "mission", "reasoning_method", "max_context", "max_iterations", "domains", "allowed_domains", "allowed_tools", "tools")
        return {key: IntelligenceOrchestrator._field(profile, key) for key in keys if IntelligenceOrchestrator._field(profile, key) is not None}

    def _invoke_specialist(self, key: str, profile: Any, context: Mapping[str, Any]) -> Mapping[str, Any]:
        profile_context = dict(context)
        profile_context["profile"] = self._profile_payload(profile)
        profile_context = self._restrict_profile_context(profile, profile_context)
        evidence_anchor = self._first_cited_finding_anchor(profile_context.get("findings", []))
        max_context = self._field(profile, "max_context")
        try:
            # Honour the persisted profile budget.  A lower bound here used
            # to silently clamp native profiles and could discard every
            # citation before the no-provider specialist ran.
            max_context = min(256 * 1024, max(1, int(max_context)))
        except (TypeError, ValueError):
            max_context = 64 * 1024
        encoded = json.dumps(_json(profile_context), separators=(",", ":"), sort_keys=True)
        original_size = len(encoded)
        context_budget = {"limit": max_context, "original_bytes": original_size, "used_bytes": original_size, "truncated": False, "status": "within_budget", "overflow": False}
        if len(encoded) > max_context:
            # Any reduction is an explicit budget overflow. Consumers must be
            # able to distinguish a complete context from a degraded one.
            context_budget.update({"truncated": True, "status": "overflow", "overflow": True})
            for field in ("findings", "historical_analogues", "decision_action_outcome_learning", "scenario_inputs"):
                profile_context[field] = [] if field != "scenario_inputs" else {}
                encoded = json.dumps(_json(profile_context), separators=(",", ":"), sort_keys=True)
                if len(encoded) <= max_context:
                    break
        if len(encoded) > max_context:
            # Keep a bounded cited anchor even for very small custom budgets;
            # never degrade to an uncited profile-only result.
            profile_context = {"profile": {"id": key}, "findings": []}
            if evidence_anchor:
                profile_context["findings"] = [evidence_anchor]
            encoded = json.dumps(_json(profile_context), separators=(",", ":"), sort_keys=True)
            context_budget.update({"used_bytes": len(encoded), "status": "overflow", "overflow": True})
        else:
            context_budget["used_bytes"] = len(encoded)
        profile_context["context_budget"] = context_budget
        provider = self.specialist_provider
        if isinstance(provider, Mapping):
            provider = provider.get(key)
        if provider is None:
            provider = getattr(self.os, "strategic_reasoning_provider", None)
        if provider is not None:
            try:
                raw, metadata = invoke_reasoning_provider(provider, profile_context)
                result = dict(raw)
                result["provider_metadata"] = metadata
                result.setdefault("context_budget", context_budget)
                return result
            except Exception:
                # Optional provider failures degrade this perspective to the
                # deterministic local path; no provider exception escapes.
                pass
        handler = self.specialist_handlers.get(key)
        if handler is not None:
            result = dict(handler(profile_context))
            result.setdefault("provider_metadata", {"status": "injected_handler", "profile_id": key})
            result.setdefault("context_budget", context_budget)
            return result
        profile_domains = self._field(profile, "domains") or self._field(profile, "allowed_domains") or ()
        profile_domains = [str(item) for item in profile_domains]
        findings = profile_context.get("findings") or []
        first = self._select_deterministic_finding(findings, profile_domains, key)
        all_evidence = first.get("evidence", []) if isinstance(first, Mapping) else []
        domain_evidence = self._domain_matched_evidence(all_evidence, profile_domains)
        evidence = list((domain_evidence or list(all_evidence))[:4])
        specialty = _text(self._field(profile, "specialty") or self._field(profile, "mission") or key)
        method = _text(self._field(profile, "reasoning_method") or "bounded evidence review")
        base_hypothesis = (first.get("hypotheses") or [{"text": "No hypothesis established."}])[0].get("text", "No hypothesis established.") if isinstance(first, Mapping) else "No hypothesis established."
        method_key = f"{key}"
        distinct = {
            "account_strategist": ("Retention/expansion lens", "Prioritize account value protection and a reversible client review."),
            "relationship_analyst": ("Stakeholder health lens", "Check relationship signals and assign an owner for the next touchpoint."),
            "delivery_analyst": ("Commitment variance lens", "Rebaseline the at-risk commitment and confirm a delivery owner."),
            "performance_analyst": ("Performance variance lens", "Compare the latest operating signal with its baseline before reallocating effort."),
            "finance_scope_analyst": ("Margin/scope lens", "Quantify scope or margin exposure before approving additional work."),
            "capacity_planner": ("Capacity constraint lens", "Sequence work against available capacity and surface the staffing tradeoff."),
            "brand_creative_analyst": ("Creative fit lens", "Validate creative consistency and request a bounded asset review."),
            "research_analyst": ("Evidence synthesis lens", "Separate observed evidence from assumptions and identify the next measurement."),
            "risk_analyst": ("Risk boundary lens", "Contain the risk, preserve opposing evidence, and require human approval for one-way action."),
            "scenario_analyst": ("Scenario sensitivity lens", "Model bounded what-if branches and compare their reversible effects."),
            "historical_analogue_analyst": ("Historical pattern lens", "Compare the visible signal with prior outcomes, without treating analogy as fact."),
            "reality_checker": ("Reality check lens", "Challenge unsupported claims and mark the result for review when evidence conflicts."),
            "executive_synthesizer": ("Executive prioritization lens", "Frame the highest-impact decision, options, and explicit human checkpoint."),
        }.get(method_key, (f"{specialty} lens", "Review the bounded evidence and choose a reversible next step."))
        return {
            "finding": f"{distinct[0]}: " + (_text(first.get("summary"), "No visible finding.") if isinstance(first, Mapping) else "No visible finding."),
            "evidence_for": evidence,
            "evidence_against": first.get("opposing_evidence", [])[:2] if isinstance(first, Mapping) else [],
            "assumptions": [f"{method} applied to ACL-visible canonical records."],
            "unknowns": ["Unobserved external causes remain unknown."],
            "hypothesis": f"{distinct[0]} using {method}: {base_hypothesis}",
            "confidence": ((first.get("confidence") or {}).get("score", 0.35) if isinstance(first, Mapping) else 0.35),
            "analogues": profile_context.get("historical_analogues", [])[:4],
            "risks": [item.get("summary") for item in (first.get("scenarios", [])[:2] if isinstance(first, Mapping) else [])],
            "options": [first.get("recommendation")] if isinstance(first, Mapping) else [],
            "recommendation": {"summary": distinct[1], "rationale": f"{method} applied to permitted {', '.join(profile_domains[:2])} evidence."},
            "expected_impact": first.get("impact", {}) if isinstance(first, Mapping) else {},
            "needs_review": not bool(findings),
            "dissent": first.get("opposing_evidence", [])[:2] if isinstance(first, Mapping) else [],
            "context_budget": context_budget,
            "status": "available" if findings else "insufficient_evidence",
            "scope": profile_context.get("scope", {}),
            "domain_coverage": profile_context.get("domain_coverage", {}),
        }

    def _run_specialists(
        self,
        profiles: Sequence[Any],
        context: Mapping[str, Any],
        allowed_refs: set[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Run specialists in parallel with one bounded wall-clock deadline.

        Results are reassembled in profile order so concurrency never changes
        the persisted trace or synthesis input ordering.
        """
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        selected = list(profiles[: self.limits.max_specialists])
        if not selected:
            return results, errors
        executor = ThreadPoolExecutor(max_workers=len(selected), thread_name_prefix="intel-specialist")
        futures = [
            (profile, self._profile_key(profile), executor.submit(self._invoke_specialist, self._profile_key(profile), profile, context))
            for profile in selected
        ]
        deadline = time.monotonic() + max(0.01, float(self.limits.timeout_seconds))
        try:
            for profile, profile_key, future in futures:
                try:
                    remaining = max(0.0, deadline - time.monotonic())
                    raw = future.result(timeout=remaining)
                    raw = self._filter_raw_evidence_refs(raw, allowed_refs)
                    normalized = validate_expert_result(raw, allowed_refs=allowed_refs)
                    normalized["profile"] = self._contract_ref(profile)
                    normalized["specialist_id"] = profile_key
                    normalized["perspective"] = _text(self._field(profile, "specialty") or self._field(profile, "mission") or profile_key)
                    if isinstance(raw, Mapping) and raw.get("provider_metadata"):
                        normalized["provider_metadata"] = _json(raw.get("provider_metadata"))
                    if isinstance(raw, Mapping) and isinstance(raw.get("context_budget"), Mapping):
                        normalized["context_budget"] = _json(raw.get("context_budget"))
                    results.append(normalized)
                except FutureTimeoutError:
                    future.cancel()
                    errors.append(f"{profile_key}:timeout")
                except Exception as exc:
                    errors.append(f"{profile_key}:{type(exc).__name__}")
        finally:
            # Do not wait for a runaway handler after its deadline. Handlers
            # receive no store and therefore cannot mutate OS state.
            executor.shutdown(wait=False, cancel_futures=True)
        return results, errors

    def _synthesize(self, situation: Mapping[str, Any], specialists: list[dict[str, Any]], contradictions: list[dict[str, Any]], errors: list[str], allowed_refs: set[str]) -> dict[str, Any]:
        if not specialists:
            return validate_expert_result({
                "finding": "No specialist produced a bounded result.", "evidence_for": [], "evidence_against": [],
                "assumptions": [], "unknowns": ["Specialist output unavailable."], "hypothesis": "No hypothesis established.",
                "confidence": 0.0, "analogues": [], "risks": errors, "options": [], "recommendation": {"summary": "Review available evidence manually."},
                "expected_impact": {"level": "unknown"}, "needs_review": True, "dissent": [],
            }, allowed_refs=allowed_refs)
        def collect(field: str) -> list[Any]:
            return [item for specialist in specialists for item in specialist.get(field, [])][: self.limits.max_items]

        def distinct_values(field: str) -> list[Any]:
            values = [specialist.get(field) for specialist in specialists if specialist.get(field) not in (None, "", {}, [])]
            unique: list[Any] = []
            seen: set[str] = set()
            for value in values:
                marker = json.dumps(_json(value), sort_keys=True, separators=(",", ":"))
                if marker not in seen:
                    seen.add(marker)
                    unique.append(value)
            return unique

        findings = distinct_values("finding")
        hypotheses = distinct_values("hypothesis")
        recommendations = distinct_values("recommendation")
        impacts = distinct_values("expected_impact")
        combined = {
            "finding": " | ".join(str(value) for value in findings)[:MAX_TEXT] or "No finding returned.",
            "evidence_for": collect("evidence_for"),
            "evidence_against": collect("evidence_against"),
            "assumptions": [item for specialist in specialists for item in specialist.get("assumptions", [])][: self.limits.max_items],
            "unknowns": [item for specialist in specialists for item in specialist.get("unknowns", [])][: self.limits.max_items],
            "hypothesis": hypotheses[0] if len(hypotheses) == 1 else ("Competing specialist hypotheses: " + " | ".join(str(value) for value in hypotheses))[:MAX_TEXT],
            "confidence": round(sum(float(item.get("confidence", 0.0)) for item in specialists) / len(specialists), 3),
            "analogues": collect("analogues"),
            "risks": collect("risks"),
            "options": collect("options"),
            "recommendation": recommendations[0] if len(recommendations) == 1 else {"summary": "Review the synthesized specialist perspectives before acting.", "alternatives": recommendations[: self.limits.max_items]},
            "expected_impact": impacts[0] if len(impacts) == 1 else {"perspectives": impacts[: self.limits.max_items]},
            "needs_review": any(bool(item.get("needs_review")) for item in specialists) or bool(contradictions or errors),
            "dissent": collect("dissent"),
        }
        if contradictions:
            combined["unknowns"] = list(combined.get("unknowns", [])) + ["Independent specialists disagree on the visible signal."]
        return validate_expert_result(combined, allowed_refs=allowed_refs)

    @staticmethod
    def _bounded_iterations(value: Any, default: int = MAX_ITERATIONS) -> int:
        try:
            return max(1, min(MAX_ITERATIONS, int(value)))
        except (TypeError, ValueError):
            return default

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
                    contradictions.append({
                        "left": left.get("profile"), "right": right.get("profile"),
                        "left_hypothesis": left.get("hypothesis"), "right_hypothesis": right.get("hypothesis"),
                        "left_evidence": left.get("evidence_for", [])[:8], "right_evidence": right.get("evidence_for", [])[:8],
                        "reason": "Independent hypotheses differ.",
                    })
        return contradictions[:MAX_ITEMS]

    @staticmethod
    def _disagreement_summary(
        specialists: Sequence[Mapping[str, Any]],
        contradictions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Return an inspectable, confidence-weighted specialist debate.

        A weighted majority is only a tie-breaker for the read model.  A close
        margin remains explicitly contested so callers cannot mistake a slim
        vote for consensus.  Profile identifiers are retained for human
        review and no specialist is silently discarded.
        """
        entries = [
            {
                "specialist_id": str(item.get("specialist_id") or ""),
                "profile": item.get("profile"),
                "hypothesis": str(item.get("hypothesis") or "").strip(),
                "confidence": round(max(0.0, min(1.0, float(item.get("confidence") or 0.0))), 3),
            }
            for item in specialists
            if str(item.get("hypothesis") or "").strip()
        ]
        if not entries:
            return {
                "status": "insufficient",
                "specialist_count": len(specialists),
                "hypothesis_count": 0,
                "weighted_confidence": 0.0,
                "confidence_margin": 0.0,
                "majority_hypothesis": None,
                "minority": [],
                "resolution": "human_review",
            }
        buckets: dict[str, dict[str, Any]] = {}
        for entry in entries:
            key = entry["hypothesis"].casefold()
            bucket = buckets.setdefault(key, {"hypothesis": entry["hypothesis"], "weight": 0.0, "members": []})
            bucket["weight"] += entry["confidence"]
            bucket["members"].append(entry)
        ranked = sorted(buckets.values(), key=lambda item: (-item["weight"], item["hypothesis"].casefold()))
        total_weight = sum(float(item["weight"]) for item in ranked)
        top = ranked[0]
        second_weight = float(ranked[1]["weight"]) if len(ranked) > 1 else 0.0
        margin = round((float(top["weight"]) - second_weight) / max(total_weight, 1e-9), 3)
        status = "contested" if contradictions else "consensus"
        if len(ranked) > 1 and margin < 0.15:
            status = "contested"
        minority = [
            {"hypothesis": bucket["hypothesis"], "weight": round(float(bucket["weight"]), 3),
             "specialists": [member["specialist_id"] for member in bucket["members"]]}
            for bucket in ranked[1:]
        ]
        return {
            "status": status,
            "specialist_count": len(specialists),
            "hypothesis_count": len(ranked),
            "weighted_confidence": round(float(top["weight"]) / max(len(top["members"]), 1), 3),
            "confidence_margin": margin,
            "majority_hypothesis": top["hypothesis"],
            "majority_specialists": [member["specialist_id"] for member in top["members"]],
            "minority": minority[:MAX_ITEMS],
            "resolution": "human_review" if status == "contested" else "weighted_consensus",
        }

    @staticmethod
    def _historical_learning(analogues: Any) -> dict[str, Any]:
        """Summarize prior analogue outcomes without inferring absent data."""
        rows = [item for item in (analogues or []) if isinstance(item, Mapping)]
        resolved = 0.0
        weighted = 0.0
        known_outcomes = 0
        for item in rows[:MAX_ITEMS]:
            stats = item.get("outcome_stats") if isinstance(item.get("outcome_stats"), Mapping) else {}
            similarity = _score(item.get("similarity"), _score(item.get("confidence"), 0.0))
            rate = stats.get("resolution_rate")
            if rate is None:
                continue
            try:
                rate = max(0.0, min(1.0, float(rate)))
            except (TypeError, ValueError):
                continue
            weighted += similarity
            resolved += similarity * rate
            known_outcomes += 1
        rate = round(resolved / weighted, 3) if weighted else None
        return {
            "status": "available" if rows else "insufficient_evidence",
            "analogue_count": len(rows),
            "outcome_observation_count": known_outcomes,
            "weighted_resolution_rate": rate,
            "recommendation_signal": (
                "Prior comparable interventions resolved the signal; reuse cautiously."
                if rate is not None and rate >= 0.6 else
                "Prior analogues are mixed or unresolved; require a reversible review."
                if rate is not None else
                "No measured analogue outcome is available; do not generalize."
            ),
        }

    @staticmethod
    def _scenario_analysis(inputs: Any) -> dict[str, Any]:
        values = dict(inputs) if isinstance(inputs, Mapping) else {}
        retained = values.get("retained_inputs") if isinstance(values.get("retained_inputs"), Mapping) else values
        projection = values.get("projection") if isinstance(values.get("projection"), Mapping) else {}
        unknowns = [str(item) for item in (values.get("constraints") or []) if item]
        return {
            "status": "available" if retained or projection else "insufficient_evidence",
            "retained_inputs": _json(retained),
            "projection": _json(projection),
            "constraint_count": len(unknowns),
            "constraints": unknowns[:MAX_ITEMS],
            "sensitivity": "bounded_inputs_only" if retained or projection else "none",
        }

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
            "domains": situation.get("domains", {}),
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

    def _runbook_gates(
        self,
        runbook: Any,
        situation: Mapping[str, Any],
        specialists: Sequence[Mapping[str, Any]],
        contradictions: Sequence[Mapping[str, Any]],
        errors: Sequence[str],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Evaluate runbook gates as bounded, read-only orchestration stages."""
        if runbook is None:
            return [], False
        activation = list(self._field(runbook, "activation_sequence") or [])
        handoff = list(self._field(runbook, "handoff_gates") or [])
        quality = list(self._field(runbook, "quality_gates") or [])
        scenario_policy = self._field(runbook, "scenario_policy")
        events: list[dict[str, Any]] = []
        review = False
        activation_status = "completed" if activation else "degraded"
        if not activation:
            review = True
        handoff_ok = bool(specialists) and not errors
        review |= not handoff_ok and bool(handoff)
        quality_ok = bool(specialists) and all(item.get("evidence_for") or item.get("unknowns") for item in specialists)
        review |= not quality_ok and bool(quality)
        contradiction_ok = not contradictions
        review |= not contradiction_ok
        scenario_ok = not scenario_policy or isinstance(situation.get("context", {}).get("scenario_inputs"), Mapping)
        review |= not scenario_ok
        events.append({
            "stage": "runbook_gates",
            "status": "review" if review else activation_status,
            "activation": {"status": activation_status, "steps": activation[:8]},
            "handoff": {"status": "completed" if handoff_ok else "degraded", "gates": handoff[:8]},
            "quality": {"status": "completed" if quality_ok else "review", "gates": quality[:8]},
            "contradiction": {"status": "completed" if contradiction_ok else "review", "count": len(contradictions)},
            "scenario": {"status": "completed" if scenario_ok else "review", "policy": scenario_policy},
        })
        return events, review

    def _restrict_profile_context(self, profile: Any, context: Mapping[str, Any]) -> dict[str, Any]:
        """Give each specialist only evidence in its declared domains."""
        domains = {
            str(item).strip().lower()
            for item in (self._field(profile, "domains") or self._field(profile, "allowed_domains") or ())
            if str(item).strip()
        }
        if not domains:
            result = dict(context)
            tools = self._field(profile, "allowed_tools") or self._field(profile, "allowed_tool_refs") or ()
            result["allowed_tools"] = [str(item) for item in tools][:MAX_ITEMS]
            result["tools"] = list(result["allowed_tools"])
            return result
        result = dict(context)
        tools = self._field(profile, "allowed_tools") or self._field(profile, "allowed_tool_refs") or ()
        result["allowed_tools"] = [str(item) for item in tools][:MAX_ITEMS]
        result["tools"] = list(result["allowed_tools"])
        findings = []
        for item in context.get("findings", []) or []:
            if not isinstance(item, Mapping):
                continue
            marker = " ".join(str(item.get(key) or "").lower() for key in ("id", "type", "title", "domain"))
            domain_evidence = self._domain_matched_evidence(item.get("evidence", []), domains)
            if any(domain in marker for domain in domains) or domain_evidence:
                scoped = dict(item)
                if domain_evidence:
                    matched = next((domain for domain in domains if self._evidence_matches_domain(domain_evidence[0], domain)), None)
                    scoped["evidence"] = domain_evidence[:4]
                    if matched:
                        scoped.setdefault("domain", matched)
                findings.append(scoped)
        # A workspace can legitimately have no finding whose title carries a
        # specialist's domain label. Preserve one ACL-visible anchor in that
        # case so offline specialists still return cited, useful uncertainty
        # instead of an uncited generic fallback.
        if not findings:
            # A finding title is not guaranteed to carry its source domain.
            # Recover a domain-specific anchor from canonical evidence refs so
            # deterministic specialists do not all inherit findings[0].
            domain_evidence: list[tuple[str, Mapping[str, Any]]] = []
            for item in context.get("findings", []) or []:
                if not isinstance(item, Mapping):
                    continue
                for evidence in item.get("evidence", []) or []:
                    matched = next((domain for domain in domains if self._evidence_matches_domain(evidence, domain)), None)
                    if matched:
                        domain_evidence.append((matched, evidence))
            if domain_evidence:
                chosen_domain = domain_evidence[0][0]
                findings = [{
                    "summary": f"Visible {chosen_domain} evidence requiring a domain-specific review.",
                    "domain": chosen_domain,
                    "evidence": [evidence for domain, evidence in domain_evidence if domain == chosen_domain][:4],
                }]
            else:
                first = next((item for item in context.get("findings", []) or [] if isinstance(item, Mapping)), None)
                if first is not None and first.get("evidence"):
                    findings = [{
                        "summary": "Visible evidence requiring a domain-specific review.",
                        "evidence": list(first.get("evidence", []))[:4],
                        "confidence": first.get("confidence"),
                        "opposing_evidence": first.get("opposing_evidence", []),
                        "recommendation": first.get("recommendation", {}),
                        "impact": first.get("impact", {}),
                    }]
        result["findings"] = findings
        if isinstance(context.get("domains"), Mapping):
            result["domains"] = {
                key: value for key, value in context["domains"].items()
                if str(key).lower() in domains
            }
        coverage = {domain: {"finding_count": 0, "evidence_count": 0} for domain in sorted(domains)}
        for item in findings:
            marker = " ".join(str(item.get(key) or "").lower() for key in ("id", "type", "title", "domain", "summary"))
            matched = [domain for domain in domains if domain in marker] or list(domains)
            evidence_count = len(item.get("evidence", []) or [])
            for domain in matched:
                coverage[domain]["finding_count"] += 1
                coverage[domain]["evidence_count"] += evidence_count
        result["domain_coverage"] = coverage
        # These aggregates lack reliable domain labels; do not leak broad
        # company history into a domain-scoped specialist.
        result["historical_analogues"] = [
            item for item in context.get("historical_analogues", []) or []
            if isinstance(item, Mapping) and any(domain in json.dumps(item, sort_keys=True).lower() for domain in domains)
        ]
        result["decision_action_outcome_learning"] = [
            item for item in context.get("decision_action_outcome_learning", []) or []
            if isinstance(item, Mapping) and any(domain in json.dumps(item, sort_keys=True).lower() for domain in domains)
        ]
        scenario = context.get("scenario_inputs") or {}
        allowed_scenario = {
            "work": {"work_hours_delta", "deadline_days_delta"},
            "capacity": {"capacity_hours_delta", "leave_hours_delta", "hiring_hours_delta"},
            "finance": {"finance_amount_delta", "client_revenue_delta", "client_cost_delta"},
            "scope": {"scope_usage_delta"},
            "client_health": {"client_health_delta"},
        }
        keys = set().union(*(allowed_scenario.get(domain, set()) for domain in domains))
        result["scenario_inputs"] = {key: value for key, value in scenario.items() if key in keys}
        return result

    @staticmethod
    def _first_cited_finding_anchor(findings: Any) -> dict[str, Any] | None:
        for item in findings or []:
            if not isinstance(item, Mapping):
                continue
            evidence = next((entry for entry in item.get("evidence", []) or [] if _ref_id(entry)), None)
            if evidence is not None:
                return {
                    "summary": _text(item.get("summary") or item.get("title"), "Visible evidence"),
                    "evidence": [_json(evidence)],
                }
        return None

    @staticmethod
    def _filter_raw_evidence_refs(value: Any, allowed_refs: set[str]) -> Any:
        if not isinstance(value, Mapping):
            return value
        filtered = dict(value)
        dropped_citations = 0
        for field in ("evidence_for", "evidence_against", "analogues", "dissent"):
            if field not in filtered:
                continue
            checked: list[Any] = []
            for item in filtered.get(field, []) or []:
                ref = _ref_id(item)
                if ref is None or ref not in allowed_refs:
                    dropped_citations += 1
                    continue
                checked.append(item)
            filtered[field] = checked
        if dropped_citations:
            filtered["status"] = "degraded"
            filtered["needs_review"] = True
            unknowns = [_text(item) for item in _bounded_list(filtered.get("unknowns"))]
            filtered["unknowns"] = unknowns[: MAX_ITEMS - 1] + [
                f"{dropped_citations} evidence item(s) lacked an allowed citation and were dropped."
            ]
        return filtered

    @staticmethod
    def _evidence_matches_domain(evidence: Any, domain: str) -> bool:
        """Match canonical evidence kinds to a declared specialist domain."""
        if not isinstance(evidence, Mapping):
            return False
        ref = evidence.get("object_ref") or evidence.get("ref") or {}
        kind = str(ref.get("type") if isinstance(ref, Mapping) else ref).lower()
        aliases = {
            "performance": {"campaign_metric_snapshot", "campaign_metrics", "performance_insight"},
            "analytics": {"campaign_metric_snapshot", "campaign_metrics", "performance_insight"},
            "campaigns": {"campaign_metric_snapshot", "campaign_metrics"},
            "delivery": {"work_item", "work_event", "review"},
            "workflow": {"work_item", "work_event", "review"},
            "capacity": {"capacity_snapshot"},
            "finance": {"finance", "revenue", "invoice", "cost"},
            "scope": {"scope_usage", "contract", "scope_allowance"},
            "client_success": {"client_health_snapshot", "decision", "signal"},
            "relationships": {"client_health_snapshot", "touchpoint", "feedback_event"},
            "risk": {"risk", "signal"},
            "research": {"fact", "document", "source"},
            "brain": {"fact", "document", "source"},
        }
        return kind in aliases.get(domain, {domain}) or domain in kind

    def _select_deterministic_finding(self, findings: Any, profile_domains: Sequence[str], key: str) -> Mapping[str, Any]:
        candidates = [item for item in (findings or []) if isinstance(item, Mapping)]
        if not candidates:
            return {}
        domains = [str(item).strip().lower() for item in profile_domains if str(item).strip()]
        if domains:
            scored: list[tuple[int, int, Mapping[str, Any]]] = []
            for index, item in enumerate(candidates):
                marker = " ".join(str(item.get(field) or "").lower() for field in ("id", "type", "title", "domain", "summary"))
                evidence = self._domain_matched_evidence(item.get("evidence", []), domains)
                score = len(evidence) * 2 + sum(1 for domain in domains if domain in marker)
                if score:
                    scored.append((score, -index, item))
            if scored:
                return max(scored, key=lambda item: (item[0], item[1]))[2]
        return candidates[sum(ord(char) for char in key) % len(candidates)]

    @staticmethod
    def _domain_matched_evidence(evidence: Any, domains: Sequence[str]) -> list[Any]:
        domain_set = [str(item).strip().lower() for item in domains if str(item).strip()]
        if not domain_set:
            return []
        return [
            item for item in (evidence or [])
            if any(IntelligenceOrchestrator._evidence_matches_domain(item, domain) for domain in domain_set)
        ]
