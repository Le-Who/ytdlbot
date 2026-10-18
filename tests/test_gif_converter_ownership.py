"""GIF conversion outputs have independent paths and bounded failure cleanup."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core import state
from app.core.process import ProcessResult
from app.services import converter
from app.services.converter import MediaConverter


@pytest.fixture
def converter_env(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "conversion_sem", asyncio.Semaphore(1))
    monkeypatch.setattr(state, "gif_file_sem", asyncio.Semaphore(1))
    monkeypatch.setattr(converter, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(converter, "_probe_video_codec", AsyncMock(return_value="vp9"))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"unchanged source")
    return source


@pytest.mark.parametrize("kind", ["animation", "native"])
async def test_repeated_gif_conversions_do_not_overwrite_an_owned_output(
    converter_env, monkeypatch, kind
):
    source = converter_env
    renders = []

    async def transform(command, **kwargs):
        path = Path(command[-1])
        if kwargs["operation"] != "native_gif_palette":
            renders.append(path)
            path.write_bytes(f"render-{len(renders)}".encode())
        else:
            path.write_bytes(b"palette")
        return ProcessResult(0, b"", b"")

    monkeypatch.setattr(converter, "_run_transform_process", transform)
    convert = (
        MediaConverter.convert_to_gif_ffmpeg
        if kind == "animation"
        else MediaConverter.convert_to_native_gif
    )
    first = Path(await convert(str(source)))
    second = Path(await convert(str(source)))

    assert first != second
    assert first.read_bytes() == b"render-1"
    assert second.read_bytes() == b"render-2"
    assert source.read_bytes() == b"unchanged source"


@pytest.mark.parametrize("kind", ["animation", "native"])
@pytest.mark.parametrize("failure", ["returncode", "empty", "exception", "cancel"])
async def test_unsuccessful_gif_conversion_removes_its_partial_output(
    converter_env, monkeypatch, kind, failure
):
    source = converter_env
    outputs = []

    async def transform(command, **kwargs):
        path = Path(command[-1])
        outputs.append(path)
        palette = kwargs["operation"] == "native_gif_palette"
        path.write_bytes(b"partial" if failure != "empty" or palette else b"")
        if not palette:
            if failure == "exception":
                raise OSError("transform adapter failed")
            if failure == "cancel":
                raise asyncio.CancelledError
            if failure == "returncode":
                return ProcessResult(1, b"", b"conversion failed")
        return ProcessResult(0, b"", b"")

    monkeypatch.setattr(converter, "_run_transform_process", transform)
    convert = (
        MediaConverter.convert_to_gif_ffmpeg
        if kind == "animation"
        else MediaConverter.convert_to_native_gif
    )
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await convert(str(source))
    else:
        assert await convert(str(source)) is None

    assert outputs
    assert all(not output.exists() for output in outputs)
    assert source.read_bytes() == b"unchanged source"
