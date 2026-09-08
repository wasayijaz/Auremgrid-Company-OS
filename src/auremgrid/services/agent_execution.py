from __future__ import annotations

"""Durable cognitive agent execution boundary.

The executor keeps model reasoning (agent.think) separate from side effects
(agent.action).  Provider calls happen out-of-process so timeout/cancel can
terminate the work at the process boundary.
"""

import json
import multiprocessing
import queue
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from auremgrid.adapters.reasoning import StrategicReasoningProviderError, invoke_reasoning_provider


AGENT_EXECUTION_DDL = """
CREATE TABLE IF NOT EXISTS agent_thinking_results (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    workspace_id TEXT,
    agent_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT,
    model_id TEXT,
    tier INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('succeeded','failed','cancelled')),
    result_json TEXT,
    error_json TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    trace_json TEXT,
    citations_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id,run_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_thinking_results_run
    ON agent_thinking_results(organization_id,run_id);

CREATE TABLE IF NOT EXISTS agent_thinking_attempts (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    workspace_id TEXT,
    run_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    model_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    ok INTEGER NOT NULL CHECK(ok IN (0,1)),
    error TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    trace_json TEXT,
    citations_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_thinking_attempts_run
    ON agent_thinking_attempts(organization_id,run_id,created_at);

CREATE TABLE IF NOT EXISTS agent_executor_actions (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    workspace_id TEXT,
    agent_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','done','failed')),
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(organization_id,run_id),
    UNIQUE(organization_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_executor_actions_run
    ON agent_executor_actions(organization_id,run_id);
"""


class DeliberationProvider(Protocol):
    name: str

    def deliberate(self, context: Mapping[str, Any]) -> Mapping[str, Any] | str: ...


@dataclass(frozen=True)
class ModelEntry:
    id: str
    provider_name: str
    tier: int
    cost_per_1k_tokens: float
    max_tokens: int

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("model id is required")
        if not self.provider_name.strip():
            raise ValueError("provider name is required")
        if self.tier not in {1, 2, 3, 4}:
            raise ValueError("model tier must be between 1 and 4")
        if self.cost_per_1k_tokens < 0:
            raise ValueError("model cost cannot be negative")
        if self.max_tokens <= 0:
            raise ValueError("model max_tokens must be positive")


@dataclass(frozen=True)
class ThinkingBudget:
    max_tokens: int
    max_cost: float

    def __post_init__(self) -> None:
        if self.max_tokens < 0:
            raise ValueError("max_tokens cannot be negative")
        if self.max_cost < 0:
            raise ValueError("max_cost cannot be negative")


@dataclass(frozen=True)
class ThinkingAttempt:
    provider_name: str
    model_id: str
    tier: int
    latency_ms: int
    ok: bool
    error: str | None
    input_tokens: int
    output_tokens: int
    cost: float
    trace: Mapping[str, Any] | list[Any] | str | None = None
    citations: tuple[Any, ...] = ()


@dataclass(frozen=True)
class ThinkingResult:
    status: str
    result: Mapping[str, Any] | None
    attempts: tuple[ThinkingAttempt, ...]
    model_id: str | None
    input_tokens: int
    output_tokens: int
    cost: float
    error: str | None = None
    trace: Mapping[str, Any] | list[Any] | str | None = None
    citations: tuple[Any, ...] = ()


AGENT_THINK_JOB_TYPE = "agent.think"


class AgentThinkUnavailable(RuntimeError):
    """Raised only when a durable think job cannot resolve a provider."""


def agent_think_available(providers: ProviderRegistry, models: ModelRegistry, tier: int = 1) -> bool:
    """Whether a tier has at least one model whose provider is configured."""
    return bool(models.configured_chain(tier, providers))


