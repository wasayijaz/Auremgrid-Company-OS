"""Structured logging, in-memory metrics, and detailed health endpoint."""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            payload["error"] = str(record.exc_info[1])
        return json.dumps(payload, separators=(",", ":"))


def setup_logging(level: int = logging.INFO, correlation_id: str | None = None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger("auremgrid")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    if correlation_id:
        root.info("correlation_id=%s", correlation_id)


@dataclass
class _Counter:
    value: int = 0


class Metrics:
    """In-memory counters and a simple request-latency histogram."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, _Counter] = {}
        self._latency_buckets: dict[str, dict[str, int]] = {}
        self._latency_bounds = [10, 50, 100, 250, 500, 1000, 2500, 5000, 10000]

    def inc(self, name: str, delta: int = 1) -> None:
        with self._lock:
            c = self._counters.get(name)
            if c is None:
                c = _Counter(); self._counters[name] = c
            c.value += delta

    def record_latency(self, name: str, duration_ms: float) -> None:
        with self._lock:
            buckets = self._latency_buckets.get(name)
            if buckets is None:
                buckets = {str(b): 0 for b in self._latency_bounds}
                buckets["over"] = 0
                self._latency_buckets[name] = buckets
            for bound in self._latency_bounds:
                if duration_ms <= bound:
                    buckets[str(bound)] += 1
                    return
            buckets["over"] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = {k: v.value for k, v in self._counters.items()}
            latency = dict(self._latency_buckets)
        return {"counters": counters, "latency_ms": latency}


_global_metrics = Metrics()


def get_metrics() -> Metrics:
    return _global_metrics


def generate_correlation_id() -> str:
    return uuid.uuid4().hex[:16]
