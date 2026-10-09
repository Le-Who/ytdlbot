"""Titles are escaped by the real format-picker callback, not a test copy."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.callbacks import on_back
from app.core import state
from app.services.ytdlp.models import ExtractionResult


@pytest.mark.parametrize(
    ("title", "escaped"),
    [
        (
            '<script>alert("xss")</script>',
            "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;",
        ),
        ("<b>Injected Bold</b>", "&lt;b&gt;Injected Bold&lt;/b&gt;"),
        ("Tom & Jerry", "Tom &amp; Jerry"),
        (
            "Title with \"quotes\" and 'apostrophes'",
            "Title with &quot;quotes&quot; and &#x27;apostrophes&#x27;",
        ),
        (
            '<img src=x onerror="alert(1)">',
            "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;",
        ),
        ("Обычное видео 2025", "Обычное видео 2025"),
        ("🎬 Funny cats 🐱", "🎬 Funny cats 🐱"),
        (
            '<script>alert(1)</script> & "exploit"',
            "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;exploit&quot;",
        ),
    ],
)
async def test_picker_caption_escapes_provider_title(monkeypatch, title, escaped):
    result = ExtractionResult(
        title=title,
        formats=[],
        special_format=None,
        duration_str="3:15",
        is_slideshow=False,
        info_json_path=None,
        thumbnail_url=None,
    )
    monkeypatch.setattr(
        state, "info_cache", SimpleNamespace(get=AsyncMock(return_value=result))
    )
    query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())

    await on_back(
        SimpleNamespace(callback_query=query),
        SimpleNamespace(user_data={"page_url": "https://youtu.be/example"}),
    )

    query.edit_message_text.assert_awaited_once()
    assert query.edit_message_text.await_args.args == (f"📹 <b>{escaped}</b>\n⏱ 3:15",)
    assert query.edit_message_text.await_args.kwargs["parse_mode"] == "HTML"
