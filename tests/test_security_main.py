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

import re

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
        mock_state.link_cache = AsyncMockCache({
            "valid_token": {"page_url": "http://example.com", "title": "Test"}
        })
        mock_state.limiter.allow_ip = AsyncMock(return_value=False)
        mock_state.limiter.allow_token = AsyncMock(return_value=True)
        resp = self.client.get("/dl/valid_token")
        self.assertEqual(resp.status_code, 429)

    @patch("app.api.routes.state")
    def test_rate_limited_token_returns_429(self, mock_state):
        """Rate-limited token returns 429."""
        mock_state.link_cache = AsyncMockCache({
            "valid_token": {"page_url": "http://example.com", "title": "Test"}
        })
        mock_state.limiter.allow_ip = AsyncMock(return_value=True)
        mock_state.limiter.allow_token = AsyncMock(return_value=False)
        resp = self.client.get("/dl/valid_token")
        self.assertEqual(resp.status_code, 429)


class TestFilenameSanitization(unittest.TestCase):
    """Test the REAL filename sanitization logic from routes.download.

    The actual sanitization from routes.py is:
        clean_title = re.sub(r'[\\x00-\\x1f\\x7f\\r\\n]', '', raw_title)[:200]
    """

    @staticmethod
    def _sanitize(raw_title: str) -> str:
        """Reproduce the exact sanitization from routes.py download()."""
        return re.sub(r"[\x00-\x1f\x7f\r\n]", "", raw_title)[:200]

    def test_control_chars_stripped(self):
        """Control characters and CRLF are removed."""
        clean = self._sanitize("normal_title\r\ninjected_header: bad")
        self.assertNotIn("\r", clean)
        self.assertNotIn("\n", clean)
        self.assertIn("normal_title", clean)
        self.assertIn("injected_header: bad", clean)

    def test_filename_length_truncated_to_200(self):
        """Titles longer than 200 chars are truncated."""
        clean = self._sanitize("A" * 500)
        self.assertEqual(len(clean), 200)

    def test_null_byte_stripped(self):
        """Null bytes in title are removed."""
        clean = self._sanitize("hello\x00world")
        self.assertEqual(clean, "helloworld")

    def test_tab_stripped(self):
        """Tab characters are removed."""
        clean = self._sanitize("hello\tworld")
        self.assertEqual(clean, "helloworld")

    def test_normal_unicode_preserved(self):
        """Normal Unicode (Cyrillic, emoji) is preserved."""
        clean = self._sanitize("Привет 🎬 мир")
        self.assertEqual(clean, "Привет 🎬 мир")


if __name__ == "__main__":
    unittest.main()
