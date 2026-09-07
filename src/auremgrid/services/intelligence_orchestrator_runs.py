"""Run lifecycle and persistence for intelligence orchestration."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from auremgrid.services.intelligence_orchestrator_shared import OrchestrationLimits, _json, _now


class IntelligenceOrchestratorRunsMixin:
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

    def latest_run(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any] | None:
        """Return the newest persisted trace visible to this exact workspace principal."""
        self.os._require_person_access(organization_id, workspace_id, person_id)
        row = self.os.store.conn.execute(
            """SELECT result_json FROM intelligence_orchestrator_runs
               WHERE organization_id=? AND workspace_id=? AND person_id=?
               ORDER BY generated_at DESC, created_at DESC, trace_id DESC
               LIMIT 1""",
            (organization_id, workspace_id, person_id),
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
        safety_id: str | None = None
        safety = getattr(self.os, "intelligence_evaluation_safety", None)
        safety_decision: dict[str, Any] | None = None
        if safety is not None:
            try:
                safety_decision = safety.can_start(organization_id, person_id, self.limits.task_class)
                policy = safety_decision.get("policy") or {}
                # Durable policy values are authoritative when available.
                self._run_budget = {
                    "tokens": 0, "cost": 0.0,
                    "max_tokens": min(int(self.limits.max_tokens), int(policy.get("max_tokens", self.limits.max_tokens))),
                    "max_cost": min(float(self.limits.max_cost_amount), float(policy.get("max_cost_amount", self.limits.max_cost_amount))),
                    "policy_max_tokens": int(policy.get("max_tokens", self.limits.max_tokens)),
                    "policy_max_cost": float(policy.get("max_cost_amount", self.limits.max_cost_amount)),
                    "cap_reason": None,
                }
                if safety_decision.get("allowed"):
                    try:
                        evaluation = safety.start(
                            organization_id, person_id, self.limits.task_class,
                            workspace_id=workspace_id, trace_id=trace_id,
                        )
                        safety_id = evaluation.get("id")
                    except Exception:
                        # Safety telemetry must never make read-only intelligence unavailable.
                        safety_id = None
            except Exception:
                self._run_budget = {"tokens": 0, "cost": 0.0, "max_tokens": int(self.limits.max_tokens), "max_cost": float(self.limits.max_cost_amount), "policy_max_tokens": int(self.limits.max_tokens), "policy_max_cost": float(self.limits.max_cost_amount), "cap_reason": None}
        else:
            self._run_budget = {"tokens": 0, "cost": 0.0, "max_tokens": int(self.limits.max_tokens), "max_cost": float(self.limits.max_cost_amount), "policy_max_tokens": int(self.limits.max_tokens), "policy_max_cost": float(self.limits.max_cost_amount), "cap_reason": None}
        evaluation_circuit_open = bool(safety_decision is not None and not safety_decision.get("allowed", True))
        # Keep the situation/read model available, but do not invoke any
        # specialist/provider while the breaker is open.
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
        if evaluation_circuit_open:
            iteration_budget = 0
        route_reason = "matched" if runbook else "no_match"
        trace.append({"stage": "runbook_router", "status": "completed" if runbook else "degraded", "reason": route_reason, "runbook": self._contract_ref(runbook)})

        if runbook is not None:
            context["runbook"] = {
                "id": self._contract_key(runbook),
                "required_evidence": [str(item) for item in (self._field(runbook, "required_evidence") or [])],
                "required_domains": [str(item) for item in (self._field(runbook, "required_domains") or self._field(runbook, "domains") or [])],
            }

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
            "action_descriptors": self._disabled_action_descriptors(),
            "allowed_actions": self._disabled_action_descriptors(),
            "runbook_route": {"status": "matched" if runbook else "no_match", "reason": route_reason},
            "runbook": self._contract_ref(runbook),
            "profiles": [self._contract_ref(p) for p in profiles[: self.limits.max_specialists]],
            "trace": trace,
            "limits": {"max_items": self.limits.max_items, "max_specialists": self.limits.max_specialists, "max_iterations": iteration_budget, "runbook_max_iterations": runbook_iterations, "max_tokens": self._run_budget.get("max_tokens") if self._run_budget else self.limits.max_tokens, "max_cost_amount": self._run_budget.get("max_cost") if self._run_budget else self.limits.max_cost_amount},
            "evaluation_safety": {"status": "capped" if self._run_budget and self._run_budget.get("cap_reason") else ("circuit_open" if safety_decision is not None and not safety_decision.get("allowed", True) else "shadow_only"), "cap_reason": self._run_budget.get("cap_reason") if self._run_budget else None, "estimated_tokens": self._run_budget.get("tokens", 0) if self._run_budget else 0, "cost_amount": self._run_budget.get("cost", 0.0) if self._run_budget else 0.0},
            "scope": {"organization_id": organization_id, "workspace_id": workspace_id, "person_id": person_id},
            "generated_at": _now(),
        }
        # Every emitted stage event is correlated to the persisted run.
        for event in trace:
            event.setdefault("trace_id", trace_id)
        self._persist_run(result)
        if safety is not None and safety_id:
            try:
                budget = self._run_budget or {}
                reported_tokens = int(budget.get("tokens", 0))
                reported_cost = float(budget.get("cost", 0.0))
                # Ensure the durable evaluator records the same cap that
                # stopped specialist acceptance, opening its rolling breaker
                # after the configured threshold.
                if budget.get("cap_reason") == "token_cap":
                    reported_tokens = max(reported_tokens, int(budget.get("policy_max_tokens", budget.get("max_tokens", 0))) + 1)
                elif budget.get("cap_reason") == "cost_cap":
                    reported_cost = max(reported_cost, float(budget.get("policy_max_cost", budget.get("max_cost", 0.0))) + 0.01)
                safety.complete(
                    organization_id, person_id, safety_id, workspace_id=workspace_id,
                    input_tokens=reported_tokens,
                    output_tokens=0,
                    cost_amount=reported_cost,
                    metadata={"cap_reason": budget.get("cap_reason")},
                )
            except Exception:
                pass
        self._run_budget = None
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
    def _disabled_action_descriptors() -> list[dict[str, Any]]:
        reason = "No safe backend action descriptor was granted for this read-only orchestration trace."
        return [
            {"action": "challenge", "label": "Challenge", "safe": False, "disabled": True, "reason": reason},
            {"action": "save_insight", "label": "Save insight", "safe": False, "disabled": True, "reason": reason},
            {"action": "execute_approved_plan", "label": "Execute approved plan", "safe": False, "disabled": True, "reason": reason},
        ]