def thinking_surface_enabled(owner: Any, tier: int = 1) -> bool:
    """Resolve optional runtime wiring without making the base API depend on it."""
    providers = getattr(owner, "agent_think_providers", None)
    models = getattr(owner, "agent_think_models", None)
    if providers is not None and models is not None:
        return agent_think_available(providers, models, tier)
    runtime = getattr(owner, "agent_thinking", None)
    if runtime is not None:
        providers = getattr(runtime, "providers", None)
        models = getattr(runtime, "models", None)
        if providers is not None and models is not None:
            return agent_think_available(providers, models, tier)
    return getattr(owner, "strategic_reasoning_provider", None) is not None


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Any] = {}

    def register(self, name: str, provider: Any) -> None:
        key = name.strip()
        if not key:
            raise ValueError("provider name is required")
        if not any(callable(getattr(provider, method, None)) for method in ("deliberate", "reason", "generate")):
            raise ValueError("provider must expose deliberate(context)")
        self._providers[key] = provider

    def get(self, name: str) -> Any:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"provider not registered: {name}") from exc

    def has(self, name: str) -> bool:
        return name in self._providers


class ModelRegistry:
    def __init__(self) -> None:
        self._models: dict[str, ModelEntry] = {}
        self._fallbacks: dict[int, list[str]] = {}

    def register(self, model: ModelEntry) -> None:
        self._models[model.id] = model

    def set_fallback_chain(self, tier: int, model_ids: list[str]) -> None:
        if tier not in {1, 2, 3, 4}:
            raise ValueError("model tier must be between 1 and 4")
        if not model_ids:
            raise ValueError("fallback chain cannot be empty")
        missing = [model_id for model_id in model_ids if model_id not in self._models]
        if missing:
            raise KeyError(f"models not registered: {', '.join(missing)}")
        self._fallbacks[tier] = list(model_ids)

    def fallback_chain(self, tier: int) -> list[ModelEntry]:
        return [self._models[model_id] for model_id in self._fallbacks.get(tier, [])]

    def configured_chain(self, tier: int, providers: ProviderRegistry) -> list[ModelEntry]:
        return [model for model in self.fallback_chain(tier) if providers.has(model.provider_name)]


class SQLiteAgentExecutionStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def initialize(self) -> None:
        self.conn.executescript(AGENT_EXECUTION_DDL)
        self.conn.commit()

    def available(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_thinking_results'"
        ).fetchone() is not None

    def record_attempt(
        self,
        *,
        organization_id: str,
        workspace_id: str | None,
        run_id: str,
        attempt: ThinkingAttempt,
    ) -> None:
        self.conn.execute(
            """INSERT INTO agent_thinking_attempts(
                id,organization_id,workspace_id,run_id,provider_name,model_id,tier,latency_ms,
                ok,error,input_tokens,output_tokens,cost,trace_json,citations_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _new_id("think_attempt"),
                organization_id,
                workspace_id,
                run_id,
                attempt.provider_name,
                attempt.model_id,
                attempt.tier,
                attempt.latency_ms,
                int(attempt.ok),
                attempt.error,
                attempt.input_tokens,
                attempt.output_tokens,
                attempt.cost,
                _json(attempt.trace) if attempt.trace is not None else None,
                _json(list(attempt.citations)) if attempt.citations else None,
                _now(),
            ),
        )
        self.conn.commit()

    def thinking_result(self, organization_id: str, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM agent_thinking_results WHERE organization_id=? AND run_id=?",
            (organization_id, run_id),
        ).fetchone()
        return _row_dict(row)

    def thinking_attempts(self, organization_id: str, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_thinking_attempts WHERE organization_id=? AND run_id=? ORDER BY created_at,id",
            (organization_id, run_id),
        ).fetchall()
        result = []
        for row in rows:
            item = _row_dict(row)
            if item is None:
                continue
            item["trace"] = _decode_json(item.pop("trace_json", None))
            item["citations"] = _decode_json(item.pop("citations_json", None)) or []
            result.append(item)
        return result

    def thinking_read_model(self, organization_id: str, workspace_id: str | None, run_id: str) -> dict[str, Any] | None:
        result = self.thinking_result(organization_id, run_id)
        if result is None or result.get("workspace_id") != workspace_id:
            return None
        result["trace"] = _decode_json(result.pop("trace_json", None))
        result["citations"] = _decode_json(result.pop("citations_json", None)) or []
        result["attempts"] = self.thinking_attempts(organization_id, run_id)
        return result

    def persist_thinking_result(self, run: Mapping[str, Any], result: ThinkingResult, tier: int) -> dict[str, Any]:
        existing = self.thinking_result(str(run["organization_id"]), str(run["run_id"]))
        if existing is not None:
            return existing
        row = {
            "id": _new_id("think"),
            "organization_id": str(run["organization_id"]),
            "workspace_id": run.get("workspace_id"),
            "agent_id": str(run["agent_id"]),
            "run_id": str(run["run_id"]),
            "task_id": run.get("task_id"),
            "model_id": result.model_id,
            "tier": tier,
            "status": result.status,
            "result_json": _json(result.result) if result.result is not None else None,
            "error_json": _json({"message": result.error}) if result.error else None,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost": result.cost,
            "trace_json": _json(result.trace) if result.trace is not None else None,
            "citations_json": _json(list(result.citations)) if result.citations else None,
            "created_at": _now(),
        }
        self.conn.execute(
            """INSERT INTO agent_thinking_results(
                id,organization_id,workspace_id,agent_id,run_id,task_id,model_id,tier,status,
                result_json,error_json,input_tokens,output_tokens,cost,trace_json,citations_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(row.values()),
        )
        self.conn.commit()
        return row

    def action_for_run(self, organization_id: str, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM agent_executor_actions WHERE organization_id=? AND run_id=?",
            (organization_id, run_id),
        ).fetchone()
        return _row_dict(row)

    def ensure_pending_action(self, run: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        existing = self.action_for_run(str(run["organization_id"]), str(run["run_id"]))
        if existing is not None:
            return existing
        row = {
            "id": _new_id("agent_action"),
            "organization_id": str(run["organization_id"]),
            "workspace_id": run.get("workspace_id"),
            "agent_id": str(run["agent_id"]),
            "run_id": str(run["run_id"]),
            "task_id": run.get("task_id"),
            "idempotency_key": idempotency_key,
            "status": "pending",
            "result_json": None,
            "error_json": None,
            "created_at": _now(),
            "completed_at": None,
        }
        self.conn.execute(
            """INSERT INTO agent_executor_actions(
                id,organization_id,workspace_id,agent_id,run_id,task_id,idempotency_key,
                status,result_json,error_json,created_at,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(row.values()),
        )
        self.conn.commit()
        return row

    def finish_action(
        self,
        action_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE agent_executor_actions
               SET status=?,result_json=?,error_json=?,completed_at=?
               WHERE id=? AND status='pending'""",
            (
                status,
                _json(result) if result is not None else None,
                _json(error) if error is not None else None,
                _now(),
                action_id,
            ),
        )
        self.conn.commit()


class ThinkingTask:
    def __init__(
        self,
        providers: ProviderRegistry,
        models: ModelRegistry,
        store: SQLiteAgentExecutionStore,
        *,
        organization_id: str,
        workspace_id: str | None,
        run_id: str,
        requesting_person_id: str | None = None,
    ) -> None:
        self.providers = providers
        self.models = models
        self.store = store
        self.organization_id = organization_id
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.requesting_person_id = requesting_person_id
        self._cancelled = multiprocessing.Event()
        self._current_process: multiprocessing.Process | None = None

    def cancel(self) -> None:
        self._cancelled.set()
        if self._current_process is not None and self._current_process.is_alive():
            self._current_process.terminate()

    def execute(
        self,
        context: Mapping[str, Any],
        *,
        tier: int,
        budget: ThinkingBudget,
        timeout_seconds: float = 30.0,
    ) -> ThinkingResult:
        attempts: list[ThinkingAttempt] = []
        used_tokens = 0
        used_cost = 0.0
        scoped_context = scope_context_packet(
            context,
            organization_id=self.organization_id,
            workspace_id=self.workspace_id,
            person_id=getattr(self, "requesting_person_id", None),
        )
        chain = self.models.fallback_chain(tier)
        if not chain:
            return ThinkingResult("failed", None, (), None, 0, 0, 0.0, "no fallback chain configured")
        for model in chain:
            reserved_cost = _cost(model.max_tokens, model.cost_per_1k_tokens)
            if used_tokens + model.max_tokens > budget.max_tokens or used_cost + reserved_cost > budget.max_cost:
                break
            if self._cancelled.is_set():
                break
            try:
                provider = self.providers.get(model.provider_name)
            except KeyError as exc:
                # A stale model chain must be durable and observable, not turn
                # into an unrecorded worker exception.
                attempt = ThinkingAttempt(
                    provider_name=model.provider_name, model_id=model.id, tier=model.tier,
                    latency_ms=0, ok=False, error=str(exc), input_tokens=0,
                    output_tokens=0, cost=0.0,
                )
                attempts.append(attempt)
                self.store.record_attempt(
                    organization_id=self.organization_id, workspace_id=self.workspace_id,
                    run_id=self.run_id, attempt=attempt,
                )
                continue
            started = time.perf_counter()
            outcome = _call_provider_in_subprocess(provider, scoped_context, timeout_seconds, self._cancelled, self)
            latency_ms = int((time.perf_counter() - started) * 1000)
            input_tokens, output_tokens = _usage_from_outcome(outcome, scoped_context)
            if input_tokens + output_tokens <= 0:
                input_tokens, output_tokens = _estimate_tokens(scoped_context), model.max_tokens
            attempt_tokens = min(model.max_tokens, input_tokens + output_tokens)
            attempt_cost = _cost(attempt_tokens, model.cost_per_1k_tokens)
            used_tokens += attempt_tokens
            used_cost += attempt_cost
            raw_result = outcome.get("result") if isinstance(outcome.get("result"), Mapping) else {}
            metadata = outcome.get("metadata") if isinstance(outcome.get("metadata"), Mapping) else {}
            trace = raw_result.get("trace") or {"provider": metadata.get("provider", model.provider_name), "model": model.id}
            citations = _as_citations(raw_result.get("citations"))
            attempt = ThinkingAttempt(
                provider_name=model.provider_name,
                model_id=model.id,
                tier=model.tier,
                latency_ms=latency_ms,
                ok=outcome["ok"],
                error=outcome.get("error"),
                input_tokens=min(input_tokens, attempt_tokens),
                output_tokens=max(0, attempt_tokens - min(input_tokens, attempt_tokens)),
                cost=attempt_cost,
                trace=trace,
                citations=citations,
            )
            attempts.append(attempt)
            self.store.record_attempt(
                organization_id=self.organization_id,
                workspace_id=self.workspace_id,
                run_id=self.run_id,
                attempt=attempt,
            )
            if outcome["ok"]:
                result = _advisory_result(dict(outcome["result"]))
                return ThinkingResult(
                    "succeeded",
                    result,
                    tuple(attempts),
                    model.id,
                    sum(item.input_tokens for item in attempts),
                    sum(item.output_tokens for item in attempts),
                    sum(item.cost for item in attempts),
                    trace=result.get("trace"),
                    citations=_as_citations(result.get("citations")),
                )
            if outcome.get("error") == "cancelled":
                return ThinkingResult(
                    "cancelled",
                    None,
                    tuple(attempts),
                    None,
                    sum(item.input_tokens for item in attempts),
                    sum(item.output_tokens for item in attempts),
                    sum(item.cost for item in attempts),
                    "cancelled",
                )
        return ThinkingResult(
            "failed",
            None,
            tuple(attempts),
            None,
            sum(item.input_tokens for item in attempts),
            sum(item.output_tokens for item in attempts),
            sum(item.cost for item in attempts),
            "budget exhausted" if attempts or chain else "no model attempted",
        )


class AgentExecutionWorker:
    def __init__(
        self,
        store: SQLiteAgentExecutionStore,
        providers: ProviderRegistry,
        models: ModelRegistry,
        action_executor: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        self.store = store
        self.providers = providers
        self.models = models
        self.action_executor = action_executor

    def execute(
        self,
        run: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        tier: int,
        budget: ThinkingBudget,
        timeout_seconds: float = 30.0,
        crash_after_think: bool = False,
    ) -> dict[str, Any]:
        organization_id = str(run["organization_id"])
        run_id = str(run["run_id"])
        thinking_row = self.store.thinking_result(organization_id, run_id)
        if thinking_row is None:
            thinking = ThinkingTask(
                self.providers,
                self.models,
                self.store,
                organization_id=organization_id,
                workspace_id=run.get("workspace_id"),
                run_id=run_id,
            ).execute(context, tier=tier, budget=budget, timeout_seconds=timeout_seconds)
            thinking_row = self.store.persist_thinking_result(run, thinking, tier)
        if crash_after_think:
            raise RuntimeError("simulated crash after durable think")
        if thinking_row["status"] != "succeeded":
            return {"status": "think_failed", "thinking": thinking_row}
        plan = json.loads(thinking_row["result_json"] or "{}")
        # A model may suggest actions, but suggestions never cross this
        # boundary into execution.  The existing agent.run approval path is
        # the only place that may execute a descriptor.
        if plan.get("action_descriptors") or plan.get("approval_required"):
            return {"status": "approval_required", "thinking": thinking_row, "action_descriptors": plan.get("action_descriptors", [])}
        idempotency_key = str(run.get("idempotency_key") or f"agent-executor:{organization_id}:{run_id}")
        action_row = self.store.ensure_pending_action(run, idempotency_key)
        if action_row["status"] == "done":
            return {
                "status": "done",
                "thinking": thinking_row,
                "action": action_row,
                "result": json.loads(action_row["result_json"] or "{}"),
            }
        if action_row["status"] == "failed":
            return {"status": "action_failed", "thinking": thinking_row, "action": action_row}
        try:
            action_result = dict(self.action_executor(plan, run))
        except Exception as exc:
            self.store.finish_action(
                action_row["id"],
                status="failed",
                error={"type": exc.__class__.__name__, "message": str(exc)},
            )
            raise
        self.store.finish_action(action_row["id"], status="done", result=action_result)
        return {
            "status": "done",
            "thinking": thinking_row,
            "action": self.store.action_for_run(organization_id, run_id),
            "result": action_result,
        }


class AgentThinkJobHandler:
    """Durable handler for ``agent.think``.

    This handler intentionally has no action executor.  A successful result is
    an advisory plan with reviewable action descriptors; an approved action is
    submitted separately through the existing ``agent.run`` path.
    """

    def __init__(self, store: SQLiteAgentExecutionStore, providers: ProviderRegistry, models: ModelRegistry) -> None:
        self.store, self.providers, self.models = store, providers, models

    def execute(self, job: Mapping[str, Any]) -> dict[str, Any]:
        return self(job)

    def __call__(self, job: Mapping[str, Any]) -> dict[str, Any]:
        payload = job.get("payload") or {}
        run_id = str(payload.get("run_id") or job.get("id") or "")
        agent_id = str(payload.get("agent_id") or "")
        if not run_id or not agent_id:
            raise ValueError("agent.think requires run_id and agent_id")
        run = {
            "organization_id": str(job["organization_id"]),
            "workspace_id": job.get("workspace_id"),
            "agent_id": agent_id,
            "run_id": run_id,
            "task_id": payload.get("task_id"),
        }
        self.store.initialize()
        existing = self.store.thinking_read_model(run["organization_id"], run["workspace_id"], run_id)
        if existing is not None:
            return {"status": "succeeded" if existing["status"] == "succeeded" else "think_failed", "thinking": existing}
        person_id = str(payload.get("person_id") or "") or None
        if person_id is None and job.get("principal_id"):
            try:
                principal = self.store.conn.execute(
                    "SELECT person_id FROM auth_principals WHERE id=? AND organization_id=?",
                    (str(job["principal_id"]), run["organization_id"]),
                ).fetchone()
                person_id = str(principal[0]) if principal and principal[0] else None
            except sqlite3.OperationalError:
                # Standalone execution stores do not need the auth schema;
                # workspace/org scoping still applies in that environment.
                person_id = None
        task = ThinkingTask(
            self.providers, self.models, self.store,
            organization_id=run["organization_id"], workspace_id=run["workspace_id"],
            run_id=run_id, requesting_person_id=person_id,
        )
        result = task.execute(
            payload.get("context") or {}, tier=int(payload.get("tier", 1)),
            budget=ThinkingBudget(int(payload.get("max_tokens", 4096)), float(payload.get("max_cost", 1.0))),
            timeout_seconds=float(payload.get("timeout_seconds", 30.0)),
        )
        row = self.store.persist_thinking_result(run, result, int(payload.get("tier", 1)))
        return {"status": "succeeded" if result.status == "succeeded" else "think_failed", "thinking": self.store.thinking_read_model(run["organization_id"], run["workspace_id"], run_id) or row}


def scope_context_packet(
    context: Mapping[str, Any], *, organization_id: str, workspace_id: str | None, person_id: str | None,
) -> dict[str, Any]:
    """Return only packet records belonging to the requesting ACL scope.

    Scope-bearing records with a conflicting organization/workspace/person are
    dropped recursively.  This keeps provider input useful while ensuring a
    mixed packet cannot disclose another workspace.
    """
    expected = {"organization_id": organization_id, "workspace_id": workspace_id, "person_id": person_id}

    def visit(value: Any, root: bool = False) -> Any:
        if isinstance(value, Mapping):
            for key, wanted in expected.items():
                if key not in value:
                    continue
                actual = value[key]
                if wanted is None:
                    # An unscoped request may use organization-level context,
                    # but must never receive a workspace/person-specific row.
                    if actual is not None and key in {"workspace_id", "person_id"}:
                        return {} if root else None
                    continue
                if actual is not None and str(actual) != str(wanted):
                    return {} if root else None
            output: dict[str, Any] = {}
            for key, item in value.items():
                scoped = visit(item)
                if scoped is not None:
                    output[str(key)] = scoped
            return output
        if isinstance(value, (list, tuple)):
            return [scoped for item in value if (scoped := visit(item)) is not None]
        return value

    scoped = visit(dict(context), root=True)
    return scoped if isinstance(scoped, dict) else {}


def _advisory_result(result: dict[str, Any]) -> dict[str, Any]:
    descriptors = result.get("action_descriptors")
    if not isinstance(descriptors, list):
        descriptors = result.get("allowed_actions") if isinstance(result.get("allowed_actions"), list) else []
    result["action_descriptors"] = [item for item in descriptors if isinstance(item, Mapping)]
    result["advisory_only"] = True
    result["approval_required"] = bool(result["action_descriptors"])
    return result


def _as_citations(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,) if value is not None else ()


def _decode_json(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def enqueue_agent_think_job(
    jobs: Any, *, organization_id: str, workspace_id: str | None, principal_id: str,
    agent_id: str, run_id: str, context: Mapping[str, Any], tier: int,
    budget: ThinkingBudget, task_id: str | None = None, priority: int = 0,
    idempotency_key: str | None = None, person_id: str | None = None,
) -> dict[str, Any]:
    """Enqueue a scoped durable think job using the normal job repository."""
    if not agent_id.strip() or not run_id.strip():
        raise ValueError("agent_id and run_id are required")
    providers = getattr(jobs, "agent_think_providers", None)
    models = getattr(jobs, "agent_think_models", None)
    if providers is not None and models is not None and not agent_think_available(providers, models, tier):
        raise AgentThinkUnavailable("agent thinking provider is not configured")
    packet = scope_context_packet(
        context, organization_id=organization_id, workspace_id=workspace_id, person_id=person_id,
    )
    return jobs.enqueue_job(
        organization_id, workspace_id, principal_id, AGENT_THINK_JOB_TYPE,
        {
            "agent_id": agent_id, "run_id": run_id, "task_id": task_id,
            "person_id": person_id,
            "context": packet, "tier": int(tier), "max_tokens": int(budget.max_tokens),
            "max_cost": float(budget.max_cost),
        },
        priority=priority,
        idempotency_key=idempotency_key or f"agent-think:{organization_id}:{run_id}",
    )


def thinking_for_job(
    store: SQLiteAgentExecutionStore, job: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build a read-only result model only for an in-scope agent.think job."""
    if str(job.get("type")) != AGENT_THINK_JOB_TYPE:
        return None
    payload = job.get("payload") or {}
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        return None
    return store.thinking_read_model(str(job["organization_id"]), job.get("workspace_id"), run_id)


def execute_agent_think_job(
    job: Mapping[str, Any], *, store: SQLiteAgentExecutionStore,
    providers: ProviderRegistry, models: ModelRegistry,
) -> dict[str, Any]:
    """Worker-dispatch seam kept independent from the central job registry."""
    return AgentThinkJobHandler(store, providers, models).execute(job)


def _call_provider_worker(provider: Any, context: Mapping[str, Any], output: multiprocessing.Queue) -> None:
    try:
        result, metadata = invoke_reasoning_provider(provider, context)
        output.put({"ok": True, "result": dict(result), "metadata": metadata})
    except StrategicReasoningProviderError as exc:
        output.put({"ok": False, "error": str(exc)})
    except BaseException as exc:
        output.put({"ok": False, "error": f"provider_process_failed:{type(exc).__name__}"})


def _call_provider_in_subprocess(
    provider: Any,
    context: Mapping[str, Any],
    timeout_seconds: float,
    cancel_event: multiprocessing.Event,
    owner: ThinkingTask,
) -> dict[str, Any]:
    output: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)
    process = multiprocessing.Process(target=_call_provider_worker, args=(provider, dict(context), output))
    owner._current_process = process
    process.start()
    deadline = time.monotonic() + max(0.01, timeout_seconds)
    try:
        while process.is_alive():
            if cancel_event.is_set():
                process.terminate()
                process.join(1)
                return {"ok": False, "error": "cancelled"}
            if time.monotonic() >= deadline:
                process.terminate()
                process.join(1)
                return {"ok": False, "error": "timeout"}
            process.join(0.02)
        process.join()
        if cancel_event.is_set():
            return {"ok": False, "error": "cancelled"}
        try:
            return output.get_nowait()
        except queue.Empty:
            if process.exitcode == 0:
                return {"ok": False, "error": "provider_returned_no_result"}
            return {"ok": False, "error": f"provider_process_exit:{process.exitcode}"}
    finally:
        if process.is_alive():
            process.kill()
            process.join(1)
        owner._current_process = None
        output.close()


def _usage_from_outcome(outcome: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[int, int]:
    if not outcome.get("ok"):
        return (_estimate_tokens(context), 0)
    result = outcome.get("result")
    usage = result.get("usage") if isinstance(result, Mapping) else None
    if isinstance(usage, Mapping):
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or usage.get("tokens") or 0)
        if total and not input_tokens and not output_tokens:
            input_tokens = _estimate_tokens(context)
            output_tokens = max(0, total - input_tokens)
        return input_tokens, output_tokens
    return _estimate_tokens(context), _estimate_tokens(result or {})


def _estimate_tokens(value: Any) -> int:
    return max(1, len(_json(value)) // 4)


def _cost(tokens: int, cost_per_1k_tokens: float) -> float:
    return (tokens / 1000.0) * cost_per_1k_tokens


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _row_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(row)
