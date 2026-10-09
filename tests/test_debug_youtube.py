from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.services.ytdlp.models import ExtractionResult, FormatItem


@pytest.mark.asyncio
async def test_debug_awaits_extraction_result_and_reports_its_fields(
    monkeypatch, capsys
) -> None:
    spec = importlib.util.spec_from_file_location(
        "synthetic_youtube_debug",
        Path(__file__).resolve().parents[1] / "scripts/debug_youtube.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class SyntheticService:
        async def list_formats(self, url):
            return ExtractionResult(
                title="Synthetic video",
                formats=[
                    FormatItem("1", "mp4", 360, 100),
                    FormatItem("2", "mp4", 720, 200),
                ],
                special_format=None,
                duration_str="0:10",
                is_slideshow=False,
                info_json_path=None,
                thumbnail_url=None,
            )

    monkeypatch.setattr(module, "YtDlpService", SyntheticService)
    await module.debug_youtube()
    output = capsys.readouterr().out
    assert "SUCCESS: Found 2 formats for 'Synthetic video'" in output
    assert "FAILURE" not in output
