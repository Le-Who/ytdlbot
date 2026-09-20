"""Small resolver-only HTTP boundary shared by external media adapters."""

from __future__ import annotations

import asyncio
import json as json_module
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from curl_cffi.requests import AsyncSession

from ..registry import FailureKind, ProviderError

_SENSITIVE_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization", "x-api-key"}
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CHALLENGE_MARKERS = (
    "cloudflare challenge",
    "captcha",
    "cf-chl-",
    "just a moment...",
    "verify you are human",
)

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ProviderEndpoint:
    """Configuration scoped to exactly one resolver origin."""

    origin: str
    api_key: str | None
    capabilities: frozenset[str]
    enabled: bool
    min_interval: float = 0.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("provider endpoint must be an HTTP(S) origin")
        if not math.isfinite(self.min_interval) or self.min_interval < 0:
            raise ValueError(
                "provider endpoint min_interval must be finite and non-negative"
            )
        object.__setattr__(self, "origin", self.origin.rstrip("/"))

    def supports(self, platform: str) -> bool:
        return self.enabled and (
            "*" in self.capabilities or platform in self.capabilities
        )


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    json_data: object | None = None
    text: str = ""


class HttpTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: object | None = None,
        data: object | None = None,
        timeout: float,
    ) -> HttpResponse: ...


class OriginPacing(Protocol):
    async def wait(self, origin: str, min_interval: float) -> None: ...


@dataclass(slots=True)
class _OriginPaceState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_allowed: float = 0.0


class OriginPacer:
    """Enforce configurable spacing independently for each exact origin."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._states: dict[str, _OriginPaceState] = {}

    async def wait(self, origin: str, min_interval: float) -> None:
        if min_interval <= 0:
            return
        state = self._states.setdefault(origin, _OriginPaceState())
        async with state.lock:
            delay = state.next_allowed - self._clock()
            if delay > 0:
                await self._sleep(delay)
            state.next_allowed = self._clock() + min_interval


class CurlProviderTransport:
    """curl_cffi transport with redirects left to the credential-aware wrapper."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: object | None = None,
        data: object | None = None,
        timeout: float,
    ) -> HttpResponse:
        async with AsyncSession() as session:
            response = await session.request(
                method,
                url,
                headers=headers,
                json=json,
                data=data,
                timeout=timeout,
                allow_redirects=False,
                impersonate="chrome",
            )
        try:
            payload: object | None = response.json()
        except (ValueError, json_module.JSONDecodeError):
            payload = None
        return HttpResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            json_data=payload,
            text=response.text,
        )


def endpoint_headers(endpoint: ProviderEndpoint) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/html;q=0.8",
        "User-Agent": "ytdlbot/2.0",
    }
    if endpoint.api_key:
        headers["Authorization"] = f"Api-Key {endpoint.api_key}"
    return headers


async def request_response(
    transport: HttpTransport,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json: object | None = None,
    data: object | None = None,
    timeout: float = 8,
    max_redirects: int = 3,
) -> HttpResponse:
    """Request while stripping sensitive headers whenever the origin changes."""
    current_url = url
    current_method = method
    current_headers = dict(headers or {})
    current_json = json
    current_data = data
    for redirect_count in range(max_redirects + 1):
        try:
            response = await transport.request(
                current_method,
                current_url,
                headers=current_headers,
                json=current_json,
                data=current_data,
                timeout=timeout,
            )
        except ProviderError:
            raise
        except (TimeoutError, OSError) as error:
            raise ProviderError(FailureKind.TRANSIENT, str(error)) from error
        except Exception as error:
            raise ProviderError(FailureKind.TRANSIENT, str(error)) from error

        if response.status_code not in _REDIRECT_STATUSES:
            return response
        location = _header(response.headers, "location")
        if not location or redirect_count >= max_redirects:
            raise ProviderError(FailureKind.TRANSIENT, "invalid redirect response")
        next_url = urljoin(current_url, location)
        origin_changed = _origin(next_url) != _origin(current_url)
        if (
            origin_changed
            and response.status_code in {307, 308}
            and (current_json is not None or current_data is not None)
        ):
            raise ProviderError(
                FailureKind.CONFIG,
                "refusing cross-origin body-preserving redirect",
            )
        if origin_changed:
            current_headers = {
                key: value
                for key, value in current_headers.items()
                if key.lower() not in _SENSITIVE_HEADERS
            }
        if response.status_code in {301, 302, 303}:
            if current_method.upper() != "GET":
                current_method = "GET"
            current_json = None
            current_data = None
        current_url = next_url
    raise ProviderError(FailureKind.TRANSIENT, "too many redirects")


