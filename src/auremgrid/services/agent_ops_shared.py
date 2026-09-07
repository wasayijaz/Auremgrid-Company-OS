"""Shared constants and helpers for AgentOperations services."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

MAX_AGENT_DELEGATION_DEPTH = 3


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _action_operator_next_step(status: str) -> str:
    if status == "succeeded":
        return "No action needed; identical replays return the recorded local result."
    if status == "running":
        return "Wait for the active fenced execution to finish before retrying."
    if status == "failed":
        return "Review the recorded error and create a new approved task or idempotency key before retrying."
    return "Review execution status before taking another action."

