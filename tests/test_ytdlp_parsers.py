import unittest
from app.services.ytdlp.parsers import (
    parse_format_metadata,
    create_format_item,
    get_special_format,
    deduplicate_formats,
    _extract_height,
    _format_duration,
    BITRATE_COEFFICIENT,
)
from app.services.ytdlp.models import FormatMetadata
from app.bot.format_formatter import format_label
from app.constants import GIF_FORMAT_ID, AUDIO_FORMAT_ID


class TestYtDlpParsers(unittest.TestCase):
    def test_format_duration(self):
        """Test duration formatting (HH:MM:SS or MM:SS)"""
        # None or 0 cases
        self.assertEqual(_format_duration(None), "--:--")
        self.assertEqual(_format_duration(0), "--:--")
        self.assertEqual(_format_duration(0.0), "--:--")

        # Seconds only (< 60)
        self.assertEqual(_format_duration(59), "00:59")
        self.assertEqual(_format_duration(5), "00:05")

        # Minutes and seconds (>= 60, < 3600)
        self.assertEqual(_format_duration(60), "01:00")
        self.assertEqual(_format_duration(61), "01:01")
        self.assertEqual(_format_duration(3599), "59:59")

        # Hours, minutes, and seconds (>= 3600)
        self.assertEqual(_format_duration(3600), "1:00:00")
        self.assertEqual(_format_duration(3661), "1:01:01")
        self.assertEqual(_format_duration(7322), "2:02:02")

        # Float input (should be truncated/converted to int)
        self.assertEqual(_format_duration(123.45), "02:03")
        self.assertEqual(_format_duration(123.99), "02:03")

    def test_parse_format_happy_path(self):
        """Standard video format from YouTube"""
        format_dict = {
            "format_id": "137",
            "ext": "mp4",
            "vcodec": "avc1.640028",
            "height": 1080,
            "filesize": 100 * 1024 * 1024,  # 100 MB
            "protocol": "https",
        }
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        is_tiktok = "tiktok.com" in url
        metadata = parse_format_metadata(format_dict, None, is_tiktok)

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.format_id, "137")
        self.assertEqual(metadata.height, 1080)
        self.assertEqual(metadata.filesize, 100 * 1024 * 1024)
        self.assertEqual(metadata.ext, "mp4")

        # Verify label creation via formatter
        item = create_format_item(metadata, is_tiktok)
        label = format_label(item)
        self.assertIn("📺", label)
        self.assertIn("100.0 MB", label)

    def test_parse_format_filtering_vcodec_none(self):
        """Non-TikTok URLs should filter out audio-only formats"""
        format_dict = {
            "format_id": "140",
            "ext": "m4a",
            "vcodec": "none",
            "protocol": "https",
        }
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        is_tiktok = "tiktok.com" in url
        result = parse_format_metadata(format_dict, None, is_tiktok)
        self.assertIsNone(result)

    def test_parse_format_tiktok_vcodec_none(self):
        """TikTok URLs should NOT filter out vcodec=none formats"""
        format_dict = {
            "format_id": "tiktok_fmt",
            "ext": "mp4",
            "vcodec": "none",
            "protocol": "https",
            "height": None,
        }
        url = "https://www.tiktok.com/@user/video/123"
        is_tiktok = "tiktok.com" in url
        metadata = parse_format_metadata(format_dict, None, is_tiktok)

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.format_id, "tiktok_fmt")
        self.assertEqual(metadata.height, 720)  # Default for TikTok

        item = create_format_item(metadata, is_tiktok)
        label = format_label(item)
        self.assertIn("🎵 TikTok", label)

    def test_parse_format_unsupported_extension(self):
        """Should return None for unsupported extensions like jpg"""
        format_dict = {"format_id": "img", "ext": "jpg", "protocol": "https"}
        url = "https://example.com/video"
        is_tiktok = "tiktok.com" in url
        result = parse_format_metadata(format_dict, None, is_tiktok)
        self.assertIsNone(result)

    def test_parse_format_hls_protocol(self):
        """Should accept HLS protocol even with unknown extension"""
        format_dict = {
            "format_id": "hls_fmt",
            "ext": "m3u8",
            "protocol": "m3u8_native",
            "height": 720,
        }
        url = "https://example.com/video"
        is_tiktok = "tiktok.com" in url
        metadata = parse_format_metadata(format_dict, None, is_tiktok)

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.format_id, "hls_fmt")

        item = create_format_item(metadata, is_tiktok)
        label = format_label(item)
        self.assertIn("HLS", label)

    def test_parse_format_missing_id(self):
        """Should return None if format_id is missing"""
        format_dict = {"ext": "mp4", "vcodec": "avc1", "height": 720}
        url = "https://example.com/video"
        is_tiktok = "tiktok.com" in url
        result = parse_format_metadata(format_dict, None, is_tiktok)
        self.assertIsNone(result)

    def test_parse_format_extract_height_from_note(self):
        """Should extract height from format_note if height field is missing"""
        format_dict = {
            "format_id": "fmt_note",
            "ext": "mp4",
            "vcodec": "avc1",
            "format_note": "720p HD",
            "protocol": "https",
        }
        url = "https://example.com/video"
        is_tiktok = "tiktok.com" in url
        metadata = parse_format_metadata(format_dict, None, is_tiktok)

        self.assertEqual(metadata.height, 720)

        item = create_format_item(metadata, is_tiktok)
        label = format_label(item)
        self.assertIn("📹 720p", label)

    def test_parse_format_filesize_approx(self):
        """Should use filesize_approx if filesize is missing"""
        format_dict = {
            "format_id": "fmt_approx",
            "ext": "mp4",
            "vcodec": "avc1",
            "height": 480,
            "filesize_approx": 50 * 1024 * 1024,
            "protocol": "https",
        }
        url = "https://example.com/video"
        is_tiktok = "tiktok.com" in url
        metadata = parse_format_metadata(format_dict, None, is_tiktok)

        self.assertEqual(metadata.filesize, 50 * 1024 * 1024)

        item = create_format_item(metadata, is_tiktok)
        label = format_label(item)
        self.assertIn("50.0 MB", label)

    def test_parse_format_filesize_from_tbr(self):
        """Should calculate filesize from tbr and duration factor"""
        format_dict = {
            "format_id": "fmt_tbr",
            "ext": "mp4",
            "vcodec": "avc1",
            "height": 720,
            "tbr": 1000,  # 1000 kbps
            "protocol": "https",
        }
        duration_sec = 10.0
        duration_factor = duration_sec * BITRATE_COEFFICIENT
        url = "https://example.com/video"
        is_tiktok = "tiktok.com" in url
        metadata = parse_format_metadata(format_dict, duration_factor, is_tiktok)

        # Calculation: (1000 * 1024 / 8) * 10 = 128000 * 10 = 1,280,000 bytes
        expected_size = 1280000
        self.assertEqual(metadata.filesize, expected_size)

        item = create_format_item(metadata, is_tiktok)
        label = format_label(item)
        self.assertIn("1.2 MB", label)

    def test_parse_format_size_units(self):
        """Verify KB formatting for small files"""
        format_dict = {
            "format_id": "small",
            "ext": "mp4",
            "filesize": 500 * 1024,  # 500 KB
            "height": 360,
        }
        metadata = parse_format_metadata(format_dict, None, False)
        item = create_format_item(metadata, False)
        label = format_label(item)
        self.assertIn("500 KB", label)


