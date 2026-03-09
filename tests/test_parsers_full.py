"""Comprehensive tests for app.services.ytdlp.parsers — all public functions."""

import unittest
from app.services.ytdlp.parsers import (
    _is_tiktok,
    _is_youtube,
    _is_pinterest,
    _is_facebook,
    classify_tiktok_content,
    classify_tiktok_error,
    TikTokError,
    _format_duration,
    _extract_height,
    _calculate_filesize,
    parse_format_metadata,
    create_format_item,
    deduplicate_formats,
    get_special_format,
    detect_tiktok_slideshow,
)
from app.services.ytdlp.models import FormatMetadata
from app.constants import GIF_FORMAT_ID, AUDIO_FORMAT_ID

# ── Platform detection ────────────────────────────────────────────


class TestPlatformDetection(unittest.TestCase):
    def test_is_tiktok(self):
        self.assertTrue(_is_tiktok("https://www.tiktok.com/@user/video/123"))
        self.assertFalse(_is_tiktok("https://youtube.com"))

    def test_is_youtube(self):
        self.assertTrue(_is_youtube("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(_is_youtube("https://youtu.be/abc"))
        self.assertFalse(_is_youtube("https://tiktok.com"))

    def test_is_pinterest(self):
        self.assertTrue(_is_pinterest("https://pinterest.com/pin/123"))
        self.assertTrue(_is_pinterest("https://pin.it/abc"))
        self.assertFalse(_is_pinterest("https://youtube.com"))

    def test_is_facebook(self):
        self.assertTrue(_is_facebook("https://facebook.com/watch"))
        self.assertTrue(_is_facebook("https://fb.watch/abc"))
        self.assertFalse(_is_facebook("https://youtube.com"))


# ── TikTok classification ────────────────────────────────────────


class TestClassifyTikTokContent(unittest.TestCase):
    def test_photo_url_is_slideshow(self):
        self.assertEqual(
            classify_tiktok_content("https://tiktok.com/@user/photo/123"),
            "slideshow",
        )

    def test_video_url(self):
        self.assertEqual(
            classify_tiktok_content("https://tiktok.com/@user/video/123"),
            "video",
        )


class TestClassifyTikTokError(unittest.TestCase):
    def test_auth_required_login(self):
        self.assertEqual(
            classify_tiktok_error("Please log in"), TikTokError.AUTH_REQUIRED
        )

    def test_auth_required_cookies(self):
        self.assertEqual(
            classify_tiktok_error("cookies required"), TikTokError.AUTH_REQUIRED
        )

    def test_auth_required_sign_in(self):
        self.assertEqual(
            classify_tiktok_error("Sign in to continue"), TikTokError.AUTH_REQUIRED
        )

    def test_auth_required_not_available(self):
        self.assertEqual(
            classify_tiktok_error("Video not available"), TikTokError.AUTH_REQUIRED
        )

    def test_slideshow(self):
        self.assertEqual(
            classify_tiktok_error("Unsupported URL"), TikTokError.SLIDESHOW
        )

    def test_forbidden(self):
        self.assertEqual(classify_tiktok_error("HTTP Error 403"), TikTokError.FORBIDDEN)

    def test_not_found(self):
        self.assertEqual(classify_tiktok_error("404 Not Found"), TikTokError.NOT_FOUND)

    def test_live(self):
        self.assertEqual(classify_tiktok_error("Live stream"), TikTokError.LIVE)

    def test_generic(self):
        self.assertEqual(
            classify_tiktok_error("some random error"), TikTokError.GENERIC
        )


# ── Duration formatting ──────────────────────────────────────────


class TestFormatDuration(unittest.TestCase):
    def test_none(self):
        self.assertEqual(_format_duration(None), "--:--")

    def test_zero(self):
        self.assertEqual(_format_duration(0), "--:--")

    def test_seconds_only(self):
        self.assertEqual(_format_duration(45), "00:45")

    def test_minutes_and_seconds(self):
        self.assertEqual(_format_duration(125), "02:05")

    def test_hours(self):
        self.assertEqual(_format_duration(3661), "1:01:01")


# ── Height extraction ────────────────────────────────────────────


class TestExtractHeight(unittest.TestCase):
    def test_720p(self):
        self.assertEqual(_extract_height("720p"), 720)

    def test_1080p(self):
        self.assertEqual(_extract_height("1080p"), 1080)

    def test_no_match(self):
        self.assertIsNone(_extract_height("best quality"))

    def test_empty(self):
        self.assertIsNone(_extract_height(""))


# ── Filesize calculation ─────────────────────────────────────────


class TestCalculateFilesize(unittest.TestCase):
    def test_explicit_filesize(self):
        self.assertEqual(_calculate_filesize({"filesize": 5000}, None), 5000)

    def test_filesize_approx(self):
        self.assertEqual(_calculate_filesize({"filesize_approx": 3000}, None), 3000)

    def test_tbr_with_duration(self):
        # tbr * duration_factor
        result = _calculate_filesize({"tbr": 1000}, 10.0)
        self.assertEqual(result, 10000)

    def test_no_data(self):
        self.assertIsNone(_calculate_filesize({}, None))

    def test_tbr_without_duration(self):
        self.assertIsNone(_calculate_filesize({"tbr": 1000}, None))


# ── Format formatter ───────────────────────────────────────────────


class TestFormatFormatter(unittest.TestCase):
    def test_tiktok_label(self):
        from app.bot.format_formatter import format_label
        from app.services.ytdlp.models import FormatItem

        item = FormatItem("id", "mp4", 720, 5_000_000, is_tiktok=True, protocol="https")
        label = format_label(item)
        self.assertIn("TikTok", label)
        self.assertIn("MB", label)

    def test_1080p_label(self):
        from app.bot.format_formatter import format_label
        from app.services.ytdlp.models import FormatItem

        item = FormatItem(
            "id", "mp4", 1080, 50_000_000, is_tiktok=False, protocol="https"
        )
        label = format_label(item)
        self.assertIn("📺", label)
        self.assertIn("1080p", label)

    def test_720p_label(self):
        from app.bot.format_formatter import format_label
        from app.services.ytdlp.models import FormatItem

        item = FormatItem("id", "mp4", 720, None, is_tiktok=False, protocol="https")
        label = format_label(item)
        self.assertIn("📹", label)
        self.assertIn("720p", label)

    def test_480p_label(self):
        from app.bot.format_formatter import format_label
        from app.services.ytdlp.models import FormatItem

        item = FormatItem("id", "mp4", 480, None, is_tiktok=False, protocol="https")
        label = format_label(item)
        self.assertIn("📱", label)

    def test_no_height(self):
        from app.bot.format_formatter import format_label
        from app.services.ytdlp.models import FormatItem

        item = FormatItem("id", "mp4", None, None, is_tiktok=False, protocol="https")
        label = format_label(item)
        self.assertIn("Video", label)

    def test_hls_protocol(self):
        from app.bot.format_formatter import format_label
        from app.services.ytdlp.models import FormatItem

        item = FormatItem("id", "mp4", 720, None, is_tiktok=False, protocol="m3u8")
        label = format_label(item)
        self.assertIn("HLS", label)

    def test_small_filesize_kb(self):
        from app.bot.format_formatter import format_label
        from app.services.ytdlp.models import FormatItem

        item = FormatItem("id", "mp4", 720, 500, is_tiktok=False, protocol="https")
        label = format_label(item)
        self.assertIn("KB", label)


# ── parse_format_metadata ────────────────────────────────────────


class TestParseFormatMetadata(unittest.TestCase):
    def test_valid_mp4_format(self):
        fmt = {
            "format_id": "137",
            "ext": "mp4",
            "vcodec": "avc1",
            "height": 1080,
            "filesize": 50_000_000,
            "protocol": "https",
        }
        result = parse_format_metadata(fmt, None, is_tiktok_url=False)
        self.assertIsNotNone(result)
        self.assertEqual(result.format_id, "137")
        self.assertEqual(result.height, 1080)

    def test_audio_only_skipped_for_non_tiktok(self):
        fmt = {"format_id": "140", "ext": "m4a", "vcodec": "none"}
        result = parse_format_metadata(fmt, None, is_tiktok_url=False)
        self.assertIsNone(result)

    def test_audio_only_kept_for_tiktok(self):
        fmt = {
            "format_id": "140",
            "ext": "mp4",
            "vcodec": "none",
            "height": None,
            "protocol": "https",
        }
        result = parse_format_metadata(fmt, None, is_tiktok_url=True)
        self.assertIsNotNone(result)
        self.assertEqual(result.height, 720)  # TikTok default

    def test_no_format_id_returns_none(self):
        fmt = {"ext": "mp4", "vcodec": "avc1"}
        self.assertIsNone(parse_format_metadata(fmt, None, False))

    def test_facebook_hd_format_id(self):
        fmt = {
            "format_id": "hd",
            "ext": "mp4",
            "vcodec": "avc1",
            "protocol": "https",
            "url": "https://example.com/video.mp4",
        }
        result = parse_format_metadata(fmt, None, False)
        self.assertIsNotNone(result)
        self.assertEqual(result.height, 720)

    def test_facebook_sd_format_id(self):
        fmt = {
            "format_id": "sd",
            "ext": "mp4",
            "vcodec": "avc1",
            "protocol": "https",
            "url": "https://example.com/video.mp4",
        }
        result = parse_format_metadata(fmt, None, False)
        self.assertIsNotNone(result)
        self.assertEqual(result.height, 360)


# ── create_format_item ───────────────────────────────────────────


class TestCreateFormatItem(unittest.TestCase):
    def test_creates_item(self):
        meta = FormatMetadata(
            format_id="137",
            ext="mp4",
            height=1080,
            filesize=50_000_000,
            protocol="https",
        )
        item = create_format_item(meta, is_tiktok=False)
        self.assertEqual(item.format_id, "137")
        self.assertEqual(item.height, 1080)


# ── deduplicate_formats ──────────────────────────────────────────


class TestDeduplicateFormats(unittest.TestCase):
    def test_dedup_by_height(self):
        formats = [
            FormatMetadata("f1", "mp4", 720, 5000, "https"),
            FormatMetadata("f2", "mp4", 720, 8000, "https"),
            FormatMetadata("f3", "mp4", 1080, 10000, "https"),
        ]
        result = deduplicate_formats(formats, is_tiktok_url=False)
        heights = [f.height for f in result]
        self.assertEqual(heights, [720, 1080])

    def test_tiktok_dedup_by_filesize(self):
        formats = [
            FormatMetadata("f1", "mp4", 720, 5000, "https"),
            FormatMetadata("f2", "mp4", 720, 5000, "https"),
            FormatMetadata("f3", "mp4", 720, 8000, "https"),
        ]
        result = deduplicate_formats(formats, is_tiktok_url=True)
        self.assertEqual(len(result), 2)

    def test_no_height_always_kept(self):
        formats = [
            FormatMetadata("f1", "mp4", 0, None, "https"),
            FormatMetadata("f2", "mp4", 0, None, "https"),
        ]
        result = deduplicate_formats(formats, is_tiktok_url=False)
        self.assertEqual(len(result), 2)


# ── get_special_format ───────────────────────────────────────────


class TestGetSpecialFormat(unittest.TestCase):
    def test_pinterest_returns_gif(self):
        item = get_special_format("https://pinterest.com/pin/123")
        self.assertEqual(item.format_id, GIF_FORMAT_ID)
        self.assertEqual(item.format_note, "gif")

    def test_non_pinterest_returns_audio(self):
        item = get_special_format("https://youtube.com/watch?v=abc")
        self.assertEqual(item.format_id, AUDIO_FORMAT_ID)
        self.assertEqual(item.format_note, "audio")


# ── detect_tiktok_slideshow ──────────────────────────────────────


class TestDetectTikTokSlideshow(unittest.TestCase):
    def test_non_tiktok_returns_false(self):
        self.assertFalse(detect_tiktok_slideshow({}, "https://youtube.com"))

    def test_no_formats_returns_true(self):
        self.assertTrue(
            detect_tiktok_slideshow(
                {"formats": []}, "https://tiktok.com/@user/video/123"
            )
        )

    def test_audio_only_returns_true(self):
        info = {"formats": [{"vcodec": "none"}, {"vcodec": "none"}]}
        self.assertTrue(
            detect_tiktok_slideshow(info, "https://tiktok.com/@user/video/123")
        )

    def test_has_video_returns_false(self):
        info = {"formats": [{"vcodec": "avc1"}, {"vcodec": "none"}]}
        self.assertFalse(
            detect_tiktok_slideshow(info, "https://tiktok.com/@user/video/123")
        )


if __name__ == "__main__":
    unittest.main()
