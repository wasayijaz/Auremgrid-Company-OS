from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _confidence(score: float) -> dict[str, Any]:
    score = round(max(0.0, min(1.0, float(score))), 3)
    label = "high" if score >= 0.8 else "medium" if score >= 0.55 else "low"
    return {"label": label, "score": score}


def _parse_time(value: Any) -> datetime | None:
    """Parse a canonical timestamp without letting malformed rows break reads."""
    if value in (None, ""):
        return None
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _tokens(value: Any) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]{3,}", str(value or "").lower())
            if token not in {"the", "and", "for", "with", "from", "that", "this", "into", "work"}}
