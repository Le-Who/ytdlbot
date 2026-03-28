"""Tests for app.core.utils — is_supported_url, extract_supported_url, safe_remove."""

import unittest
from unittest.mock import patch

from app.core.utils import is_supported_url, extract_supported_url, safe_remove


class TestSafeRemove(unittest.TestCase):
    @patch("os.path.exists", return_value=True)
    @patch("os.unlink")
    def test_removes_existing_file(self, mock_unlink, _):
        safe_remove("a.txt")
        mock_unlink.assert_called_once_with("a.txt")

    @patch("os.path.exists", return_value=False)
    @patch("os.unlink")
    def test_skips_nonexistent_file(self, mock_unlink, _):
        safe_remove("missing.txt")
        mock_unlink.assert_not_called()

    @patch("os.path.exists", return_value=True)
    @patch("os.unlink", side_effect=OSError("Permission denied"))
    def test_suppresses_os_error(self, mock_unlink, _):
        """OSError during unlink is silently suppressed."""
        safe_remove("locked.txt")  # Should not raise

    def test_empty_string_noop(self):
        """Empty path is a noop (falsy check in code)."""
        safe_remove("")  # Should not raise

    def test_none_noop(self):
        """None path is a noop."""
        safe_remove(None)  # type: ignore


class TestIsSupportedUrl(unittest.TestCase):
    """Test is_supported_url against supported platforms from constants."""

    # ── Positive cases ──
    def test_youtube_full(self):
        self.assertTrue(is_supported_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_youtube_short(self):
        self.assertTrue(is_supported_url("https://youtu.be/dQw4w9WgXcQ"))

    def test_tiktok(self):
        self.assertTrue(is_supported_url("https://www.tiktok.com/@user/video/123"))

    def test_vk(self):
        self.assertTrue(is_supported_url("https://vk.com/video-123_456"))

    def test_vkvideo(self):
        self.assertTrue(is_supported_url("https://vkvideo.ru/video-123_456"))

    def test_rutube(self):
        self.assertTrue(is_supported_url("https://rutube.ru/video/abc123"))

    def test_pinterest(self):
        self.assertTrue(is_supported_url("https://pinterest.com/pin/123456"))

    def test_pin_it(self):
        self.assertTrue(is_supported_url("https://pin.it/abc"))

    def test_facebook(self):
        self.assertTrue(is_supported_url("https://www.facebook.com/watch?v=123"))

    def test_fb_watch(self):
        self.assertTrue(is_supported_url("https://fb.watch/abc"))

    def test_x_com(self):
        self.assertTrue(is_supported_url("https://x.com/i/status/123"))

    def test_twitter_com(self):
        self.assertTrue(is_supported_url("https://twitter.com/user/status/123"))

    # ── Negative cases ──
    def test_unsupported_domain(self):
        self.assertFalse(is_supported_url("https://example.com/video"))

    def test_plain_text(self):
        self.assertFalse(is_supported_url("just some text"))

    def test_empty_string(self):
        self.assertFalse(is_supported_url(""))

    def test_no_scheme(self):
        self.assertFalse(is_supported_url("youtube.com/watch?v=abc"))

    # ── Injection prevention ──
    def test_dash_prefix_rejected(self):
        """URLs starting with '-' are rejected (CLI injection prevention)."""
        self.assertFalse(is_supported_url("-https://youtube.com/watch?v=abc"))

    def test_double_dash_rejected(self):
        self.assertFalse(is_supported_url("--version"))


class TestExtractSupportedUrl(unittest.TestCase):
    """Test extract_supported_url which finds URLs from freeform text."""

    def test_extracts_from_message(self):
        text = "Смотри тут https://youtube.com/watch?v=abc123 классное видео!"
        self.assertEqual(
            extract_supported_url(text),
            "https://youtube.com/watch?v=abc123",
        )

    def test_extracts_tiktok(self):
        text = "https://www.tiktok.com/@user/video/1234567890"
        self.assertEqual(
            extract_supported_url(text),
            "https://www.tiktok.com/@user/video/1234567890",
        )

    def test_strips_trailing_punctuation(self):
        """Trailing punctuation (.!:;) is stripped from extracted URLs."""
        text = "Вот ссылка: https://youtube.com/watch?v=abc."
        result = extract_supported_url(text)
        self.assertIsNotNone(result)
        self.assertFalse(result.endswith("."))

    def test_returns_none_for_no_url(self):
        self.assertIsNone(extract_supported_url("просто текст без ссылок"))

    def test_returns_none_for_unsupported_url(self):
        self.assertIsNone(extract_supported_url("https://example.com/page"))

    def test_returns_none_for_empty(self):
        self.assertIsNone(extract_supported_url(""))

    def test_trailing_parenthesis_stripped(self):
        """Trailing ')' is stripped (common in markdown links)."""
        text = "(https://youtube.com/watch?v=abc)"
        result = extract_supported_url(text)
        self.assertIsNotNone(result)
        self.assertFalse(result.endswith(")"))


if __name__ == "__main__":
    unittest.main()