class TestDeduplicateFormats(unittest.TestCase):
    def test_deduplicate_formats_tiktok(self):
        """TikTok formats should not be deduplicated"""
        formats = [
            FormatMetadata(
                format_id="1", ext="mp4", height=720, filesize=100, protocol="https"
            ),
            FormatMetadata(
                format_id="2", ext="mp4", height=720, filesize=200, protocol="https"
            ),
        ]
        result = deduplicate_formats(formats, is_tiktok_url=True)
        self.assertEqual(len(result), 2)
        self.assertEqual(result, formats)

    def test_deduplicate_formats_standard(self):
        """Standard deduplication based on height"""
        formats = [
            FormatMetadata(
                format_id="1", ext="mp4", height=1080, filesize=100, protocol="https"
            ),
            FormatMetadata(
                format_id="2", ext="mp4", height=1080, filesize=200, protocol="https"
            ),  # Duplicate height
            FormatMetadata(
                format_id="3", ext="mp4", height=720, filesize=300, protocol="https"
            ),
        ]
        result = deduplicate_formats(formats, is_tiktok_url=False)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].format_id, "1")
        self.assertEqual(result[1].format_id, "3")

    def test_deduplicate_formats_none_height(self):
        """Formats with None height should all be kept"""
        formats = [
            FormatMetadata(
                format_id="1", ext="mp4", height=None, filesize=100, protocol="https"
            ),
            FormatMetadata(
                format_id="2", ext="mp4", height=None, filesize=200, protocol="https"
            ),
            FormatMetadata(
                format_id="3", ext="mp4", height=720, filesize=300, protocol="https"
            ),
        ]
        result = deduplicate_formats(formats, is_tiktok_url=False)
        self.assertEqual(len(result), 3)

    def test_deduplicate_formats_mixed(self):
        """Mixed scenario with duplicates and None heights"""
        formats = [
            FormatMetadata(
                format_id="1", ext="mp4", height=1080, filesize=100, protocol="https"
            ),
            FormatMetadata(
                format_id="2", ext="mp4", height=1080, filesize=200, protocol="https"
            ),  # Dup
            FormatMetadata(
                format_id="3", ext="mp4", height=None, filesize=300, protocol="https"
            ),
            FormatMetadata(
                format_id="4", ext="mp4", height=None, filesize=400, protocol="https"
            ),
            FormatMetadata(
                format_id="5", ext="mp4", height=720, filesize=500, protocol="https"
            ),
        ]
        result = deduplicate_formats(formats, is_tiktok_url=False)
        self.assertEqual(len(result), 4)
        ids = [f.format_id for f in result]
        self.assertEqual(ids, ["1", "3", "4", "5"])


