import unittest
import threading
from unittest.mock import MagicMock, patch
from app.services.ytdlp.service import YtDlpService
import yt_dlp


class TestYtDlpCaching(unittest.TestCase):
    def setUp(self):
        self.service = YtDlpService()

    def test_caching_behavior(self):
        # Mock YoutubeDL class
        with patch("yt_dlp.YoutubeDL") as MockYDL:
            mock_instance = MagicMock()
            # return dummy info
            mock_instance.extract_info.return_value = {"title": "Test", "formats": []}
            # Important: __enter__ must return the instance
            mock_instance.__enter__.return_value = mock_instance
            MockYDL.return_value = mock_instance

            # First call
            self.service.extract("http://test.com", for_list_formats=True)

            # Second call in same thread
            self.service.extract("http://test.com", for_list_formats=True)

            # Check call count
            # With optimization, this should be 1.
            self.assertEqual(
                MockYDL.call_count,
                1,
                "YoutubeDL should be instantiated only once per thread",
            )

    def test_thread_independence(self):
        with patch("yt_dlp.YoutubeDL") as MockYDL:
            mock_instance = MagicMock()
            mock_instance.extract_info.return_value = {"title": "Test", "formats": []}
            mock_instance.__enter__.return_value = mock_instance
            MockYDL.return_value = mock_instance

            def worker():
                self.service.extract("http://test.com", for_list_formats=True)

            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)

            t1.start()
            t2.start()
            t1.join()
            t2.join()

            # Should be called twice (once per thread)
            # Note: calling MockYDL.call_count is thread-safe enough for this simple check usually,
            # but strictly speaking mock calls are recorded.
            self.assertEqual(
                MockYDL.call_count,
                2,
                "YoutubeDL should be instantiated once per thread",
            )


if __name__ == "__main__":
    unittest.main()
