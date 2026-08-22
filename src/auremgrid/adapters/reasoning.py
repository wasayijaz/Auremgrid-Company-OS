from __future__ import annotations

"""Optional provider-neutral strategic reasoning boundary.

Providers receive a prepared, ACL-scoped context and return JSON-compatible
data.  They never receive a store/CompanyOS handle and therefore cannot
execute canonical writes through this boundary.
"""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from typing import Any, Callable, Mapping, Protocol


class StrategicReasoningProvider(Protocol):
    """A provider that proposes structured reasoning, never actions."""

    name: str
    model: str
    version: str

    def deliberate(self, context: Mapping[str, Any]) -> Mapping[str, Any] | str: ...


class CallableStrategicReasoningProvider:
    """Small adapter for an injected callable or SDK client method."""

    def __init__(
        self,
        callback: Callable[[Mapping[str, Any]], Mapping[str, Any] | str],
        *,
        name: str = "injected",
        model: str = "configured",
        version: str = "1",
    ) -> None:
        if not callable(callback):
            raise TypeError("strategic reasoning callback must be callable")
        self._callback = callback
        self.name = name.strip() or "injected"
        self.model = model.strip() or "configured"
        self.version = version.strip() or "1"

    def deliberate(self, context: Mapping[str, Any]) -> Mapping[str, Any] | str:
        return self._callback(context)


class HttpStrategicReasoningProvider:
    """Minimal JSON HTTP adapter for an explicitly configured model endpoint.

    It is intentionally dependency-free and sends only the prepared context.
    Credentials are read at call time from an environment variable and never
    appear in provider metadata or exception text.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        model: str = "configured",
        version: str = "1",
        api_key_env: str = "AUREMGRID_REASONING_API_KEY",
        timeout: float = 20.0,
    ) -> None:
        endpoint = endpoint.strip()
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("reasoning endpoint must be an http(s) URL")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("non-loopback reasoning endpoints must use HTTPS")
        self.endpoint = endpoint
        self.name = "json_http"
        self.model = model.strip() or "configured"
        self.version = version.strip() or "1"
        self.api_key_env = api_key_env.strip() or "AUREMGRID_REASONING_API_KEY"
        self.timeout = min(60.0, max(1.0, float(timeout)))

    def deliberate(self, context: Mapping[str, Any]) -> Mapping[str, Any] | str:
        body = json.dumps({"context": context}, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        api_key = os.environ.get(self.api_key_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with _REASONING_OPENER.open(request, timeout=self.timeout) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > _MAX_REASONING_RESPONSE_BYTES:
                    raise StrategicReasoningProviderError("response_too_large")
                body = response.read(_MAX_REASONING_RESPONSE_BYTES + 1)
                if len(body) > _MAX_REASONING_RESPONSE_BYTES:
                    raise StrategicReasoningProviderError("response_too_large")
                payload = json.loads(body.decode("utf-8"))
        except HTTPError as exc:
            raise StrategicReasoningProviderError(f"http_status_{exc.code}") from exc
        except (URLError, TimeoutError, ValueError, UnicodeError) as exc:
            raise StrategicReasoningProviderError(f"http_request_failed:{type(exc).__name__}") from exc
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            return payload["result"]
        return payload


_MAX_REASONING_RESPONSE_BYTES = 512 * 1024


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        old = urlparse(req.full_url)
        new = urlparse(newurl)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise StrategicReasoningProviderError("redirect_not_allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_REASONING_OPENER = build_opener(_SameOriginRedirectHandler)


def strategic_reasoning_provider_from_config(
    *, environ: Mapping[str, str] | None = None
) -> StrategicReasoningProvider | None:
    """Build the optional HTTP provider; absent endpoint means offline mode."""
    env = environ if environ is not None else os.environ
    endpoint = env.get("AUREMGRID_REASONING_ENDPOINT")
    if not endpoint:
        return None
    timeout = float(env.get("AUREMGRID_REASONING_TIMEOUT", "20"))
    return HttpStrategicReasoningProvider(
        endpoint,
        model=env.get("AUREMGRID_REASONING_MODEL", "configured"),
        version=env.get("AUREMGRID_REASONING_VERSION", "1"),
        api_key_env=env.get("AUREMGRID_REASONING_API_KEY_ENV", "AUREMGRID_REASONING_API_KEY"),
        timeout=timeout,
    )


class StrategicReasoningProviderError(RuntimeError):
    """Raised when an optional provider cannot produce a response."""


def invoke_reasoning_provider(
    provider: Any, context: Mapping[str, Any]
) -> tuple[Mapping[str, Any] | str, dict[str, str]]:
    """Invoke the provider's explicit reasoning method without guessing I/O.

    ``reason`` and ``generate`` are accepted for simple injected fakes, while
    production adapters should implement ``deliberate``.
    """
    method = next(
        (getattr(provider, name, None) for name in ("deliberate", "reason", "generate")
         if callable(getattr(provider, name, None))),
        None,
    )
    if method is None:
        raise StrategicReasoningProviderError("provider has no deliberate method")
    try:
        result = method(context)
    except Exception as exc:  # provider failures are an honest fallback
        raise StrategicReasoningProviderError(f"provider_call_failed:{type(exc).__name__}") from exc
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError) as exc:
            raise StrategicReasoningProviderError("provider_returned_invalid_json") from exc
    if not isinstance(result, Mapping):
        raise StrategicReasoningProviderError("provider_returned_non_object")
    metadata = {
        "provider": str(getattr(provider, "name", type(provider).__name__))[:120],
        "model": str(getattr(provider, "model", "configured"))[:120],
        "version": str(getattr(provider, "version", "1"))[:80],
    }
    return result, metadata
