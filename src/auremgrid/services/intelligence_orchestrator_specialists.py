"""Specialist invocation and per-run budget handling."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Mapping, Sequence

from auremgrid.adapters.reasoning import invoke_reasoning_provider
from auremgrid.domain.errors import ValidationError
from auremgrid.services.intelligence_orchestrator_shared import MAX_ITEMS, _bounded_list, _json, _ref_id, _text, validate_expert_result


class IntelligenceOrchestratorSpecialistsMixin:
    def _profile_payload(self, profile: Any) -> dict[str, Any]:
        keys = ("id", "version", "name", "specialty", "mission", "reasoning_method", "max_context", "max_iterations", "domains", "allowed_domains", "allowed_tools", "tools", "required_evidence")
        return {key: self._field(profile, key) for key in keys if self._field(profile, key) is not None}

    def _invoke_specialist(self, key: str, profile: Any, context: Mapping[str, Any], budget: dict[str, Any] | None, deadline: float | None) -> Mapping[str, Any]:
        profile_context = dict(context)
        profile_context["profile"] = self._profile_payload(profile)
        profile_context["retrieval_plan"] = self._build_retrieval_plan(profile, profile_context)
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
        # Character-to-token ratio is intentionally conservative but must not
        # reject a normal 13-profile deterministic fan-out under the default
        # 50k token cap. Provider-reported usage remains authoritative.
        estimated_tokens = max(1, len(encoded) // 16)
        if not self._consume_budget(budget, tokens=estimated_tokens):
            raise ValidationError("evaluation token cap exceeded")
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
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("specialist run deadline elapsed before provider invocation")
                raw, metadata = invoke_reasoning_provider(provider, profile_context)
                if isinstance(metadata, Mapping):
                    if not self._consume_budget(
                        budget,
                        tokens=max(0, int(metadata.get("input_tokens") or 0)) + max(0, int(metadata.get("output_tokens") or 0)),
                        cost=float(metadata.get("cost_amount") or metadata.get("cost") or 0.0),
                    ):
                        raise ValidationError("evaluation budget cap exceeded")
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

    def _consume_budget(self, budget: dict[str, Any] | None, *, tokens: int = 0, cost: float = 0.0) -> bool:
        """Atomically reserve bounded evaluation budget owned by one run.

        A missing or closed budget refuses consumption: no caller may spend
        against shared state or after its run finished collecting results.
        """
        if budget is None or budget.get("closed"):
            return False
        with self._budget_lock:
            next_tokens = int(budget.get("tokens", 0)) + max(0, int(tokens))
            next_cost = float(budget.get("cost", 0.0)) + max(0.0, float(cost))
            if next_tokens > int(budget.get("max_tokens", self.limits.max_tokens)):
                budget["cap_reason"] = "token_cap"
                return False
            if next_cost > float(budget.get("max_cost", self.limits.max_cost_amount)):
                budget["cap_reason"] = "cost_cap"
                return False
            budget["tokens"], budget["cost"] = next_tokens, next_cost
            return True

    def _run_specialists(
        self,
        profiles: Sequence[Any],
        context: Mapping[str, Any],
        allowed_refs: set[str],
        budget: dict[str, Any] | None,
        deadline: float | None,
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
            (profile, self._profile_key(profile), executor.submit(self._invoke_specialist, self._profile_key(profile), profile, context, budget, deadline))
            for profile in selected
        ]
        fanout_deadline = time.monotonic() + max(0.01, float(self.limits.timeout_seconds))
        effective_deadline = min(fanout_deadline, deadline) if deadline is not None else fanout_deadline
        try:
            for profile, profile_key, future in futures:
                try:
                    remaining = max(0.0, effective_deadline - time.monotonic())
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