class TestGetSpecialFormat(unittest.TestCase):
    def test_get_special_format_pinterest(self):
        """Should return GIF format for Pinterest URLs"""
        urls = [
            "https://www.pinterest.com/pin/123456789/",
            "https://pin.it/1234567",
            "https://www.Pinterest.com/pin/123/",
            "http://pin.it/abc",
        ]
        for url in urls:
            with self.subTest(url=url):
                item = get_special_format(url)
                self.assertEqual(item.format_id, GIF_FORMAT_ID)
                self.assertEqual(item.format_note, "gif")
                self.assertEqual(item.ext, "gif")
                self.assertIsNone(item.height)
                self.assertIsNone(item.filesize)

    def test_get_special_format_default(self):
        """Should return Audio format for non-Pinterest URLs"""
        urls = [
            "https://www.youtube.com/watch?v=123",
            "https://youtu.be/123",
            "https://www.tiktok.com/@user/video/123",
            "https://example.com/video",
            "",
        ]
        for url in urls:
            with self.subTest(url=url):
                item = get_special_format(url)
                self.assertEqual(item.format_id, AUDIO_FORMAT_ID)
                self.assertEqual(item.format_note, "audio")
                self.assertEqual(item.ext, "audio")
                self.assertIsNone(item.height)
                self.assertIsNone(item.filesize)


