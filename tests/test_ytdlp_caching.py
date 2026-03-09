import unittest
from unittest.mock import MagicMock, patch

from app.services.ytdlp.service import YtDlpService


class TestYtDlpCaching(unittest.TestCase):
    """Test that YtDlpService.extract() properly creates/uses YoutubeDL instances."""

    def setUp(self):
        self.service = YtDlpService()

    def test_extract_calls_ytdl(self):
        """Test that extract() successfully calls YoutubeDL and returns result."""
        with patch("app.services.ytdlp.service.yt_dlp.YoutubeDL") as MockYDL:
            mock_instance = MagicMock()
            mock_instance.extract_info.return_value = {"title": "Test", "formats": []}
            mock_instance.__enter__ = MagicMock(return_value=mock_instance)
            mock_instance.__exit__ = MagicMock(return_value=False)
            MockYDL.return_value = mock_instance

            result = self.service.extract("http://test.com", for_list_formats=True)
            self.assertEqual(result["title"], "Test")
            MockYDL.assert_called()

    def test_extract_passes_correct_opts(self):
        """Test that extract() passes correct options for list_formats mode."""
        with patch("app.services.ytdlp.service.yt_dlp.YoutubeDL") as MockYDL:
            mock_instance = MagicMock()
            mock_instance.extract_info.return_value = {"title": "Test", "formats": []}
            mock_instance.__enter__ = MagicMock(return_value=mock_instance)
            mock_instance.__exit__ = MagicMock(return_value=False)
            MockYDL.return_value = mock_instance

            self.service.extract("http://test.com", for_list_formats=True)

            # Verify extract_info was called with the right URL and download=False
            mock_instance.extract_info.assert_called_once_with(
                "http://test.com", download=False
            )


if __name__ == "__main__":
    unittest.main()
