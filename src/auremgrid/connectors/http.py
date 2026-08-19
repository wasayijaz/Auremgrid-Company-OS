"""Small urllib transport shared by credential-backed connectors."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> object:
        return json.loads(self.body.decode("utf-8"))


class ConnectorTransportError(RuntimeError):
    """Structured transport/API failure; callers decide when to retry."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


Requester = Callable[[str, str, Mapping[str, str], bytes | None], HttpResponse]

_CREDENTIAL_KEY = re.compile(r"(?:token|secret|password|passwd|authorization|api[_-]?key|access[_-]?token)", re.I)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I)
_CREDENTIAL_PAIR = re.compile(r"((?:token|secret|password|api[_-]?key|access[_-]?token)\s*[=:]\s*)[^\s,;&]+", re.I)


def sanitize_content(value: Any, known_secrets: Iterable[str] = ()) -> Any:
    """Redact credentials from connector payloads before they become evidence."""

    secrets = tuple(secret for secret in known_secrets if isinstance(secret, str) and secret)
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if _CREDENTIAL_KEY.search(str(key)) else sanitize_content(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_content(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_content(item, secrets) for item in value)
    if isinstance(value, str):
        result = value
        for secret in secrets:
            result = result.replace(secret, "[REDACTED]")
        result = _BEARER.sub(r"\1[REDACTED]", result)
        return _CREDENTIAL_PAIR.sub(r"\1[REDACTED]", result)
    return value


class HttpTransport:
    def __init__(self, requester: Requester | None = None, timeout: float = 20.0) -> None:
        self.requester = requester
        self.timeout = timeout

    def request(self, method: str, url: str, headers: Mapping[str, str] | None = None, body: bytes | None = None) -> HttpResponse:
        request_headers = dict(headers or {})
        if self.requester is not None:
            response = self.requester(method, url, request_headers, body)
        else:
            request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as handle:
                    response = HttpResponse(handle.status, dict(handle.headers.items()), handle.read())
            except urllib.error.HTTPError as exc:
                response = HttpResponse(exc.code, dict(exc.headers.items()), exc.read())
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ConnectorTransportError("connector network failure", retryable=True) from exc
        if response.status < 200 or response.status >= 300:
            raise ConnectorTransportError(
                "connector HTTP request failed",
                status=response.status,
                retryable=response.status in {408, 425, 429} or response.status >= 500,
                retry_after=_retry_after(response.headers),
            )
        return response


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is not None:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            pass
    reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    if reset is not None:
        try:
            return max(0.0, float(reset) - time.time())
        except (TypeError, ValueError):
            pass
    return None