async def request_json(
    transport: HttpTransport,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json: object | None = None,
    data: object | None = None,
    timeout: float = 8,
) -> tuple[dict[str, Any], HttpResponse]:
    response = await request_response(
        transport,
        method,
        url,
        headers=headers,
        json=json,
        data=data,
        timeout=timeout,
    )
    _raise_for_response(response, authenticated=_has_credentials(headers))
    if not isinstance(response.json_data, dict):
        if is_challenge(response.text):
            raise ProviderError(FailureKind.TRANSIENT, "upstream challenge response")
        raise ProviderError(FailureKind.TRANSIENT, "upstream returned non-JSON content")
    return response.json_data, response


async def request_html(
    transport: HttpTransport,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: object | None = None,
    timeout: float = 8,
) -> HttpResponse:
    response = await request_response(
        transport,
        method,
        url,
        headers=headers,
        data=data,
        timeout=timeout,
    )
    _raise_for_response(response, authenticated=_has_credentials(headers))
    if is_challenge(response.text):
        raise ProviderError(FailureKind.TRANSIENT, "upstream challenge response")
    content_type = (_header(response.headers, "content-type") or "").lower()
    if content_type and "html" not in content_type:
        raise ProviderError(FailureKind.TRANSIENT, "upstream returned non-HTML content")
    return response


async def probe_candidate(
    transport: HttpTransport,
    url: str,
    *,
    wall_clock: Any = time.time,
) -> None:
    """Perform only a bounded Range probe; never forward resolver credentials."""
    ensure_not_expired(url, wall_clock=wall_clock)
    if isinstance(transport, CurlProviderTransport):
        # Production probing shares the same DNS pinning, redirect checks, byte
        # bounds, and credential stripping as materialization. Resolver fixture
        # transports retain the small protocol below for deterministic tests.
        import tempfile

        from ..transport import MediaTransport

        await MediaTransport(output_dir=tempfile.gettempdir()).probe(url)
        return
    response = await request_response(
        transport,
        "GET",
        url,
        headers={"Accept": "*/*", "Range": "bytes=0-0", "User-Agent": "ytdlbot/2.0"},
        timeout=3,
    )
    if response.status_code == 429:
        raise ProviderError(
            FailureKind.TRANSIENT,
            "candidate rate limited",
            retry_after=parse_retry_after(response.headers),
        )
    if response.status_code in {401, 403, 404, 410}:
        raise ProviderError(FailureKind.TRANSIENT, "candidate URL expired or denied")
    if response.status_code not in {200, 206}:
        raise ProviderError(
            FailureKind.TRANSIENT,
            f"candidate probe returned HTTP {response.status_code}",
        )
    if is_challenge(response.text):
        raise ProviderError(FailureKind.TRANSIENT, "candidate returned challenge HTML")
    content_type = (_header(response.headers, "content-type") or "").lower()
    if "text/html" in content_type:
        raise ProviderError(FailureKind.TRANSIENT, "candidate returned HTML")


def ensure_not_expired(
    url: str, *, wall_clock: Callable[[], float] = time.time
) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderError(FailureKind.PERMANENT, "candidate URL is not HTTP(S)")
    from urllib.parse import parse_qs

    query = parse_qs(parsed.query)
    for key in ("expire", "expires", "expiry"):
        values = query.get(key)
        if not values:
            continue
        try:
            expiry = float(values[0])
        except (TypeError, ValueError):
            continue
        if expiry <= float(wall_clock()):
            raise ProviderError(FailureKind.TRANSIENT, "candidate URL is expired")


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    value = _header(headers, "retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return max(0.0, parsed.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def is_challenge(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def same_origin(first: str, second: str) -> bool:
    return _origin(first) == _origin(second)


def _raise_for_response(response: HttpResponse, *, authenticated: bool) -> None:
    if response.status_code == 429:
        raise ProviderError(
            FailureKind.TRANSIENT,
            "upstream rate limited",
            retry_after=parse_retry_after(response.headers),
        )
    if response.status_code in {401, 403}:
        kind = FailureKind.AUTH if authenticated else FailureKind.TRANSIENT
        raise ProviderError(kind, f"upstream returned HTTP {response.status_code}")
    if response.status_code >= 500:
        raise ProviderError(
            FailureKind.TRANSIENT,
            f"upstream returned HTTP {response.status_code}",
        )
    if response.status_code >= 400:
        raise ProviderError(
            FailureKind.PERMANENT,
            f"upstream returned HTTP {response.status_code}",
        )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def _has_credentials(headers: Mapping[str, str] | None) -> bool:
    return any(key.lower() in _SENSITIVE_HEADERS for key in (headers or {}))
