"""SSRF-safe, bounded streaming materialization for provider candidates."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import socket
import time
import uuid
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession, Response

from app.core.resource_budget import DiskBudget, DiskReservation

from .models import MediaCandidate, MediaRequest, MediaSource, RefreshDescriptor

logger = logging.getLogger("app.services.media.transport")

DECIMAL_MEDIA_LIMIT = 2_000_000_000
SMALL_PARALLEL_LIMIT = 20_000_000
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_CROSS_ORIGIN_HEADER_ALLOWLIST = frozenset(
    {"accept", "accept-encoding", "range", "user-agent"}
)


class MaterializationError(RuntimeError):
    """A candidate could not be safely materialized."""


class UnsafeMediaURL(MaterializationError):
    """A URL or one of its resolved addresses is not safe for remote fetching."""


class CredentialRedirectError(MaterializationError):
    """A body-preserving redirect would cross an origin boundary."""


class MediaSizeExceeded(MaterializationError):
    """Declared or streamed bytes exceed the configured decimal byte limit."""


class TransferTimeout(MaterializationError):
    """The source failed the first-byte or progress timeout."""


class DownloadFailed(MaterializationError):
    """The remote response could not produce a media file."""


Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]


@dataclass(frozen=True, slots=True)
class ResolvedURL:
    url: str
    host: str
    port: int
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]


class URLPolicy:
    """Resolve and reject every non-global address before opening a socket."""

    def __init__(self, *, resolver: Resolver | None = None) -> None:
        self._resolver = resolver or _system_resolver

    async def resolve(self, url: str) -> ResolvedURL:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise UnsafeMediaURL("malformed media URL") from error
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise UnsafeMediaURL("media URL must use HTTP(S)")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeMediaURL("media URL must not contain credentials")
        host = parsed.hostname.rstrip(".").lower()
        effective_port = port or (443 if parsed.scheme.lower() == "https" else 80)
        if effective_port < 1 or effective_port > 65535:
            raise UnsafeMediaURL("invalid media URL port")
        try:
            raw_addresses = await self._resolver(host, effective_port)
            addresses = tuple(ipaddress.ip_address(value) for value in raw_addresses)
        except (OSError, ValueError) as error:
            raise UnsafeMediaURL("media host resolution failed") from error
        if not addresses:
            raise UnsafeMediaURL("media host resolved to no addresses")
        if any(
            not address.is_global
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            for address in addresses
        ):
            raise UnsafeMediaURL("media host resolved to a non-public address")
        return ResolvedURL(url, host, effective_port, addresses)


class StreamingResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def iter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


class StreamingClient(Protocol):
    async def open(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        resolved_ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
        timeout: float,
    ) -> StreamingResponse: ...


class RefreshProvider(Protocol):
    async def refresh(
        self,
        request: MediaRequest,
        descriptor: RefreshDescriptor,
        *,
        attempt: int,
        deadline: float,
    ) -> MediaCandidate: ...


class _CurlResponse:
    def __init__(self, response: Response, session: AsyncSession[Response]) -> None:
        self._response = response
        self._session = session
        self.status_code = response.status_code
        self.headers: Mapping[str, str] = {
            str(key): str(value)
            for key, value in response.headers.items()
            if value is not None
        }

    async def iter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
        async for chunk in self._response.aiter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk

    async def close(self) -> None:
        try:
            await self._response.aclose()
        finally:
            await self._session.close()


class CurlStreamingClient:
    """curl client that pins a prevalidated DNS answer for the request."""

    async def open(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        resolved_ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
        timeout: float,
    ) -> StreamingResponse:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        address = f"[{resolved_ip}]" if resolved_ip.version == 6 else str(resolved_ip)
        session: AsyncSession[Response] = AsyncSession(
            curl_options={CurlOpt.RESOLVE: [f"{host}:{port}:{address}"]}
        )
        try:
            response = await session.request(
                cast(Any, method),
                url,
                headers=dict(headers),
                timeout=timeout,
                allow_redirects=False,
                stream=True,
                impersonate="chrome",
            )
        except BaseException:
            await session.close()
            raise
        return _CurlResponse(response, session)


@dataclass(slots=True)
class MaterializedItem:
    paths: tuple[Path, ...]
    size_bytes: int
    candidate: MediaCandidate = field(repr=False)
    _reservation: DiskReservation = field(repr=False)

    async def release(self, *, delete: bool = False) -> None:
        try:
            if delete:
                for path in self.paths:
                    path.unlink(missing_ok=True)
        finally:
            await self._reservation.release()

    def renew_lease(self) -> None:
        self._reservation.renew()


@dataclass(slots=True)
class _Opened:
    response: StreamingResponse
    url: str
    started_at: float


class MediaTransport:
    """Validate, probe, and stream an equivalent provider candidate to disk."""

    def __init__(
        self,
        *,
        output_dir: str | os.PathLike[str] = "/srv/ytdlbot/media",
        client: StreamingClient | None = None,
        url_policy: URLPolicy | None = None,
        disk_budget: DiskBudget | None = None,
        max_bytes: int = DECIMAL_MEDIA_LIMIT,
        probe_bytes: int = 4096,
        chunk_size: int = 1024 * 1024,
        first_byte_timeout: float = 1.5,
        stall_timeout: float = 3.0,
        request_timeout: float = 30.0,
        dns_timeout: float = 3.0,
        max_redirects: int = 3,
        materialization_timeout: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_bytes <= 0 or probe_bytes <= 0 or chunk_size <= 0:
            raise ValueError("transport byte limits must be positive")
        if any(
            not math.isfinite(value) or value <= 0
            for value in (
                first_byte_timeout,
                stall_timeout,
                request_timeout,
                dns_timeout,
                materialization_timeout,
            )
        ):
            raise ValueError("transport timeouts must be finite and positive")
        if max_redirects < 0:
            raise ValueError("redirect budget must not be negative")
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.client = client or CurlStreamingClient()
        self.url_policy = url_policy or URLPolicy()
        self.disk_budget = disk_budget or DiskBudget(self.output_dir)
        self.max_bytes = max_bytes
        self.probe_bytes = probe_bytes
        self.chunk_size = chunk_size
        self.first_byte_timeout = first_byte_timeout
        self.stall_timeout = stall_timeout
        self.request_timeout = request_timeout
        self.dns_timeout = dns_timeout
        self.max_redirects = max_redirects
        self.materialization_timeout = materialization_timeout
        self.clock = clock

    async def probe(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> bytes:
        request_headers = {
            "Accept": "*/*",
            "Range": f"bytes=0-{self.probe_bytes - 1}",
            "User-Agent": "ytdlbot/2.0",
            **dict(headers or {}),
        }
        opened = await self._request("GET", url, headers=request_headers)
        response = opened.response
        try:
            self._check_response(response, opened.url)
            data = bytearray()
            iterator = response.iter_bytes(min(self.chunk_size, self.probe_bytes))
            first = True
            while len(data) < self.probe_bytes:
                try:
                    first_timeout = self.first_byte_timeout - (
                        self.clock() - opened.started_at
                    )
                    chunk = await asyncio.wait_for(
                        anext(iterator),
                        timeout=(
                            max(0, first_timeout) if first else self.stall_timeout
                        ),
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError as error:
                    raise TransferTimeout("media probe made no progress") from error
                first = False
                remaining = self.probe_bytes - len(data)
                data.extend(chunk[:remaining])
            signature = bytes(data)
            self._check_signature(signature, response.headers)
            return signature
        finally:
            await response.close()

    async def materialize(
        self,
        request: MediaRequest,
        candidates: Sequence[MediaCandidate],
        *,
        refreshers: Mapping[str, RefreshProvider] | None = None,
        deadline: float | None = None,
    ) -> MaterializedItem:
        if not candidates:
            raise MaterializationError("no media candidates")
        materialization_deadline = (
            deadline
            if deadline is not None
            else self.clock() + self.materialization_timeout
        )
        await self._validate_url(request.canonical_url)
        safe: list[MediaCandidate] = []
        errors: list[MaterializationError] = []
        for candidate in candidates:
            try:
                self._check_declared_size(candidate)
                await self._validate_candidate_urls(candidate)
            except MaterializationError as error:
                errors.append(error)
            else:
                safe.append(candidate)
        if not safe:
            if len(errors) == 1:
                raise errors[0]
            raise MaterializationError("all candidates failed validation")

        refresh_attempted: set[RefreshDescriptor] = set()
        refresh_lock = asyncio.Lock()

        async def attempt(candidate: MediaCandidate) -> MaterializedItem:
            try:
                return await self._download_candidate(
                    candidate, materialization_deadline
                )
            except (DownloadFailed, TransferTimeout) as error:
                descriptor = candidate.refresh
                provider = (
                    refreshers.get(descriptor.provider)
                    if refreshers and descriptor is not None
                    else None
                )
                if provider is None or descriptor is None:
                    raise
                async with refresh_lock:
                    if descriptor in refresh_attempted:
                        raise
                    refresh_attempted.add(descriptor)
                if self.clock() >= materialization_deadline:
                    raise TransferTimeout(
                        "materialization deadline exceeded"
                    ) from error
                fresh = await provider.refresh(
                    request,
                    descriptor,
                    attempt=0,
                    deadline=materialization_deadline,
                )
                self._check_declared_size(fresh)
                await self._validate_candidate_urls(fresh)
                return await self._download_candidate(fresh, materialization_deadline)

        if len(safe) >= 2 and all(self._is_small(candidate) for candidate in safe[:2]):
            winner, parallel_errors = await self._race_small(
                safe[:2], attempt, materialization_deadline
            )
            errors.extend(parallel_errors)
            if winner is not None:
                return winner
            safe = safe[2:]

        for candidate in safe:
            if self.clock() >= materialization_deadline:
                break
            try:
                return await attempt(candidate)
            except MaterializationError as error:
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        summary = ", ".join(type(error).__name__ for error in errors)
        summary = summary or "no usable result"
        raise MaterializationError(f"materialization failed ({summary})")

    async def _race_small(
        self,
        candidates: Sequence[MediaCandidate],
        attempt: Callable[[MediaCandidate], Coroutine[Any, Any, MaterializedItem]],
        deadline: float,
    ) -> tuple[MaterializedItem | None, list[MaterializationError]]:
        tasks: set[asyncio.Task[MaterializedItem]] = {
            asyncio.create_task(attempt(candidate)) for candidate in candidates
        }
        errors: list[MaterializationError] = []
        winner: MaterializedItem | None = None
        try:
            while tasks and self.clock() < deadline:
                done, tasks = await asyncio.wait(
                    tasks,
                    timeout=max(0, deadline - self.clock()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    break
                for task in done:
                    try:
                        result = task.result()
                    except MaterializationError as error:
                        errors.append(error)
                    else:
                        if winner is None:
                            winner = result
                        else:
                            await result.release(delete=True)
                if winner is not None:
                    break
        finally:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for gathered_result in results:
                if isinstance(gathered_result, MaterializedItem):
                    await gathered_result.release(delete=True)
        return winner, errors

    async def _download_candidate(
        self, candidate: MediaCandidate, deadline: float
    ) -> MaterializedItem:
        sources = self._download_sources(candidate)
        declared = self._declared_size(candidate)
        reservation = await self.disk_budget.reserve(
            declared if declared is not None else self.max_bytes,
            owner=uuid.uuid4().hex,
        )
        completed: list[Path] = []
        total = 0
        try:
            for source in sources:
                if self.clock() >= deadline:
                    raise TransferTimeout("materialization deadline exceeded")
                if source.expires_at is not None and source.expires_at <= time.time():
                    raise DownloadFailed("media URL expired or denied")
                await self.probe(source.url, headers=dict(source.http_headers))
                path, written = await self._stream_source(
                    source,
                    reservation,
                    already_written=total,
                    deadline=deadline,
                )
                completed.append(path)
                total += written
            return MaterializedItem(tuple(completed), total, candidate, reservation)
        except BaseException:
            for path in completed:
                path.unlink(missing_ok=True)
            await reservation.release()
            raise

    async def _stream_source(
        self,
        source: MediaSource,
        reservation: DiskReservation,
        *,
        already_written: int,
        deadline: float,
    ) -> tuple[Path, int]:
        headers = {"Accept": "*/*", "User-Agent": "ytdlbot/2.0"}
        headers.update(dict(source.http_headers))
        opened = await self._request("GET", source.url, headers=headers)
        response = opened.response
        extension = _extension(source)
        final_path = self.output_dir / f"media_{uuid.uuid4().hex}{extension}"
        partial_path = final_path.with_suffix(f"{final_path.suffix}.part")
        written = 0
        try:
            reservation.bind(partial_path)
            self._check_response(response, opened.url)
            declared_response = _content_length(response.headers)
            if declared_response is not None:
                self._check_actual_size(already_written + declared_response)
                await reservation.ensure(already_written + declared_response)
            iterator = response.iter_bytes(self.chunk_size)
            # Opening the already-created local path is intentionally synchronous:
            # cancellation cannot strand a file descriptor between a worker thread
            # completing open() and handing its result back to this coroutine.
            output = partial_path.open("wb")
            try:
                first = True
                signature = bytearray()
                while True:
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise TransferTimeout("materialization deadline exceeded")
                    timeout = min(
                        (
                            max(
                                0,
                                self.first_byte_timeout
                                - (self.clock() - opened.started_at),
                            )
                            if first
                            else self.stall_timeout
                        ),
                        remaining,
                    )
                    try:
                        chunk = await asyncio.wait_for(anext(iterator), timeout=timeout)
                    except StopAsyncIteration:
                        break
                    except TimeoutError as error:
                        phase = "first byte" if first else "stream progress"
                        raise TransferTimeout(f"media {phase} timed out") from error
                    first = False
                    if not chunk:
                        continue
                    written += len(chunk)
                    self._check_actual_size(already_written + written)
                    await reservation.ensure(already_written + written)
                    if len(signature) < self.probe_bytes:
                        signature.extend(chunk[: self.probe_bytes - len(signature)])
                    await asyncio.to_thread(output.write, chunk)
            finally:
                await asyncio.to_thread(output.close)
            if written == 0:
                raise DownloadFailed("media response was empty")
            self._check_signature(bytes(signature), response.headers)
            os.replace(partial_path, final_path)
            reservation.rebind(partial_path, final_path)
            return final_path, written
        except BaseException:
            partial_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        finally:
            await response.close()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body_sensitive: bool = False,
    ) -> _Opened:
        started_at = self.clock()
        current_url = url
        current_method = method.upper()
        current_headers = dict(headers or {})
        for redirect_count in range(self.max_redirects + 1):
            try:
                remaining = self.first_byte_timeout - (self.clock() - started_at)
                resolved = await asyncio.wait_for(
                    self.url_policy.resolve(current_url), timeout=max(0, remaining)
                )
                response = await asyncio.wait_for(
                    self.client.open(
                        current_method,
                        current_url,
                        headers=current_headers,
                        resolved_ip=resolved.addresses[0],
                        timeout=self.request_timeout,
                    ),
                    timeout=max(
                        0,
                        self.first_byte_timeout - (self.clock() - started_at),
                    ),
                )
            except (UnsafeMediaURL, MaterializationError):
                raise
            except (TimeoutError, OSError) as error:
                raise TransferTimeout("media connection failed") from error
            except Exception as error:
                raise DownloadFailed("media connection failed") from error
            if response.status_code not in _REDIRECTS:
                return _Opened(response, current_url, started_at)
            location = _header(response.headers, "location")
            await response.close()
            if not location or redirect_count >= self.max_redirects:
                raise DownloadFailed("invalid media redirect")
            next_url = urljoin(current_url, location)
            origin_changed = _origin(current_url) != _origin(next_url)
            if origin_changed and response.status_code in {307, 308} and body_sensitive:
                raise CredentialRedirectError(
                    "refusing cross-origin body-preserving redirect"
                )
            if origin_changed:
                current_headers = {
                    key: value
                    for key, value in current_headers.items()
                    if key.lower() in _CROSS_ORIGIN_HEADER_ALLOWLIST
                }
            if response.status_code in {301, 302, 303}:
                current_method = "GET"
                body_sensitive = False
            current_url = next_url
        raise DownloadFailed("too many media redirects")

    async def _validate_candidate_urls(self, candidate: MediaCandidate) -> None:
        urls = [candidate.url]
        urls.extend(source.url for source in candidate.sources)
        urls.extend(item.url for item in candidate.items)
        for url in dict.fromkeys(urls):
            await self._validate_url(url)

    async def _validate_url(self, url: str) -> None:
        try:
            await asyncio.wait_for(
                self.url_policy.resolve(url), timeout=self.dns_timeout
            )
        except TimeoutError as error:
            raise UnsafeMediaURL("media host resolution timed out") from error

    def _download_sources(self, candidate: MediaCandidate) -> tuple[MediaSource, ...]:
        if candidate.sources:
            return candidate.sources
        if candidate.items:
            return tuple(
                MediaSource(
                    format_id=str(index),
                    url=item.url,
                    container=item.container,
                    filesize_bytes=item.filesize_bytes,
                )
                for index, item in enumerate(candidate.items)
            )
        return (
            MediaSource(
                format_id=candidate.candidate_id,
                url=candidate.url,
                container=candidate.container,
                filesize_bytes=candidate.filesize_bytes,
            ),
        )

    def _declared_size(self, candidate: MediaCandidate) -> int | None:
        if candidate.filesize_bytes is not None:
            return candidate.filesize_bytes
        sources = self._download_sources(candidate)
        if all(source.filesize_bytes is not None for source in sources):
            return sum(source.filesize_bytes or 0 for source in sources)
        return None

    def _check_declared_size(self, candidate: MediaCandidate) -> None:
        declared = self._declared_size(candidate)
        if declared is not None and (declared < 0 or declared > self.max_bytes):
            raise MediaSizeExceeded("declared media size exceeds configured limit")

    def _is_small(self, candidate: MediaCandidate) -> bool:
        declared = self._declared_size(candidate)
        return declared is not None and declared <= SMALL_PARALLEL_LIMIT

    def _check_actual_size(self, size: int) -> None:
        if size > self.max_bytes:
            raise MediaSizeExceeded("streamed media exceeds configured limit")

    def _check_response(self, response: StreamingResponse, url: str) -> None:
        del url
        if response.status_code in {401, 403, 404, 410}:
            raise DownloadFailed("media URL expired or denied")
        if response.status_code == 429 or response.status_code >= 500:
            raise DownloadFailed("media source temporarily unavailable")
        if response.status_code not in {200, 206}:
            raise DownloadFailed("unexpected media response")
        declared = _content_length(response.headers)
        if declared is not None:
            self._check_actual_size(declared)

    @staticmethod
    def _check_signature(data: bytes, headers: Mapping[str, str]) -> None:
        content_type = (_header(headers, "content-type") or "").lower()
        prefix = data.lstrip()[:32].lower()
        if not data:
            raise DownloadFailed("media probe was empty")
        if "text/html" in content_type or prefix.startswith((b"<!doctype", b"<html")):
            raise DownloadFailed("media probe returned HTML")
        if not _has_media_signature(data):
            raise DownloadFailed("media probe returned an unknown container signature")


async def _system_resolver(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    return tuple(dict.fromkeys(str(record[4][0]) for record in records))


def redact_url(url: str) -> str:
    """Return a log-safe URL without credentials, query, or fragment."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return "<invalid-url>"
    return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, "", ""))


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    return (
        scheme,
        (parsed.hostname or "").lower().rstrip("."),
        parsed.port or (443 if scheme == "https" else 80),
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = _header(headers, "content-length")
    if not value:
        return None
    try:
        length = int(value)
    except ValueError:
        return None
    return length if length >= 0 else None


def _extension(source: MediaSource) -> str:
    container = (source.container or "").lower().lstrip(".")
    if container.isalnum() and len(container) <= 8:
        return f".{container}"
    suffix = Path(urlsplit(source.url).path).suffix.lower()
    if suffix and suffix[1:].isalnum() and len(suffix) <= 9:
        return suffix
    return ".bin"


def _has_media_signature(data: bytes) -> bool:
    """Recognize the bounded signatures accepted by current media providers."""
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return True  # MP4/M4A/MOV/HEIF/AVIF family
    if data.startswith(
        (
            b"\x1aE\xdf\xa3",  # Matroska/WebM
            b"\xff\xd8\xff",  # JPEG
            b"\x89PNG\r\n\x1a\n",
            b"GIF87a",
            b"GIF89a",
            b"OggS",
            b"fLaC",
            b"ID3",
            b"BM",  # BMP
            b"\x00\x00\x01\xba",  # MPEG program stream
        )
    ):
        return True
    if (
        len(data) >= 12
        and data.startswith(b"RIFF")
        and data[8:12]
        in {
            b"AVI ",
            b"WAVE",
            b"WEBP",
        }
    ):
        return True
    if len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0:
        return True  # MP3 or AAC frame sync
    return bool(data) and data[0] == 0x47  # MPEG transport stream sync
