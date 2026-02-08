import unittest
from app.services.ytdlp.parsers import _create_format_label

class TestCreateFormatLabel(unittest.TestCase):
    def test_tiktok_label(self):
        """Test TikTok label generation"""
        # TikTok label should always start with "🎵 TikTok" regardless of height
        filesize = 10 * 1024 * 1024 # 10 MB
        label = _create_format_label(
            height=None,
            filesize=filesize,
            protocol="http",
            is_tiktok=True
        )
        self.assertEqual(label, "🎵 TikTok • 10.0 MB")

    def test_height_icons_non_tiktok(self):
        """Test height-based icons for non-TikTok videos"""
        # >= 1080p -> 📺
        label_1080 = _create_format_label(
            height=1080,
            filesize=None,
            protocol="http",
            is_tiktok=False
        )
        self.assertEqual(label_1080, "📺 1080p")

        # >= 720p -> 📹
        label_720 = _create_format_label(
            height=720,
            filesize=None,
            protocol="http",
            is_tiktok=False
        )
        self.assertEqual(label_720, "📹 720p")

        # < 720p -> 📱
        label_480 = _create_format_label(
            height=480,
            filesize=None,
            protocol="http",
            is_tiktok=False
        )
        self.assertEqual(label_480, "📱 480p")

        # Unknown height -> 📹 ???p
        label_none = _create_format_label(
            height=None,
            filesize=None,
            protocol="http",
            is_tiktok=False
        )
        self.assertEqual(label_none, "📹 ???p")

    def test_filesize_formatting(self):
        """Test filesize formatting (MB vs KB)"""
        # 15 MB
        filesize_mb = 15 * 1024 * 1024
        label_mb = _create_format_label(
            height=720,
            filesize=filesize_mb,
            protocol="http",
            is_tiktok=False
        )
        self.assertEqual(label_mb, "📹 720p • 15.0 MB")

        # 500 KB
        filesize_kb = 500 * 1024
        label_kb = _create_format_label(
            height=720,
            filesize=filesize_kb,
            protocol="http",
            is_tiktok=False
        )
        self.assertEqual(label_kb, "📹 720p • 500 KB")

    def test_protocol_fallback(self):
        """Test protocol-based fallback when filesize is missing"""
        # HLS protocol
        label_hls = _create_format_label(
            height=720,
            filesize=None,
            protocol="m3u8_native",
            is_tiktok=False
        )
        self.assertEqual(label_hls, "📹 720p • HLS")

        # Other protocol -> ?
        label_other = _create_format_label(
            height=720,
            filesize=None,
            protocol="https",
            is_tiktok=False
        )
        self.assertEqual(label_other, "📹 720p")

    def test_combinations(self):
        """Test combinations of parameters"""
        # TikTok with KB size
        label_tiktok_kb = _create_format_label(
            height=1080, # height ignored for icon in tiktok mode
            filesize=100 * 1024, # 100 KB
            protocol="http",
            is_tiktok=True
        )
        self.assertEqual(label_tiktok_kb, "🎵 TikTok • 100 KB")

        # 1080p with HLS
        label_1080_hls = _create_format_label(
            height=1080,
            filesize=None,
            protocol="m3u8",
            is_tiktok=False
        )
        self.assertEqual(label_1080_hls, "📺 1080p • HLS")

if __name__ == "__main__":
    unittest.main()
