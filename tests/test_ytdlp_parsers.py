import unittest
from app.services.ytdlp.parsers import parse_format_metadata, create_format_item
from app.services.ytdlp.models import FormatItem, FormatMetadata

class TestYtDlpParsers(unittest.TestCase):
    def test_parse_format_happy_path(self):
        """Standard video format from YouTube"""
        format_dict = {
            "format_id": "137",
            "ext": "mp4",
            "vcodec": "avc1.640028",
            "height": 1080,
            "filesize": 100 * 1024 * 1024, # 100 MB
            "protocol": "https"
        }
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        is_tiktok = "tiktok.com" in url
        metadata = parse_format_metadata(format_dict, None, is_tiktok)

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.format_id, "137")
        self.assertEqual(metadata.height, 1080)
        self.assertEqual(metadata.filesize, 100 * 1024 * 1024)
        self.assertEqual(metadata.ext, "mp4")

        # Verify label creation separately
        item = create_format_item(metadata, is_tiktok)
        self.assertIn("📺", item.label)
        self.assertIn("100.0 MB", item.label)

    def test_parse_format_filtering_vcodec_none(self):
        """Non-TikTok URLs should filter out audio-only formats"""
        format_dict = {
            "format_id": "140",
            "ext": "m4a",
            "vcodec": "none",
            "protocol": "https"
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
            "height": None
        }
        url = "https://www.tiktok.com/@user/video/123"
        is_tiktok = "tiktok.com" in url
        metadata = parse_format_metadata(format_dict, None, is_tiktok)

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.format_id, "tiktok_fmt")
        self.assertEqual(metadata.height, 720) # Default for TikTok

        item = create_format_item(metadata, is_tiktok)
        self.assertIn("🎵 TikTok", item.label)

    def test_parse_format_unsupported_extension(self):
        """Should return None for unsupported extensions like jpg"""
        format_dict = {
            "format_id": "img",
            "ext": "jpg",
            "protocol": "https"
        }
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
            "height": 720
        }
        url = "https://example.com/video"
        is_tiktok = "tiktok.com" in url
        metadata = parse_format_metadata(format_dict, None, is_tiktok)

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.format_id, "hls_fmt")

        item = create_format_item(metadata, is_tiktok)
        self.assertIn("HLS", item.label)

    def test_parse_format_missing_id(self):
        """Should return None if format_id is missing"""
        format_dict = {
            "ext": "mp4",
            "vcodec": "avc1",
            "height": 720
        }
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
            "protocol": "https"
        }
        url = "https://example.com/video"
        is_tiktok = "tiktok.com" in url
        metadata = parse_format_metadata(format_dict, None, is_tiktok)

        self.assertEqual(metadata.height, 720)

        item = create_format_item(metadata, is_tiktok)
        self.assertIn("📹 720p", item.label)

    def test_parse_format_filesize_approx(self):
        """Should use filesize_approx if filesize is missing"""
        format_dict = {
            "format_id": "fmt_approx",
            "ext": "mp4",
            "vcodec": "avc1",
            "height": 480,
            "filesize_approx": 50 * 1024 * 1024,
            "protocol": "https"
        }
        url = "https://example.com/video"
        is_tiktok = "tiktok.com" in url
        metadata = parse_format_metadata(format_dict, None, is_tiktok)

        self.assertEqual(metadata.filesize, 50 * 1024 * 1024)

        item = create_format_item(metadata, is_tiktok)
        self.assertIn("50.0 MB", item.label)

    def test_parse_format_filesize_from_tbr(self):
        """Should calculate filesize from tbr and duration"""
        format_dict = {
            "format_id": "fmt_tbr",
            "ext": "mp4",
            "vcodec": "avc1",
            "height": 720,
            "tbr": 1000, # 1000 kbps
            "protocol": "https"
        }
        duration_sec = 10.0
        url = "https://example.com/video"
        is_tiktok = "tiktok.com" in url
        metadata = parse_format_metadata(format_dict, duration_sec, is_tiktok)

        # Calculation: (1000 * 1024 / 8) * 10 = 128000 * 10 = 1,280,000 bytes
        expected_size = 1280000
        self.assertEqual(metadata.filesize, expected_size)

        item = create_format_item(metadata, is_tiktok)
        self.assertIn("1.2 MB", item.label)

    def test_parse_format_label_icons(self):
        """Verify correct icons for different heights"""
        # 1080p -> 📺
        m1080 = parse_format_metadata({"format_id": "1", "ext": "mp4", "height": 1080}, None, False)
        item1080 = create_format_item(m1080, False)
        self.assertIn("📺", item1080.label)

        # 720p -> 📹
        m720 = parse_format_metadata({"format_id": "2", "ext": "mp4", "height": 720}, None, False)
        item720 = create_format_item(m720, False)
        self.assertIn("📹", item720.label)

        # 480p -> 📱
        m480 = parse_format_metadata({"format_id": "3", "ext": "mp4", "height": 480}, None, False)
        item480 = create_format_item(m480, False)
        self.assertIn("📱", item480.label)

    def test_parse_format_size_units(self):
        """Verify KB formatting for small files"""
        format_dict = {
            "format_id": "small",
            "ext": "mp4",
            "filesize": 500 * 1024, # 500 KB
            "height": 360
        }
        metadata = parse_format_metadata(format_dict, None, False)
        item = create_format_item(metadata, False)
        self.assertIn("500 KB", item.label)

if __name__ == "__main__":
    unittest.main()
