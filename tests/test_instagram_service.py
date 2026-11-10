"""Tests for Instagram service — URL parsing, profile metadata, downloads."""

import json
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import curl_cffi  # noqa: F401

    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


class TestInstagramUrlParsing(unittest.TestCase):
    """Test parse_instagram_url for various Instagram URL formats."""

    def test_highlight_share_without_item_opens_highlight(self):
        from app.services.instagram import parse_instagram_url

        self.assertEqual(
            parse_instagram_url("https://www.instagram.com/s/aGlnaGxpZ2h0Ojc/"),
            ("highlight", "7", None),
        )

    def test_highlight_share_requires_an_instagram_host_and_numeric_highlight(self):
        from app.services.instagram import parse_instagram_url

        for url in (
            "https://instagram.com.example/s/aGlnaGxpZ2h0Ojc=",
            "https://www.instagram.com/s/aGlnaGxpZ2h0Ong=",
        ):
            self.assertEqual(parse_instagram_url(url), ("unknown", None, None))

    def test_profile_url(self):
        from app.services.instagram import parse_instagram_url

        t, user, item = parse_instagram_url("https://www.instagram.com/natgeo/")
        self.assertEqual(t, "profile")
        self.assertEqual(user, "natgeo")
        self.assertIsNone(item)

    def test_story_url_uses_host_and_path_instead_of_query_or_fragment(self):
        from app.services.instagram import parse_instagram_url

        self.assertEqual(
            parse_instagram_url(
                "https://WWW.INSTAGRAM.COM/stories/tester/41/#fragment"
            ),
            ("stories", "tester", "41"),
        )
        self.assertEqual(
            parse_instagram_url(
                "https://www.instagram.com/tester/?next=https://www.instagram.com/stories/other/41/"
            ),
            ("profile", "tester", None),
        )

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


