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
import threading
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
    # Per-run output budgets.  These are hard acceptance caps for specialist
    # payloads; exceeding one degrades the run and records a safety event.
    max_tokens: int = 50000
    max_cost_amount: float = 5.0
    task_class: str = "reasoning"
