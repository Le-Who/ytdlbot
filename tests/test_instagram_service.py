"""Tests for Instagram service — URL parsing, profile metadata, downloads."""

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import curl_cffi  # noqa: F401

    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


class TestInstagramUrlParsing(unittest.TestCase):
    """Test parse_instagram_url for various Instagram URL formats."""

    def test_profile_url(self):
        from app.services.instagram import parse_instagram_url

        t, user, item = parse_instagram_url("https://www.instagram.com/natgeo/")
        self.assertEqual(t, "profile")
        self.assertEqual(user, "natgeo")
        self.assertIsNone(item)

    def test_profile_url_without_trailing_slash(self):
        from app.services.instagram import parse_instagram_url

        t, user, _ = parse_instagram_url("https://instagram.com/elonmusk")
        self.assertEqual(t, "profile")
        self.assertEqual(user, "elonmusk")

    def test_profile_url_with_at_sign(self):
        from app.services.instagram import parse_instagram_url

        t, user, _ = parse_instagram_url("https://instagram.com/@some_user")
        self.assertEqual(t, "profile")
        self.assertEqual(user, "some_user")

    def test_stories_url_with_id(self):
        from app.services.instagram import parse_instagram_url

        t, user, item = parse_instagram_url(
            "https://www.instagram.com/stories/natgeo/3123456789012345678/"
        )
        self.assertEqual(t, "stories")
        self.assertEqual(user, "natgeo")
        self.assertEqual(item, "3123456789012345678")

    def test_stories_url_without_id(self):
        from app.services.instagram import parse_instagram_url

        t, user, item = parse_instagram_url("https://www.instagram.com/stories/natgeo/")
        self.assertEqual(t, "stories")
        self.assertEqual(user, "natgeo")
        self.assertIsNone(item)

    def test_post_url(self):
        from app.services.instagram import parse_instagram_url

        t, shortcode, _ = parse_instagram_url(
            "https://www.instagram.com/p/CxYz1234AbC/"
        )
        self.assertEqual(t, "post")
        self.assertEqual(shortcode, "CxYz1234AbC")

    def test_reel_url(self):
        from app.services.instagram import parse_instagram_url

        t, shortcode, _ = parse_instagram_url(
            "https://www.instagram.com/reel/CxYz1234AbC/"
        )
        self.assertEqual(t, "post")
        self.assertEqual(shortcode, "CxYz1234AbC")

    def test_reels_url(self):
        from app.services.instagram import parse_instagram_url

        t, shortcode, _ = parse_instagram_url(
            "https://www.instagram.com/reels/CxYz1234AbC/"
        )
        self.assertEqual(t, "post")
        self.assertEqual(shortcode, "CxYz1234AbC")

    def test_unknown_url_for_reserved_path(self):
        from app.services.instagram import parse_instagram_url

        t, _, _ = parse_instagram_url("https://www.instagram.com/explore/")
        self.assertEqual(t, "unknown")

    def test_unknown_url_for_accounts(self):
        from app.services.instagram import parse_instagram_url

        t, _, _ = parse_instagram_url("https://www.instagram.com/accounts/login/")
        self.assertEqual(t, "unknown")

    def test_profile_with_query_params(self):
        from app.services.instagram import parse_instagram_url

        t, user, _ = parse_instagram_url("https://instagram.com/user123?igsh=abc123")
        self.assertEqual(t, "profile")
        self.assertEqual(user, "user123")


class TestIsInstagramUrl(unittest.TestCase):
    """Test is_instagram_url helper."""

    def test_instagram_com(self):
        from app.services.instagram import is_instagram_url

        self.assertTrue(is_instagram_url("https://www.instagram.com/natgeo"))

    def test_instagr_am(self):
        from app.services.instagram import is_instagram_url

        self.assertTrue(is_instagram_url("https://instagr.am/p/ABC123"))

    def test_non_instagram(self):
        from app.services.instagram import is_instagram_url

        self.assertFalse(is_instagram_url("https://tiktok.com/@user"))

    def test_non_instagram_containing_word(self):
        from app.services.instagram import is_instagram_url

        self.assertFalse(is_instagram_url("https://notinstagram.xyz/page"))