class TestIGStoryItem(unittest.TestCase):
    """Test IGStoryItem dataclass properties."""

    def test_label_for_video(self):
        from app.services.instagram import IGStoryItem

        item = IGStoryItem(
            mediaid="123",
            is_video=True,
            url="https://example.com/video.mp4",
            thumbnail_url="https://example.com/thumb.jpg",
            timestamp=datetime(2026, 3, 25, 12, 30, tzinfo=UTC),
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
            timestamp=datetime(2026, 3, 25, 14, 0, tzinfo=UTC),
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
            timestamp=datetime.now(UTC),
        )
        p = IGStoryItem(
            mediaid="2",
            is_video=False,
            url="",
            thumbnail_url="",
            timestamp=datetime.now(UTC),
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

    async def test_profile_web_requests_return_active_stories_and_highlights(self):
        from curl_cffi.requests import Response

        from app.services.instagram import InstagramService

        def graph(field, node):
            response = Response()
            response.status_code = 200
            response.content = json.dumps(
                {"data": {field: {"edges": [{"node": node}]}}}
            ).encode()
            return response

        page = Response()
        page.status_code = 200
        page.url = "https://www.instagram.com/tester/"
        page.content = b'["LSD",[],{"token":"lsd-test"}] {"profile_id":"12"}'
        story = {
            "pk": "41",
            "media_type": 1,
            "image_versions2": {
                "candidates": [{"url": "https://cdn.example/story.jpg"}]
            },
        }
        highlight = {
            "id": "highlight:7",
            "title": "Travel",
            "cover_media": {
                "cropped_image_version": {"url": "https://cdn.example/cover.jpg"}
            },
        }
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.cookies = {}

        async def get(url, **kwargs):
            if url == page.url:
                return page
            unavailable = Response()
            unavailable.status_code = 404
            unavailable.ok = False
            return unavailable

        async def post(url, *, data, **kwargs):
            if (
                data["fb_api_req_friendly_name"]
                == "PolarisStoriesV3HighlightsPageQuery"
            ):
                return graph(
                    "xdt_api__v1__feed__reels_media__connection",
                    {"id": "12", "items": [story]},
                )
            return graph("highlights", highlight)

        session.get.side_effect = get
        session.post.side_effect = post
        with (
            patch.object(
                InstagramService,
                "_get_available_session",
                return_value=(0, {"sessionid": "test"}),
            ),
            patch("app.services.instagram.AsyncSession", return_value=session),
        ):
            result = await InstagramService.get_profile_media("tester")
        self.assertIsNone(result.error)
        self.assertEqual([item.mediaid for item in result.stories], ["41"])
        self.assertEqual([item.highlight_id for item in result.highlights], ["7"])


@unittest.skipUnless(HAS_CURL_CFFI, "curl_cffi not installed")
class TestInstagramServiceGetHighlight(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_highlight_through_authenticated_web_api(self):
        from curl_cffi.requests import Response

        from app.services.instagram import InstagramService

        page = Response()
        page.status_code = 200
        page.url = "https://www.instagram.com/stories/highlights/7/"
        page.content = b'["LSD",[],{"token":"lsd-test"}]'
        response = Response()
        response.status_code = 200
        response.content = b"""{"data":{"xdt_api__v1__feed__reels_media__connection":{"edges":[
          {"node":{"id":"highlight:7","items":[{"pk":"41","media_type":2,
          "video_versions":[{"url":"https://cdn.example/41.mp4"}],
          "video_duration":20,"taken_at":1794283200}]}}
        ]}}}"""
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.cookies = {}
        consent = Response()
        consent.status_code = 302
        consent.headers["Location"] = "https://www.instagram.com/consent/?next=opaque"
        session.get.side_effect = [consent, page, page]
        session.post.return_value = response

        with (
            patch.object(InstagramService, "_init_pool"),
            patch.object(InstagramService, "_dead_sessions", set()),
            patch.object(
                InstagramService,
                "_get_available_session",
                side_effect=[
                    (0, {"sessionid": "needs-consent"}),
                    (1, {"sessionid": "valid"}),
                    (1, {"sessionid": "valid"}),
                ],
            ),
            patch("app.services.instagram.AsyncSession", return_value=session),
        ):
            items, error = await InstagramService.get_highlight_items("7")
            self.assertIsNone(error)
            self.assertEqual(
                [(i.mediaid, i.url) for i in items],
                [("41", "https://cdn.example/41.mp4")],
            )

            response.status_code = 429
            response.ok = False
            items, error = await InstagramService.get_highlight_items("7")
            self.assertEqual(items, [])
            self.assertIn("429", error or "")


@unittest.skipUnless(HAS_CURL_CFFI, "curl_cffi not installed")
class TestInstagramServiceDownloadStoryItem(unittest.IsolatedAsyncioTestCase):
    """Test InstagramService.download_story_item with mocked curl_cffi."""

    async def test_download_streams_correctly(self):
        import os
        import tempfile

        from app.services.instagram import IGStoryItem, InstagramService

        tmpdir = tempfile.mkdtemp()
        chunks = [b"video_part1", b"video_part2"]

        item = IGStoryItem(
            mediaid="999",
            is_video=True,
            url="https://scontent.cdninstagram.com/v/t123.mp4",
            thumbnail_url="https://scontent.cdninstagram.com/v/thumb.jpg",
            timestamp=datetime(2026, 3, 25, 10, 0, tzinfo=UTC),
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
            patch(
                "app.services.instagram.InstagramService._get_available_session",
                AsyncMock(return_value=(-1, None)),
            ),
        ):
            path, error = await InstagramService.download_story_item(item)

        self.assertIsNone(error)
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(os.path.exists(path))

        # Small synthetic fixture I/O completes inline; no runtime worker ownership.

        with open(path, "rb") as f:  # noqa: ASYNC230
            self.assertEqual(f.read(), b"video_part1video_part2")

        os.unlink(path)
        os.rmdir(tmpdir)

    async def test_download_returns_error_on_http_failure(self):
        from app.services.instagram import IGStoryItem, InstagramService

        item = IGStoryItem(
            mediaid="bad",
            is_video=False,
            url="https://scontent.cdninstagram.com/v/gone.jpg",
            thumbnail_url="",
            timestamp=datetime(2026, 3, 25, 10, 0, tzinfo=UTC),
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with (
            patch("app.services.instagram.AsyncSession", return_value=mock_session),
            patch(
                "app.services.instagram.InstagramService._get_available_session",
                AsyncMock(return_value=(-1, None)),
            ),
        ):
            path, error = await InstagramService.download_story_item(item)

        self.assertIsNone(path)
        assert error is not None
        self.assertIn("404", error)


if __name__ == "__main__":
    unittest.main()
