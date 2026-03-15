import unittest
from app.services.ytdlp.exceptions import (
    map_ytdlp_error,
    AccessDeniedError,
    VideoNotFoundError,
    LiveStreamError,
    ExtractionError,
)
from app.core.texts import Texts


class TestYtDlpExceptions(unittest.TestCase):
    def test_access_denied_mapping(self):
        url = "https://example.com"
        test_cases = [
            "HTTP Error 403: Forbidden",
            "Status code 401",
            "This video is forbidden",
            "Access denied to this content",
            "Please log in to watch",
            "Sign in to confirm your age",
            "Could not find cookies file",
        ]
        for stderr in test_cases:
            with self.subTest(stderr=stderr):
                error = map_ytdlp_error(stderr, url)
                self.assertIsInstance(error, AccessDeniedError)
                self.assertEqual(str(error), Texts.SVC_ACCESS_DENIED)

    def test_video_not_found_mapping(self):
        url = "https://example.com"
        test_cases = [
            "HTTP Error 404: Not Found",
            "video not found",
            "The video is unavailable",
            "This is a private video",
        ]
        for stderr in test_cases:
            with self.subTest(stderr=stderr):
                error = map_ytdlp_error(stderr, url)
                self.assertIsInstance(error, VideoNotFoundError)
                self.assertEqual(str(error), Texts.SVC_VIDEO_NOT_FOUND)

    def test_live_stream_mapping(self):
        url = "https://example.com"
        # Happy path for live stream
        error = map_ytdlp_error("This is a live stream", url)
        self.assertIsInstance(error, LiveStreamError)
        self.assertEqual(str(error), Texts.SVC_LIVE_NOT_SUPPORTED)

        error = map_ytdlp_error("This live event has ended", url)
        self.assertIsInstance(error, LiveStreamError)

        # Should not map if "available" is present (as per code logic)
        error = map_ytdlp_error("This is a live stream but now available as VOD", url)
        self.assertNotIsInstance(error, LiveStreamError)

    def test_format_unavailable_mapping(self):
        url = "https://example.com"
        error = map_ytdlp_error("Requested format is not available", url)
        self.assertIsInstance(error, ExtractionError)
        self.assertEqual(str(error), Texts.SVC_FORMAT_UNAVAILABLE)

    def test_geoblock_mapping(self):
        url = "https://example.com"
        test_cases = [
            "This video is geo-restricted",
            "The uploader has not made this video available in your country",
        ]
        expected_msg = "⚠️ Это видео недоступно в нашей стране (Geo-restricted)."
        for stderr in test_cases:
            with self.subTest(stderr=stderr):
                error = map_ytdlp_error(stderr, url)
                self.assertIsInstance(error, AccessDeniedError)
                self.assertEqual(str(error), expected_msg)

    def test_fallback_generic_error(self):
        url = "https://example.com"
        stderr = "Some random error\nDetailed error message here"
        error = map_ytdlp_error(stderr, url)
        self.assertIsInstance(error, ExtractionError)
        expected_detail = "Detailed error message here"
        self.assertEqual(str(error), Texts.SVC_EXTRACTION_ERROR.format(detail=expected_detail))

    def test_empty_stderr(self):
        url = "https://example.com"
        error = map_ytdlp_error("", url)
        self.assertIsInstance(error, ExtractionError)
        self.assertEqual(str(error), Texts.SVC_EXTRACTION_ERROR.format(detail="Unknown yt-dlp error"))

if __name__ == "__main__":
    unittest.main()
