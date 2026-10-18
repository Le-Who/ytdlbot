"""HTTP download failures must terminate their owned files and background work."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import ClientDisconnect

from app.api import routes
from app.constants import GIF_FORMAT_ID
from app.core.models import DownloadContext


@pytest.fixture
def admitted_download(monkeypatch):
    context = DownloadContext(page_url="https://youtu.be/example", format_id="22")
    monkeypatch.setattr(
        routes.state, "link_cache", SimpleNamespace(get=AsyncMock(return_value=context))
    )
    monkeypatch.setattr(
        routes.state,
        "limiter",
        SimpleNamespace(
            allow_ip=AsyncMock(return_value=True),
            allow_token=AsyncMock(return_value=True),
        ),
    )
    monkeypatch.setattr(routes.state, "media_pipeline", None)
    return context, SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


@pytest.mark.parametrize("stage", ("start", "body"))
@pytest.mark.parametrize("failure", ("disconnect", "cancel"))
async def test_http_response_failure_releases_materialization(
    admitted_download, tmp_path: Path, monkeypatch, stage, failure
):
    """Catch a lease left open when ASGI send fails outside generator execution."""
    _, request = admitted_download
    path = tmp_path / "owned.mp4"
    path.write_bytes(b"media")
    exited = []
    managers = []

    @asynccontextmanager
    async def materialize(media_request):
        try:
            yield SimpleNamespace(paths=(path,), size_bytes=5, renew_lease=lambda: None)
        finally:
            await asyncio.sleep(0)
            path.unlink()
            exited.append(True)

    def open_materialized(media_request):
        manager = materialize(media_request)
        managers.append(manager)
        return manager

    monkeypatch.setattr(
        routes.state,
        "media_pipeline",
        SimpleNamespace(open_materialized=open_materialized),
    )
    response = await routes.download("token", request)
    sending = asyncio.Event()
    sent = []

    async def send(message):
        sent.append(message)
        if message["type"] == f"http.response.{stage}":
            sending.set()
            if failure == "disconnect":
                raise OSError("client closed")
            await asyncio.Event().wait()

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
    task = asyncio.create_task(response(scope, AsyncMock(), send))
    try:
        await asyncio.wait_for(sending.wait(), timeout=1)
        if failure == "cancel":
            task.cancel()
        with pytest.raises(
            ClientDisconnect if failure == "disconnect" else asyncio.CancelledError
        ):
            await task
        assert not path.exists(), "response terminated with its media lease still owned"
        assert exited == [True]
        assert not any(
            message["type"] == "http.response.body"
            and message.get("more_body") is False
            for message in sent
        )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await response.body_iterator.aclose()
        for manager in managers:
            await manager.__aexit__(None, None, None)


class _Process:
    def __init__(self, chunks, returncode):
        self.stdout = SimpleNamespace(read=self.read)
        self.stdin = SimpleNamespace(
            write=lambda chunk: None, drain=AsyncMock(), close=lambda: None
        )
        self.chunks = iter(chunks)
        self.returncode = returncode

    async def read(self, count):
        return next(self.chunks, b"")

    async def wait(self):
        assert self.returncode is not None, "waited for an unread live source"
        return self.returncode


@pytest.mark.parametrize(
    ("format_id", "exit_codes"),
    (("22", (7,)), (GIF_FORMAT_ID, (7, 0)), (GIF_FORMAT_ID, (0, 9))),
)
async def test_http_fallback_nonzero_exit_is_not_successful_eof(
    admitted_download, monkeypatch, format_id, exit_codes
):
    """Catch truncated subprocess output being exposed as a complete download."""
    context, request = admitted_download
    context.format_id = format_id
    processes = [_Process((b"partial", b""), code) for code in exit_codes]
    active = []
    entered = []

    @asynccontextmanager
    async def run(command, **kwargs):
        process = processes[len(entered)]
        entered.append(process)
        active.append(process)
        try:
            yield SimpleNamespace(proc=process)
        finally:
            active.remove(process)

    monkeypatch.setattr(routes, "run_subprocess", run)
    monkeypatch.setattr(
        routes.state,
        "ytdlp",
        SimpleNamespace(build_command=lambda *args, **kwargs: ["extractor"]),
    )
    response = await routes.download("token", request)
    chunks = []
    with pytest.raises(RuntimeError, match="subprocess failed"):
        async for chunk in response.body_iterator:
            chunks.append(chunk)
    assert chunks == [b"partial"]
    assert not active
    assert len(entered) == len(exit_codes)


async def test_http_gif_stream_awaits_pipe_shutdown(admitted_download, monkeypatch):
    """Catch a cancelled pipe task still running after stream/context shutdown."""
    context, request = admitted_download
    context.format_id = GIF_FORMAT_ID
    pipe_started = asyncio.Event()
    pipe_stopped = asyncio.Event()
    pipe_tasks = []
    active = []

    async def source_read(count):
        pipe_tasks.append(asyncio.current_task())
        pipe_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            pipe_stopped.set()

    async def output_read(count):
        await pipe_started.wait()
        return b""

    source = _Process((), None)
    source.stdout.read = source_read
    output = _Process((), 0)
    output.stdout.read = output_read

    @asynccontextmanager
    async def run(command, **kwargs):
        process = source if command == ["extractor"] else output
        active.append(process)
        try:
            yield SimpleNamespace(proc=process)
        finally:
            active.remove(process)

    monkeypatch.setattr(routes, "run_subprocess", run)
    monkeypatch.setattr(
        routes.state,
        "ytdlp",
        SimpleNamespace(build_command=lambda *args, **kwargs: ["extractor"]),
    )
    response = await routes.download("token", request)
    try:
        assert [chunk async for chunk in response.body_iterator] == []
        assert pipe_stopped.is_set(), "stream returned before its pipe task terminated"
        assert not active
    finally:
        for task in pipe_tasks:
            task.cancel()
        await asyncio.gather(*pipe_tasks, return_exceptions=True)


async def test_http_fallback_disconnect_closes_process_context(
    admitted_download, monkeypatch
):
    """Catch an extractor left owned while the generator is suspended at yield."""
    _, request = admitted_download
    active = []

    @asynccontextmanager
    async def run(command, **kwargs):
        process = _Process((b"partial",), None)
        active.append(process)
        try:
            yield SimpleNamespace(proc=process)
        finally:
            await asyncio.sleep(0)
            active.remove(process)

    monkeypatch.setattr(routes, "run_subprocess", run)
    monkeypatch.setattr(
        routes.state,
        "ytdlp",
        SimpleNamespace(build_command=lambda *args, **kwargs: ["extractor"]),
    )
    response = await routes.download("token", request)

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("client closed")

    try:
        with pytest.raises(ClientDisconnect):
            await response(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send
            )
        assert not active, "response terminated before subprocess context cleanup"
    finally:
        await response.body_iterator.aclose()


async def test_http_disconnect_before_body_with_receive_listener(
    admitted_download, tmp_path, monkeypatch
):
    """Catch pre-body leaks through the ASGI 2.3 disconnect-listener path."""
    _, request = admitted_download
    path = tmp_path / "owned.mp4"
    path.write_bytes(b"media")

    @asynccontextmanager
    async def materialize(media_request):
        try:
            yield SimpleNamespace(paths=(path,), size_bytes=5, renew_lease=lambda: None)
        finally:
            await asyncio.sleep(0)
            path.unlink()

    manager = materialize(None)
    monkeypatch.setattr(
        routes.state,
        "media_pipeline",
        SimpleNamespace(open_materialized=lambda request: manager),
    )
    response = await routes.download("token", request)
    started = asyncio.Event()

    async def send(message):
        assert message["type"] == "http.response.start"
        started.set()
        await asyncio.Event().wait()

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    try:
        await asyncio.wait_for(
            response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send),
            timeout=1,
        )
        assert not path.exists(), "disconnect listener left the eager lease owned"
    finally:
        await response.body_iterator.aclose()
        await manager.__aexit__(None, None, None)
