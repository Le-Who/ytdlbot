from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.cobalt import _stream_response


class Response:
    def __init__(self, chunks: tuple[bytes, ...], declared: int) -> None:
        self.chunks = chunks
        self.headers = {"content-length": str(declared)}
        self.largest_chunk = 0

    async def aiter_content(self, chunk_size: int):
        assert chunk_size == 1024 * 1024
        for chunk in self.chunks:
            await asyncio.sleep(0)
            self.largest_chunk = max(self.largest_chunk, len(chunk))
            yield chunk


@pytest.mark.asyncio
async def test_cobalt_streams_chunks_and_verifies_declared_length(
    tmp_path: Path,
) -> None:
    response = Response((b"first", b"second"), declared=11)
    output = tmp_path / "media.bin"

    written = await _stream_response(response, str(output))

    assert written == 11
    assert response.largest_chunk == 6
    assert output.read_bytes() == b"firstsecond"


@pytest.mark.asyncio
async def test_cobalt_rejects_truncated_declared_length(tmp_path: Path) -> None:
    response = Response((b"short",), declared=10)

    with pytest.raises(ValueError, match="truncated"):
        await _stream_response(response, str(tmp_path / "partial.bin"))
