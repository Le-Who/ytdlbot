from unittest.mock import AsyncMock

"""Tests for /dl endpoint security — verifies REAL download route behavior."""

import unittest


class AsyncMockCache(dict):
    async def get(self, key, default=None):
        return super().get(key, default)

    async def set(self, key, value):
        self[key] = value

    async def delete(self, key):
        self.pop(key, None)


from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router

_app = FastAPI()
_app.include_router(router)


class TestDownloadEndpoint(unittest.TestCase):
    """Test the real /dl/{token} endpoint via TestClient."""

    def setUp(self):
        self.client = TestClient(_app)

    @patch("app.api.routes.state")
    def test_missing_token_returns_404(self, mock_state):
        """Nonexistent token returns 404 from the real endpoint."""
        mock_state.link_cache = AsyncMockCache()
        resp = self.client.get("/dl/nonexistent_token")
        self.assertEqual(resp.status_code, 404)

    @patch("app.api.routes.state")
    def test_rate_limited_ip_returns_429(self, mock_state):
        """Rate-limited IP returns 429."""
        mock_state.link_cache = AsyncMockCache(
            {"valid_token": {"page_url": "http://example.com", "title": "Test"}}
        )
        mock_state.limiter.allow_ip = AsyncMock(return_value=False)
        mock_state.limiter.allow_token = AsyncMock(return_value=True)
        resp = self.client.get("/dl/valid_token")
        self.assertEqual(resp.status_code, 429)

    @patch("app.api.routes.state")
    def test_rate_limited_token_returns_429(self, mock_state):
        """Rate-limited token returns 429."""
        mock_state.link_cache = AsyncMockCache(
            {"valid_token": {"page_url": "http://example.com", "title": "Test"}}
        )
        mock_state.limiter.allow_ip = AsyncMock(return_value=True)
        mock_state.limiter.allow_token = AsyncMock(return_value=False)
        resp = self.client.get("/dl/valid_token")
        self.assertEqual(resp.status_code, 429)


from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.core import state
from app.core.models import DownloadContext


@pytest.mark.parametrize(
    ("title", "filename"),
    [
        (
            "normal_title\r\ninjected_header: bad",
            "normal_titleinjected_header%3A%20bad",
        ),
        ("A" * 500, "A" * 200),
        ("hello\x00world", "helloworld"),
        ("hello\tworld", "helloworld"),
        (
            "Привет 🎬 мир",
            "%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82%20%F0%9F%8E%AC%20%D0%BC%D0%B8%D1%80",
        ),
    ],
)
def test_download_response_sanitizes_filename(monkeypatch, tmp_path, title, filename):
    """Removing route sanitization must change the actual response header."""
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")

    @asynccontextmanager
    async def open_materialized(request):
        yield SimpleNamespace(paths=(path,), size_bytes=5, renew_lease=lambda: None)

    monkeypatch.setattr(
        state,
        "link_cache",
        AsyncMockCache(
            {
                "filename": DownloadContext(
                    page_url="https://youtu.be/example", title=title
                )
            }
        ),
    )
    monkeypatch.setattr(
        state, "media_pipeline", SimpleNamespace(open_materialized=open_materialized)
    )
    monkeypatch.setattr(
        state,
        "limiter",
        SimpleNamespace(
            allow_ip=AsyncMock(return_value=True),
            allow_token=AsyncMock(return_value=True),
        ),
    )
    with TestClient(_app) as client:
        response = client.get("/dl/filename")

    assert response.status_code == 200
    assert response.content == b"video"
    assert response.headers["Content-Disposition"] == (
        f"attachment; filename*=UTF-8''{filename}.mp4"
    )


if __name__ == "__main__":
    unittest.main()
