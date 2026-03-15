import unittest
from app.services.ytdlp.parsers import classify_tiktok_content

class TestTikTokContentClassification(unittest.TestCase):
    def test_classify_tiktok_content(self):
        test_cases = [
            # Standard video URLs
            ("https://www.tiktok.com/@user/video/123", "video"),
            ("http://www.tiktok.com/@user/video/123", "video"),
            ("www.tiktok.com/@user/video/123", "video"),

            # Slideshow / Photo URLs
            ("https://www.tiktok.com/@user/photo/456", "slideshow"),
            ("https://www.tiktok.com/@user/photo/456?is_from_webapp=1", "slideshow"),

            # Case sensitivity
            ("https://www.tiktok.com/@user/PHOTO/789", "slideshow"),
            ("https://www.tiktok.com/@user/Photo/789", "slideshow"),

            # Short URLs
            ("https://vm.tiktok.com/ZM6abc123/", "video"),
            ("https://vt.tiktok.com/ZM6abc123/", "video"),
            ("https://www.tiktok.com/t/ZPRabc123/", "video"),

            # Edge cases and non-TikTok URLs
            ("https://example.com/video", "video"),
            ("https://example.com/photo/123", "slideshow"), # Logic is currently domain-agnostic
            ("", "video"),
            ("photo", "video"), # Only matches "/photo/"
            ("/photo/", "slideshow"),
        ]

        for url, expected in test_cases:
            with self.subTest(url=url):
                self.assertEqual(classify_tiktok_content(url), expected)

if __name__ == "__main__":
    unittest.main()
