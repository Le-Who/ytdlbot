"""SSRF-safe, bounded streaming materialization for provider candidates."""

from __future__ import annotations

import asyncio
import errno
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

from app.core.metrics import metrics
from app.core.process import run_subprocess
from app.core.resource_budget import DiskBudget, DiskReservation

from .models import MediaCandidate, MediaRequest, MediaSource, RefreshDescriptor
from .validation import validate_candidate

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
ProcessRunner = Callable[[list[str], float], Awaitable[int]]
FileMover = Callable[[Path, Path], Any]


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
        output_dir: str | os.PathLike[str] | None = None,
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
        transform_poll_interval: float = 0.01,
        cleanup_timeout: float = 3.5,
        clock: Callable[[], float] = time.monotonic,
        process_runner: ProcessRunner | None = None,
        file_mover: FileMover = os.replace,
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
                transform_poll_interval,
                cleanup_timeout,
            )
        ):
            raise ValueError("transport timeouts must be finite and positive")
        if max_redirects < 0:
            raise ValueError("redirect budget must not be negative")
        if output_dir is None:
            from app.core.config import MEDIA_DIR

            output_dir = MEDIA_DIR
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
        self.transform_poll_interval = transform_poll_interval
        self.cleanup_timeout = cleanup_timeout
        self.clock = clock
        self.process_runner = process_runner or _run_process
        self.file_mover = file_mover
        self._detached_cleanup_tasks: set[asyncio.Future[Any]] = set()

    async def probe(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        deadline: float | None = None,
    ) -> bytes:
        operation_deadline = (
            deadline if deadline is not None else self.clock() + self.request_timeout
        )
        self._remaining(operation_deadline)
        request_headers = {
            "Accept": "*/*",
            "Range": f"bytes=0-{self.probe_bytes - 1}",
            "User-Agent": "ytdlbot/2.0",
            **dict(headers or {}),
        }
        opened = await self._request(
            "GET", url, headers=request_headers, deadline=operation_deadline
        )
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
                            min(
                                self._remaining(operation_deadline),
                                max(0, first_timeout) if first else self.stall_timeout,
                            )
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
            await self._close_response(response, operation_deadline)

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
        deadlines = [self.clock() + self.materialization_timeout]
        if request.deadline is not None:
            deadlines.append(request.deadline)
        if deadline is not None:
            deadlines.append(deadline)
        materialization_deadline = min(deadlines)
        self._remaining(materialization_deadline)
        await self._validate_url(request.canonical_url, materialization_deadline)
        safe: list[MediaCandidate] = []
        errors: list[MaterializationError] = []
        for candidate in candidates:
            try:
                self._check_declared_size(candidate)
                await self._validate_candidate_urls(candidate, materialization_deadline)
            except MaterializationError as error:
                errors.append(error)
            else:
                safe.append(candidate)
        if not safe:
            if len(errors) == 1:
                raise errors[0]
            raise MaterializationError("all candidates failed validation")

        refresh_attempted = False
        refresh_lock = asyncio.Lock()

        async def attempt(candidate: MediaCandidate) -> MaterializedItem:
            nonlocal refresh_attempted
            try:
                return await self._download_candidate(
                    request, candidate, materialization_deadline
                )
            except (DownloadFailed, TransferTimeout):
                descriptor = candidate.refresh
                provider = (
                    refreshers.get(descriptor.provider)
                    if refreshers and descriptor is not None
                    else None
                )
                if provider is None or descriptor is None:
                    raise
                async with refresh_lock:
                    if refresh_attempted:
                        raise
                    refresh_attempted = True
                remaining = self._remaining(materialization_deadline)
                try:
                    fresh = await asyncio.wait_for(
                        provider.refresh(
                            request,
                            descriptor,
                            attempt=0,
                            deadline=materialization_deadline,
                        ),
                        timeout=remaining,
                    )
                except asyncio.CancelledError:
                    raise
                except TimeoutError as refresh_error:
                    raise TransferTimeout(
                        "provider refresh timed out"
                    ) from refresh_error
                except Exception as refresh_error:
                    raise DownloadFailed("provider refresh failed") from refresh_error
                self._validate_refreshed_candidate(
                    request, candidate, descriptor, fresh
                )
                self._check_declared_size(fresh)
                await self._validate_candidate_urls(fresh, materialization_deadline)
                return await self._download_candidate(
                    request, fresh, materialization_deadline
                )

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

    async def adopt_local(
        self,
        request: MediaRequest,
        path: Path,
        candidate: MediaCandidate,
        *,
        deadline: float | None = None,
    ) -> MaterializedItem:
        """Move a locally derived artifact under DiskBudget/lease ownership.

        A requested clip is applied after adoption through the same supervised
        transform path used for downloaded candidates.
        """
        source = path.resolve()
        if not source.is_file():
            raise DownloadFailed("local artifact is missing")
        source_size = source.stat().st_size
        if source_size <= 0:
            raise DownloadFailed("local artifact is empty")
        self._check_actual_size(source_size)
        deadlines = [self.clock() + self.materialization_timeout]
        if request.deadline is not None:
            deadlines.append(request.deadline)
        if deadline is not None:
            deadlines.append(deadline)
        materialization_deadline = min(deadlines)
        reserve_timeout = self._remaining(materialization_deadline)
        try:
            reservation = await asyncio.wait_for(
                self.disk_budget.reserve(source_size, owner=uuid.uuid4().hex),
                timeout=reserve_timeout,
            )
        except TimeoutError as error:
            raise TransferTimeout("disk reservation timed out") from error

        adopted = self.output_dir / (
            f"media_{uuid.uuid4().hex}{_candidate_extension(candidate)}"
        )
        staging = adopted.with_suffix(f"{adopted.suffix}.part")
        completed: tuple[Path, ...] = ()
        succeeded = False
        try:
            reservation.bind(adopted)
            try:
                await asyncio.to_thread(self.file_mover, source, adopted)
            except OSError as error:
                if (
                    error.errno != errno.EXDEV
                    and getattr(error, "winerror", None) != 17
                ):
                    raise
                reservation.unbind(adopted)
                reservation.bind(staging)
                await self._copy_local_file(source, staging, materialization_deadline)
                if staging.stat().st_size != source_size:
                    raise DownloadFailed("local artifact copy was truncated")
                reservation.promote(staging, adopted)
                source.unlink(missing_ok=True)
            self._remaining(materialization_deadline)
            if adopted.stat().st_size != source_size:
                raise DownloadFailed("local artifact move was truncated")
            completed = await self._finalize_candidate(
                request,
                candidate,
                [adopted],
                reservation,
                source_size,
                materialization_deadline,
            )
            final_size = sum(item.stat().st_size for item in completed)
            self._check_actual_size(final_size)
            self._remaining(materialization_deadline)
            succeeded = True
            return MaterializedItem(completed, final_size, candidate, reservation)
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise TransferTimeout("local artifact adoption timed out") from error
        except MaterializationError:
            raise
        except Exception as error:
            raise DownloadFailed("local artifact adoption failed") from error
        finally:
            if not succeeded:
                adopted.unlink(missing_ok=True)
                staging.unlink(missing_ok=True)
                for item in completed:
                    item.unlink(missing_ok=True)
                await reservation.release()

    async def _copy_local_file(
        self, source: Path, destination: Path, deadline: float
    ) -> None:
        """Copy across filesystems with cancellation bounded to one chunk."""
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            while True:
                self._remaining(deadline)
                chunk = await _await_thread_io(
                    asyncio.to_thread(input_stream.read, self.chunk_size)
                )
                if not chunk:
                    break
                await _await_thread_io(asyncio.to_thread(output_stream.write, chunk))
            await _await_thread_io(asyncio.to_thread(output_stream.flush))

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
                            metrics.race_wasted_bytes.inc(
                                result.size_bytes,
                                provider=result.candidate.provider or "unknown",
                            )
                            await result.release(delete=True)
                if winner is not None:
                    break
        finally:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for gathered_result in results:
                if isinstance(gathered_result, MaterializedItem):
                    metrics.race_wasted_bytes.inc(
                        gathered_result.size_bytes,
                        provider=gathered_result.candidate.provider or "unknown",
                    )
                    await gathered_result.release(delete=True)
        return winner, errors

    async def _download_candidate(
        self, request: MediaRequest, candidate: MediaCandidate, deadline: float
    ) -> MaterializedItem:
        self._remaining(deadline)
        sources = self._download_sources(candidate)
        declared = self._declared_size(candidate)
        reserve_timeout = self._remaining(deadline)
        try:
            reservation = await asyncio.wait_for(
                self.disk_budget.reserve(
                    declared if declared is not None else self.max_bytes,
                    owner=uuid.uuid4().hex,
                ),
                timeout=reserve_timeout,
            )
        except TimeoutError as error:
            raise TransferTimeout("disk reservation timed out") from error
        completed: list[Path] = []
        total = 0
        try:
            for source in sources:
                if self.clock() >= deadline:
                    raise TransferTimeout("materialization deadline exceeded")
                if source.expires_at is not None and source.expires_at <= time.time():
                    raise DownloadFailed("media URL expired or denied")
                await self.probe(
                    source.url,
                    headers=dict(source.http_headers),
                    deadline=deadline,
                )
                path, written = await self._stream_source(
                    source,
                    reservation,
                    already_written=total,
                    deadline=deadline,
                )
                completed.append(path)
                total += written
            final_paths = await self._finalize_candidate(
                request,
                candidate,
                completed,
                reservation,
                total,
                deadline,
            )
            final_size = sum(path.stat().st_size for path in final_paths)
            self._check_actual_size(final_size)
            self._remaining(deadline)
            return MaterializedItem(final_paths, final_size, candidate, reservation)
        except BaseException:
            for path in completed:
                path.unlink(missing_ok=True)
            await reservation.release()
            raise

    async def _finalize_candidate(
        self,
        request: MediaRequest,
        candidate: MediaCandidate,
        inputs: list[Path],
        reservation: DiskReservation,
        input_bytes: int,
        deadline: float,
    ) -> tuple[Path, ...]:
        clip_requested = _clip_requested(request)
        mode = candidate.mux_mode
        if mode is None and not clip_requested:
            if len(inputs) > 1 and not candidate.items:
                raise DownloadFailed("split media candidate omitted a mux plan")
            return tuple(inputs)
        if candidate.items and len(inputs) > 1:
            raise DownloadFailed("album transforms require per-item plans")
        if mode not in {None, "copy", "extract-mp3", "mute-mp4"}:
            raise DownloadFailed("unsupported media transform plan")
        if mode == "extract-mp3" and len(inputs) != 1:
            raise DownloadFailed("MP3 extraction requires one source")

        extension = (
            ".mp3"
            if mode == "extract-mp3"
            else ".mp4"
            if mode == "mute-mp4"
            else _candidate_extension(candidate)
        )
        final_path = self.output_dir / f"media_{uuid.uuid4().hex}{extension}"
        partial_path = final_path.with_suffix(f"{final_path.suffix}.part")
        # During a transform both inputs and the final artifact coexist.
        reserve_timeout = self._remaining(deadline)
        try:
            await asyncio.wait_for(
                reservation.ensure(input_bytes + self.max_bytes),
                timeout=reserve_timeout,
            )
        except TimeoutError as error:
            raise TransferTimeout("transform reservation timed out") from error
        reservation.bind(partial_path)
        command = self._build_transform_command(
            inputs, partial_path, request, candidate, mode or "copy", extension
        )
        succeeded = False
        try:
            return_code = await self._run_transform(command, partial_path, deadline)
            if return_code != 0:
                raise DownloadFailed("media transform failed")
            if not partial_path.is_file():
                raise DownloadFailed("media transform produced no output")
            output_size = partial_path.stat().st_size
            if output_size <= 0:
                raise DownloadFailed("media transform produced an empty output")
            self._check_actual_size(output_size)
            with partial_path.open("rb") as transformed:
                signature = transformed.read(self.probe_bytes)
            self._check_signature(signature, {})
            self._remaining(deadline)
            reservation.promote(partial_path, final_path)
            for path in inputs:
                path.unlink(missing_ok=True)
                reservation.unbind(path)
            succeeded = True
            return (final_path,)
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise TransferTimeout("media transform timed out") from error
        except MaterializationError:
            raise
        except Exception as error:
            raise DownloadFailed("media transform failed") from error
        finally:
            partial_path.unlink(missing_ok=True)
            if not succeeded:
                final_path.unlink(missing_ok=True)
                reservation.unbind(partial_path)
                reservation.unbind(final_path)

    def _build_transform_command(
        self,
        inputs: Sequence[Path],
        output: Path,
        request: MediaRequest,
        candidate: MediaCandidate,
        mode: str,
        extension: str,
    ) -> list[str]:
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        for path in inputs:
            command.extend(("-i", str(path)))
        command.extend(
            _animation_clip_arguments(request)
            if mode == "mute-mp4"
            else _clip_arguments(request)
        )
        if mode == "extract-mp3":
            command.extend(("-vn", "-c:a", "libmp3lame", "-f", "mp3"))
        elif mode == "mute-mp4":
            command.extend(("-map", "0:v:0", "-an", "-c:v", "copy", "-f", "mp4"))
        else:
            if len(inputs) == 2:
                command.extend(("-map", "0:v:0", "-map", "1:a:0"))
            else:
                command.extend(("-map", "0"))
            if _clip_requested(request):
                command.extend(_reencode_arguments(candidate, extension))
            else:
                command.extend(("-c", "copy"))
            command.extend(("-f", _ffmpeg_format(extension)))
        command.extend(("-fs", str(self.max_bytes)))
        command.append(str(output))
        return command

    async def _run_transform(
        self, command: list[str], partial_path: Path, deadline: float
    ) -> int:
        transform_started = self.clock()
        process_timeout = self._remaining(deadline)
        task: asyncio.Future[int] = asyncio.ensure_future(
            self.process_runner(command, process_timeout)
        )
        try:
            while True:
                remaining = self._remaining(deadline)
                done, _ = await asyncio.wait(
                    {task},
                    timeout=min(self.transform_poll_interval, remaining),
                )
                if partial_path.is_file():
                    self._check_actual_size(partial_path.stat().st_size)
                if task in done:
                    return task.result()
        except asyncio.CancelledError:
            await self._cancel_process_task(task)
            raise
        except Exception:
            await self._cancel_process_task(task)
            raise
        finally:
            metrics.transcode_cpu_seconds.inc(
                max(0.0, self.clock() - transform_started),
                operation="ffmpeg",
                measurement="wall_time_proxy",
            )

    async def _cancel_process_task(self, task: asyncio.Future[int]) -> None:
        if task.done():
            await asyncio.gather(task, return_exceptions=True)
            return
        task.cancel()
        _, pending = await asyncio.wait({task}, timeout=self.cleanup_timeout)
        if pending:
            self._detach_cleanup_task(task)
            return
        await asyncio.gather(task, return_exceptions=True)

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
        opened = await self._request(
            "GET", source.url, headers=headers, deadline=deadline
        )
        response = opened.response
        extension = _extension(source)
        final_path = self.output_dir / f"media_{uuid.uuid4().hex}{extension}"
        partial_path = final_path.with_suffix(f"{final_path.suffix}.part")
        written = 0
        try:
            try:
                reservation.bind(partial_path)
                self._check_response(response, opened.url)
                declared_response = _content_length(response.headers)
                if declared_response is not None:
                    self._check_actual_size(already_written + declared_response)
                    reserve_timeout = self._remaining(deadline)
                    try:
                        await asyncio.wait_for(
                            reservation.ensure(already_written + declared_response),
                            timeout=reserve_timeout,
                        )
                    except TimeoutError as error:
                        raise TransferTimeout("disk reservation timed out") from error
                iterator = response.iter_bytes(self.chunk_size)
                # Opening the already-created local path is intentionally synchronous:
                # cancellation cannot strand a descriptor between a worker thread
                # completing open() and handing its result back to this coroutine.
                output = partial_path.open("wb")
                try:
                    first = True
                    signature = bytearray()
                    while True:
                        remaining = self._remaining(deadline)
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
                            chunk = await asyncio.wait_for(
                                anext(iterator), timeout=timeout
                            )
                        except StopAsyncIteration:
                            break
                        except TimeoutError as error:
                            phase = "first byte" if first else "stream progress"
                            raise TransferTimeout(f"media {phase} timed out") from error
                        if not chunk:
                            continue
                        if first:
                            metrics.pipeline_duration.observe(
                                max(0.0, self.clock() - opened.started_at),
                                phase="first_byte",
                            )
                            first = False
                        written += len(chunk)
                        self._check_actual_size(already_written + written)
                        reserve_timeout = self._remaining(deadline)
                        try:
                            await asyncio.wait_for(
                                reservation.ensure(already_written + written),
                                timeout=reserve_timeout,
                            )
                        except TimeoutError as error:
                            raise TransferTimeout(
                                "disk reservation timed out"
                            ) from error
                        if len(signature) < self.probe_bytes:
                            signature.extend(chunk[: self.probe_bytes - len(signature)])
                        write_timeout = self._remaining(deadline)
                        try:
                            await asyncio.wait_for(
                                asyncio.to_thread(output.write, chunk),
                                timeout=write_timeout,
                            )
                        except TimeoutError as error:
                            raise TransferTimeout("media write timed out") from error
                finally:
                    output.close()
                if written == 0:
                    raise DownloadFailed("media response was empty")
                self._check_signature(bytes(signature), response.headers)
                self._remaining(deadline)
                reservation.promote(partial_path, final_path)
                return final_path, written
            finally:
                await self._close_response(response, deadline)
        except BaseException:
            partial_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body_sensitive: bool = False,
        deadline: float | None = None,
    ) -> _Opened:
        started_at = self.clock()
        operation_deadline = (
            deadline if deadline is not None else started_at + self.request_timeout
        )
        self._remaining(operation_deadline)
        current_url = url
        current_method = method.upper()
        current_headers = dict(headers or {})
        for redirect_count in range(self.max_redirects + 1):
            try:
                remaining = min(
                    self._remaining(operation_deadline),
                    self.first_byte_timeout - (self.clock() - started_at),
                )
                resolved = await asyncio.wait_for(
                    self.url_policy.resolve(current_url), timeout=max(0, remaining)
                )
                request_remaining = min(
                    self._remaining(operation_deadline),
                    self.first_byte_timeout - (self.clock() - started_at),
                )
                response = await asyncio.wait_for(
                    self.client.open(
                        current_method,
                        current_url,
                        headers=current_headers,
                        resolved_ip=resolved.addresses[0],
                        timeout=self.request_timeout,
                    ),
                    timeout=max(0, request_remaining),
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
            await self._close_response(response, operation_deadline)
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

    async def _validate_candidate_urls(
        self, candidate: MediaCandidate, deadline: float
    ) -> None:
        urls = [candidate.url]
        urls.extend(source.url for source in candidate.sources)
        urls.extend(item.url for item in candidate.items)
        for url in dict.fromkeys(urls):
            await self._validate_url(url, deadline)

    async def _validate_url(self, url: str, deadline: float) -> None:
        remaining = self._remaining(deadline)
        deadline_limited = remaining <= self.dns_timeout
        try:
            await asyncio.wait_for(
                self.url_policy.resolve(url), timeout=min(self.dns_timeout, remaining)
            )
        except TimeoutError as error:
            if deadline_limited:
                raise TransferTimeout("materialization deadline exceeded") from error
            raise UnsafeMediaURL("media host resolution timed out") from error

    def _validate_refreshed_candidate(
        self,
        request: MediaRequest,
        original: MediaCandidate,
        descriptor: RefreshDescriptor,
        fresh: MediaCandidate,
    ) -> None:
        identity_matches = (
            descriptor.max_attempts == 1
            and descriptor.media_id == request.media_id
            and original.provider == descriptor.provider
            and original.media_id == descriptor.media_id
            and original.candidate_id == descriptor.variant_id
            and fresh.provider == descriptor.provider
            and fresh.media_id == descriptor.media_id
            and fresh.candidate_id == descriptor.variant_id
            and fresh.auth_scope == request.auth_scope
            and fresh.auth_scope == original.auth_scope
            and fresh.refresh == descriptor
        )
        if not identity_matches or not validate_candidate(request, fresh).usable:
            raise DownloadFailed("refreshed candidate contract mismatch")

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TransferTimeout("materialization deadline exceeded")
        return remaining

    async def _close_response(
        self, response: StreamingResponse, deadline: float
    ) -> None:
        del deadline
        current_task = asyncio.current_task()
        cancellation_pending = bool(current_task and current_task.cancelling())
        close_task = asyncio.create_task(response.close())
        try:
            await asyncio.wait_for(
                asyncio.shield(close_task), timeout=self.cleanup_timeout
            )
        except asyncio.CancelledError:
            await self._finish_response_cleanup(close_task, allow_grace=True)
            raise
        except TimeoutError as error:
            await self._finish_response_cleanup(close_task, allow_grace=False)
            if cancellation_pending:
                raise asyncio.CancelledError from error
            raise TransferTimeout("media response cleanup timed out") from error
        except Exception as error:
            if cancellation_pending:
                raise asyncio.CancelledError from error
            raise DownloadFailed("media response cleanup failed") from error

    async def _finish_response_cleanup(
        self, task: asyncio.Task[None], *, allow_grace: bool
    ) -> None:
        pending: set[asyncio.Task[None]] = {task} if not task.done() else set()
        if allow_grace and pending:
            _, pending = await asyncio.wait(pending, timeout=self.cleanup_timeout)
        if pending:
            task.cancel()
            self._detach_cleanup_task(task)
            return
        await asyncio.gather(task, return_exceptions=True)

    def _detach_cleanup_task(self, task: asyncio.Future[Any]) -> None:
        """Keep stubborn cleanup alive while consuming its eventual outcome."""
        self._detached_cleanup_tasks.add(task)

        def consume_result(finished: asyncio.Future[Any]) -> None:
            self._detached_cleanup_tasks.discard(finished)
            if finished.cancelled():
                return
            finished.exception()

        task.add_done_callback(consume_result)

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


def _candidate_extension(candidate: MediaCandidate) -> str:
    container = (candidate.container or "mkv").lower().lstrip(".")
    if not container.isalnum() or len(container) > 8:
        raise DownloadFailed("invalid output container")
    return f".{container}"


def _ffmpeg_format(extension: str) -> str:
    return {
        ".m4a": "ipod",
        ".mkv": "matroska",
        ".mov": "mov",
    }.get(extension, extension.lstrip("."))


def _clip_arguments(request: MediaRequest) -> list[str]:
    start = request.clip.start_seconds
    end = request.clip.end_seconds
    arguments: list[str] = []
    if start is not None and start > 0:
        arguments.extend(("-ss", _format_seconds(start)))
    if end is not None:
        duration = end - (start or 0)
        if duration <= 0:
            raise DownloadFailed("clip interval produced no media")
        arguments.extend(("-t", _format_seconds(duration)))
    return arguments


def _animation_clip_arguments(request: MediaRequest) -> list[str]:
    """Telegram animations are silent MP4 files capped at sixty seconds."""
    start = request.clip.start_seconds
    end = request.clip.end_seconds
    arguments: list[str] = []
    if start is not None and start > 0:
        arguments.extend(("-ss", _format_seconds(start)))
    duration = 60.0
    if end is not None:
        duration = min(duration, end - (start or 0))
    if duration <= 0:
        raise DownloadFailed("clip interval produced no media")
    arguments.extend(("-t", _format_seconds(duration)))
    return arguments


def _clip_requested(request: MediaRequest) -> bool:
    return (
        request.clip.start_seconds is not None or request.clip.end_seconds is not None
    )


def _reencode_arguments(candidate: MediaCandidate, extension: str) -> list[str]:
    if not candidate.has_video:
        audio_codec = {
            ".flac": "flac",
            ".ogg": "libopus",
            ".opus": "libopus",
            ".wav": "pcm_s16le",
        }.get(extension, "aac")
        return ["-vn", "-c:a", audio_codec]
    if extension == ".webm":
        arguments = ["-c:v", "libvpx-vp9"]
        arguments.extend(("-c:a", "libopus") if candidate.has_audio else ("-an",))
        return arguments
    if extension == ".gif":
        return ["-c:v", "gif", "-an"]
    arguments = ["-c:v", "libx264", "-preset", "medium"]
    arguments.extend(("-c:a", "aac") if candidate.has_audio else ("-an",))
    return arguments


def _format_seconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.6f}".rstrip("0")


async def _await_thread_io(operation: Awaitable[Any]) -> Any:
    """Finish one local I/O chunk, then restore caller cancellation."""
    task: asyncio.Future[Any] = asyncio.ensure_future(operation)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


async def _run_process(command: list[str], timeout: float) -> int:
    try:
        async with run_subprocess(
            command,
            stdout_pipe=False,
            stderr_pipe=True,
            timeout=timeout,
        ) as handle:
            return await handle.wait()
    except TimeoutError as error:
        raise TransferTimeout("media transform timed out") from error


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