class TestIGStoryItem(unittest.TestCase):
    """Test IGStoryItem dataclass properties."""

    def test_label_for_video(self):
        from app.services.instagram import IGStoryItem

        item = IGStoryItem(
            mediaid="123",
            is_video=True,
            url="https://example.com/video.mp4",
            thumbnail_url="https://example.com/thumb.jpg",
            timestamp=datetime(2026, 3, 25, 12, 30, tzinfo=timezone.utc),
            duration=15.0,
        )
        label = item.label
        self.assertIn("🎬", label)
        self.assertIn("25.03", label)
        self.assertIn("12:30", label)
        self.assertIn("(15с)", label)

    def test_label_for_photo(self):
        from app.services.instagram import IGStoryItem

        item = IGStoryItem(
            mediaid="456",
            is_video=False,
            url="https://example.com/photo.jpg",
            thumbnail_url="https://example.com/thumb.jpg",
            timestamp=datetime(2026, 3, 25, 14, 0, tzinfo=timezone.utc),
        )
        label = item.label
        self.assertIn("📸", label)
        self.assertIn("14:00", label)
        self.assertNotIn("(", label)  # no duration for photos

    def test_type_emoji(self):
        from app.services.instagram import IGStoryItem

        v = IGStoryItem(
            mediaid="1",
            is_video=True,
            url="",
            thumbnail_url="",
            timestamp=datetime.now(timezone.utc),
        )
        p = IGStoryItem(
            mediaid="2",
            is_video=False,
            url="",
            thumbnail_url="",
            timestamp=datetime.now(timezone.utc),
        )
        self.assertEqual(v.type_emoji, "🎬")
        self.assertEqual(p.type_emoji, "📸")


class TestIGHighlight(unittest.TestCase):
    """Test IGHighlight dataclass."""

    def test_label(self):
        from app.services.instagram import IGHighlight

        hl = IGHighlight(
            highlight_id="hl_123", title="Travel", cover_url="", item_count=5
        )
        self.assertEqual(hl.label, "📁 Travel (5)")


class TestInstagramServiceGetProfile(unittest.IsolatedAsyncioTestCase):
    """Test InstagramService.get_profile_media with mocked curl_cffi."""

    async def test_returns_error_for_nonexistent_profile(self):
        from app.services.instagram import InstagramService

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("app.services.instagram.AsyncSession", return_value=mock_session):
            result = await InstagramService.get_profile_media("nonexistent_user_xyz")

        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertIn("не найден", result.error)


@unittest.skipUnless(HAS_CURL_CFFI, "curl_cffi not installed")
class TestInstagramServiceDownloadStoryItem(unittest.IsolatedAsyncioTestCase):
    """Test InstagramService.download_story_item with mocked curl_cffi."""

    async def test_download_streams_correctly(self):
        import tempfile
        import os
        from app.services.instagram import InstagramService, IGStoryItem

        tmpdir = tempfile.mkdtemp()
        chunks = [b"video_part1", b"video_part2"]

        item = IGStoryItem(
            mediaid="999",
            is_video=True,
            url="https://scontent.cdninstagram.com/v/t123.mp4",
            thumbnail_url="https://scontent.cdninstagram.com/v/thumb.jpg",
            timestamp=datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc),
            duration=10.0,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _aiter():
            for c in chunks:
                yield c

        mock_resp.aiter_content = _aiter

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with (
            patch("app.services.instagram.TEMP_DIR", tmpdir),
            patch("app.services.instagram.AsyncSession", return_value=mock_session),
            patch("app.services.instagram.InstagramService._get_available_session", AsyncMock(return_value=(-1, None)))
        ):
            path, error = await InstagramService.download_story_item(item)

        self.assertIsNone(error)
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(os.path.exists(path))

        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"video_part1video_part2")

        os.unlink(path)
        os.rmdir(tmpdir)

    async def test_download_returns_error_on_http_failure(self):
        from app.services.instagram import InstagramService, IGStoryItem

        item = IGStoryItem(
            mediaid="bad",
            is_video=False,
            url="https://scontent.cdninstagram.com/v/gone.jpg",
            thumbnail_url="",
            timestamp=datetime(2026, 3, 25, 10, 0, tzinfo=timezone.utc),
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with (
            patch("app.services.instagram.AsyncSession", return_value=mock_session),
            patch("app.services.instagram.InstagramService._get_available_session", AsyncMock(return_value=(-1, None)))
        ):
            path, error = await InstagramService.download_story_item(item)

        self.assertIsNone(path)
        assert error is not None
        self.assertIn("404", error)


if __name__ == "__main__":
    unittest.main()
