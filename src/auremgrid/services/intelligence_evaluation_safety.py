from __future__ import annotations

"""Durable, shadow-only evaluation and safety boundary for Intelligence runs."""

from datetime import datetime, timedelta, timezone
import json
import uuid
from typing import Any, Mapping

from auremgrid.domain.errors import AuthorizationError, ValidationError


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


DEFAULT_POLICY = {
    "max_runtime_ms": 120000,
    "max_cost_amount": 5.0,
    "max_tokens": 50000,
    "breaker_threshold": 3,
    "breaker_window_seconds": 900,
    "breaker_open_seconds": 300,
}


class IntelligenceEvaluationSafety:
    """Persist evaluation telemetry while never mutating routing decisions."""

    def __init__(self, os: Any) -> None:
        self.os = os
        self.conn = os.store.conn

    def policy(self, organization_id: str, person_id: str, task_class: str) -> dict[str, Any]:
        self._authorize(organization_id, person_id)
        row = self.conn.execute(
            "SELECT * FROM intelligence_evaluation_policies WHERE organization_id=? AND task_class=?",
            (organization_id, task_class),
        ).fetchone()
        if row is None:
            return {"organization_id": organization_id, "task_class": task_class, **DEFAULT_POLICY, "failure_count": 0, "breaker_open_until": None}
        return dict(row)

    def can_start(self, organization_id: str, person_id: str, task_class: str) -> dict[str, Any]:
        policy = self.policy(organization_id, person_id, task_class)
        until = policy.get("breaker_open_until")
        open_now = bool(until and datetime.fromisoformat(str(until).replace("Z", "+00:00")) > _now())
        return {"allowed": not open_now, "shadow_only": True, "reason": "circuit_open" if open_now else "shadow_evaluation_only", "policy": policy}

    def configure_policy(self, organization_id: str, person_id: str, task_class: str, **limits: Any) -> dict[str, Any]:
        """Set hard evaluation caps; this never changes agent routing policy."""
        self._authorize(organization_id, person_id)
        current = self.policy(organization_id, person_id, task_class)
        values = {key: int(limits.get(key, current[key])) if key != "max_cost_amount" else float(limits.get(key, current[key])) for key in DEFAULT_POLICY}
        if any(values[key] <= 0 for key in DEFAULT_POLICY):
            raise ValidationError("evaluation caps must be positive")
        now = _now().isoformat()
        self.conn.execute(
            "INSERT INTO intelligence_evaluation_policies VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(organization_id,task_class) DO UPDATE SET max_runtime_ms=excluded.max_runtime_ms,max_cost_amount=excluded.max_cost_amount,max_tokens=excluded.max_tokens,breaker_threshold=excluded.breaker_threshold,breaker_window_seconds=excluded.breaker_window_seconds,breaker_open_seconds=excluded.breaker_open_seconds,updated_at=excluded.updated_at",
            (organization_id, task_class, values["max_runtime_ms"], values["max_cost_amount"], values["max_tokens"], values["breaker_threshold"], values["breaker_window_seconds"], values["breaker_open_seconds"], current.get("failure_count", 0), current.get("breaker_open_until"), now),
        )
        self.conn.commit()
        return self.policy(organization_id, person_id, task_class)

    def route(self, organization_id: str, person_id: str, task_class: str, requested_route: str) -> dict[str, Any]:
        """Return a shadow route descriptor without mutating routing selection."""
        decision = self.can_start(organization_id, person_id, task_class)
        return {"requested_route": requested_route, "selected_route": requested_route, "shadow_only": True, "allowed": decision["allowed"], "reason": decision["reason"]}

    def start(
        self,
        organization_id: str,
        person_id: str,
        task_class: str,
        *,
        workspace_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        specialist_profile_id: str | None = None,
        runbook_id: str | None = None,
        runbook_version: int | None = None,
        trace_id: str | None = None,
        agent_run_id: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(organization_id, person_id, workspace_id)
        decision = self.can_start(organization_id, person_id, task_class)
        if not decision["allowed"]:
            raise ValidationError("evaluation circuit breaker is open")
        now = _now().isoformat()
        item = {
            "id": _new_id("ieval"), "organization_id": organization_id, "workspace_id": workspace_id,
            "agent_run_id": agent_run_id, "trace_id": trace_id, "task_class": task_class,
            "provider": provider, "model": model, "specialist_profile_id": specialist_profile_id,
            "runbook_id": runbook_id, "runbook_version": runbook_version, "status": "shadow_only",
            "shadow_only": 1, "started_at": now, "completed_at": None, "latency_ms": None,
            "input_tokens": None, "output_tokens": None, "cost_amount": None, "cost_currency": None,
            "evidence_completeness": None, "evaluator_score": None, "human_acceptance": None,
            "revision_count": 0, "downstream_outcome_score": None, "cap_reason": None,
            "metadata_json": "{}", "created_at": now,
        }
        self.conn.execute(
            "INSERT INTO intelligence_evaluation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(item.values()),
        )
        self.conn.commit()
        return item

    def complete(
        self,
        organization_id: str,
        person_id: str,
        evaluation_id: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_amount: float | None = None,
        cost_currency: str | None = None,
        evidence_completeness: float | None = None,
        evaluator_score: float | None = None,
        human_acceptance: bool | None = None,
        revision_count: int = 0,
        downstream_outcome_score: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._authorize(organization_id, person_id)
        row = self.conn.execute("SELECT * FROM intelligence_evaluation_runs WHERE organization_id=? AND id=?", (organization_id, evaluation_id)).fetchone()
        if row is None:
            raise ValidationError("evaluation not found")
        item = dict(row)
        started = datetime.fromisoformat(str(item["started_at"]).replace("Z", "+00:00"))
        latency = max(0, int((_now() - started.astimezone(timezone.utc)).total_seconds() * 1000))
        policy = self.policy(organization_id, person_id, item["task_class"])
        total_tokens = (input_tokens or 0) + (output_tokens or 0)
        cap_reason = None
        if latency > int(policy["max_runtime_ms"]): cap_reason = "runtime_cap"
        elif total_tokens > int(policy["max_tokens"]): cap_reason = "token_cap"
        elif cost_amount is not None and float(cost_amount) > float(policy["max_cost_amount"]): cap_reason = "cost_cap"
        status = "capped" if cap_reason else "completed"
        updated = {
            **item, "status": status, "completed_at": _now().isoformat(), "latency_ms": latency,
            "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_amount": cost_amount,
            "cost_currency": cost_currency, "evidence_completeness": self._bounded_score(evidence_completeness),
            "evaluator_score": self._bounded_score(evaluator_score), "human_acceptance": None if human_acceptance is None else int(human_acceptance),
            "revision_count": max(0, int(revision_count)), "downstream_outcome_score": self._bounded_score(downstream_outcome_score),
            "cap_reason": cap_reason, "metadata_json": _json(metadata or {}),
        }
        assignments = ",".join(f"{key}=?" for key in updated if key not in {"id", "organization_id", "created_at"})
        values = [updated[key] for key in updated if key not in {"id", "organization_id", "created_at"}] + [evaluation_id, organization_id]
        self.conn.execute(f"UPDATE intelligence_evaluation_runs SET {assignments} WHERE id=? AND organization_id=?", values)
        if cap_reason:
            self._record_failure(organization_id, person_id, item["task_class"], evaluation_id, cap_reason, policy)
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM intelligence_evaluation_runs WHERE id=?", (evaluation_id,)).fetchone())

    def _record_failure(self, org: str, person: str, task_class: str, evaluation_id: str, reason: str, policy: Mapping[str, Any]) -> None:
        now = _now()
        count = int(policy.get("failure_count") or 0) + 1
        opened = now + timedelta(seconds=int(policy["breaker_open_seconds"])) if count >= int(policy["breaker_threshold"]) else None
        self.conn.execute(
            "INSERT INTO intelligence_evaluation_circuit_events VALUES (?,?,?,?,?,?,?)",
            (_new_id("ievent"), org, task_class, "opened" if opened else "cap_exceeded", evaluation_id, reason, now.isoformat()),
        )
        self.conn.execute(
            "INSERT INTO intelligence_evaluation_policies VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(organization_id,task_class) DO UPDATE SET failure_count=excluded.failure_count,breaker_open_until=excluded.breaker_open_until,updated_at=excluded.updated_at",
            (org, task_class, policy["max_runtime_ms"], policy["max_cost_amount"], policy["max_tokens"], policy["breaker_threshold"], policy["breaker_window_seconds"], policy["breaker_open_seconds"], count, opened.isoformat() if opened else policy.get("breaker_open_until"), now.isoformat()),
        )

    @staticmethod
    def _bounded_score(value: float | None) -> float | None:
        if value is None: return None
        return max(0.0, min(1.0, float(value)))

    def _authorize(self, organization_id: str, person_id: str, workspace_id: str | None = None) -> None:
        if workspace_id is None:
            if self.os.company.org_membership(organization_id, person_id) is None: raise AuthorizationError("organization membership required")
        else:
            self.os._require_person_access(organization_id, workspace_id, person_id)