class TestExtractHeight(unittest.TestCase):
    def test_extract_height_valid(self):
        """Should extract height from standard format notes"""
        self.assertEqual(_extract_height("720p"), 720)
        self.assertEqual(_extract_height("1080p"), 1080)
        self.assertEqual(_extract_height("480p"), 480)
        self.assertEqual(_extract_height("2160p"), 2160)

    def test_extract_height_with_text(self):
        """Should extract height when surrounded by other text"""
        self.assertEqual(_extract_height("720p HD"), 720)
        self.assertEqual(_extract_height("Quality: 480p"), 480)
        self.assertEqual(_extract_height("video 1080p60"), 1080)
        self.assertEqual(_extract_height("720p, 30fps"), 720)

    def test_extract_height_no_match(self):
        """Should return None for strings without valid height pattern"""
        self.assertIsNone(_extract_height("audio"))
        self.assertIsNone(_extract_height("unknown"))
        self.assertIsNone(_extract_height("p720"))  # Pattern expects digits then p
        self.assertIsNone(_extract_height("1080"))  # Missing p
        self.assertIsNone(_extract_height("myp"))

    def test_extract_height_none_empty(self):
        """Should handle None and empty strings gracefully"""
        self.assertIsNone(_extract_height(None))
        self.assertIsNone(_extract_height(""))

    def test_extract_height_zero(self):
        """Should handle 0p edge case"""
        self.assertEqual(_extract_height("0p"), 0)


class TestFacebookFormats(unittest.TestCase):
    """Tests for Facebook-specific format handling."""

    def test_facebook_sd_format(self):
        """Facebook SD format: format_id='sd', no ext, no height → 360p."""
        fmt = {
            "format_id": "sd",
            "quality": -3,
            "url": "https://video.xx.fbcdn.net/sd.mp4",
        }
        metadata = parse_format_metadata(fmt, None, False)

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.height, 360)
        self.assertEqual(metadata.ext, "mp4")
        self.assertIsNone(metadata.filesize)

        item = create_format_item(metadata, False)
        label = format_label(item)
        self.assertIn("📱", label)
        self.assertIn("360p", label)

    def test_facebook_hd_format(self):
        """Facebook HD format: format_id='hd', no ext, no height → 720p."""
        fmt = {
            "format_id": "hd",
            "quality": -2,
            "url": "https://video.xx.fbcdn.net/hd.mp4",
        }
        metadata = parse_format_metadata(fmt, None, False)

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.height, 720)
        self.assertEqual(metadata.ext, "mp4")

        item = create_format_item(metadata, False)
        label = format_label(item)
        self.assertIn("📹", label)
        self.assertIn("720p", label)

    def test_facebook_legacy_format_ids(self):
        """Facebook legacy format_id patterns with _sd/_hd suffixes."""
        sd_fmt = {"format_id": "progressive_sd_src", "url": "https://example.com/sd"}
        hd_fmt = {"format_id": "progressive_hd_src", "url": "https://example.com/hd"}

        sd = parse_format_metadata(sd_fmt, None, False)
        hd = parse_format_metadata(hd_fmt, None, False)

        self.assertEqual(sd.height, 360)
        self.assertEqual(hd.height, 720)

    def test_facebook_format_without_ext_accepted(self):
        """Format without ext but with URL should be accepted (not filtered)."""
        fmt = {"format_id": "sd", "url": "https://video.xx.fbcdn.net/v.mp4"}
        metadata = parse_format_metadata(fmt, None, False)
        self.assertIsNotNone(metadata)

    def test_format_without_ext_and_url_rejected(self):
        """Format without ext AND without URL should still be rejected."""
        fmt = {"format_id": "bad", "ext": "jpg"}
        metadata = parse_format_metadata(fmt, None, False)
        self.assertIsNone(metadata)

    def test_fallback_label_no_height(self):
        """Format with no height should show 'Video' not '???p'."""
        fmt = {"format_id": "unknown_fmt", "ext": "mp4", "height": None}
        metadata = parse_format_metadata(fmt, None, False)
        self.assertIsNotNone(metadata)

        item = create_format_item(metadata, False)
        label = format_label(item)
        self.assertIn("📹 Video", label)
        self.assertNotIn("???", label)

    def test_facebook_dedup_keeps_both_sd_hd(self):
        """Facebook SD and HD should not be deduped (different heights)."""
        formats = [
            FormatMetadata(
                format_id="sd", ext="mp4", height=360, filesize=None, protocol=""
            ),
            FormatMetadata(
                format_id="hd", ext="mp4", height=720, filesize=None, protocol=""
            ),
        ]
        result = deduplicate_formats(formats, is_tiktok_url=False)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
