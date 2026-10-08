"""Process-owned proxy credentials and bounded, platform-scoped route retries."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar
from urllib.parse import urlsplit, urlunsplit

from .registry import FailureKind, ProviderError

T = TypeVar("T")
DEFAULT_PROXY_PLATFORMS = frozenset(
    {
        "youtube",
        "instagram",
        "facebook",
        "tiktok",
        "pinterest",
        "vk",
        "rutube",
        "twitter",
    }
)
logger = logging.getLogger(__name__)


class MediaProxyPool:
    """Opaque keys bind extracted URLs to a route without copying credentials."""

    def __init__(
        self,
        urls: Sequence[str] = (),
        *,
        platforms: frozenset[str] = DEFAULT_PROXY_PLATFORMS,
        attempt_timeout: float = 20.0,
        direct_timeout: float = 3.0,
        cooldown: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if any(
            not math.isfinite(v) or v <= 0
            for v in (attempt_timeout, direct_timeout, cooldown)
        ):
            raise ValueError("proxy timeouts must be finite and positive")
        self._urls: dict[str, str] = {}
        for index, url in enumerate(urls, 1):
            try:
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in {"socks5", "socks5h"}
                    or not parsed.hostname
                    or parsed.port is None
                    or not 1 <= parsed.port <= 65535
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError(
                    "MEDIA_PROXY_URLS must contain valid SOCKS5 URLs with explicit ports"
                ) from None
            # Local DNS and CurlOpt.RESOLVE must also apply through the SOCKS route.
            self._urls[f"proxy-{index}"] = urlunsplit(
                ("socks5", parsed.netloc, "", "", "")
            )
        self.platforms = (
            platforms | {"x", "twitter"} if platforms & {"x", "twitter"} else platforms
        )
        self.attempt_timeout = attempt_timeout
        self.direct_timeout = direct_timeout
        self.cooldown = cooldown
        self.clock = clock
        self._failed_until: dict[tuple[str, str | None], float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._urls)

    def configured(self, platform: str) -> bool:
        return self.enabled and platform in self.platforms

    @property
    def extraction_timeout(self) -> float:
        """Cover direct fallback, every configured proxy and process cleanup."""
        return self.direct_timeout + len(self._urls) * self.attempt_timeout + 1.0

    def url(self, key: str) -> str:
        try:
            return self._urls[key]
        except KeyError:
            raise ValueError("unknown media proxy route") from None

    def routes(self, platform: str) -> tuple[str | None, ...]:
        if not self.configured(platform):
            return (None,)
        keys: tuple[str | None, ...] = tuple(self._urls)
        if platform != "youtube":
            keys = (None, *keys)
        now = self.clock()
        healthy = tuple(
            key for key in keys if self._failed_until.get((platform, key), 0) <= now
        )
        # A fully cooled pool still makes one recovery attempt, without repeated
        # probes of every known bad route on each incoming request.
        return healthy or (
            min(keys, key=lambda key: self._failed_until[(platform, key)]),
        )

    def failed(self, platform: str, key: str | None) -> None:
        if self.configured(platform):
            self._failed_until[(platform, key)] = self.clock() + self.cooldown

    def succeeded(self, platform: str, key: str | None) -> None:
        self._failed_until.pop((platform, key), None)

    async def extract(
        self,
        platform: str,
        operation: Callable[[str | None], Awaitable[T]],
        *,
        deadline: float | None = None,
        auth_scope: str = "public",
    ) -> tuple[T, str | None]:
        if not self.configured(platform):
            return await operation(None), None
        last_error: ProviderError | None = None
        for key in self.routes(platform):
            started = self.clock()
            budget = self.attempt_timeout if key else self.direct_timeout
            if deadline is not None:
                budget = min(budget, deadline - self.clock())
            if budget <= 0:
                raise ProviderError(
                    FailureKind.TRANSIENT, "media extraction deadline exceeded"
                )
            try:
                result = await asyncio.wait_for(
                    operation(self.url(key) if key else None), timeout=budget
                )
            except TimeoutError:
                last_error = ProviderError(
                    FailureKind.TRANSIENT, "media route extraction timed out"
                )
            except ProviderError as error:
                if error.kind not in {FailureKind.AUTH, FailureKind.TRANSIENT}:
                    raise
                last_error = error
            else:
                self.succeeded(platform, key)
                logger.info(
                    "media route resolved: platform=%s route=%s",
                    platform,
                    key or "direct",
                    extra={"duration_ms": round((self.clock() - started) * 1000, 3)},
                )
                return result, key
            self.failed(platform, key)
            logger.warning(
                "media route failed: platform=%s route=%s kind=%s",
                platform,
                key or "direct",
                last_error.kind.value,
                extra={
                    "op": "media-route-failed",
                    "duration_ms": round((self.clock() - started) * 1000, 3),
                    "error": str(last_error),
                    "metrics": {"timeout_seconds": budget},
                },
            )
        assert last_error is not None
        if auth_scope == "public" and last_error.kind is FailureKind.AUTH:
            raise ProviderError(FailureKind.TRANSIENT, str(last_error)) from last_error
        raise last_error
