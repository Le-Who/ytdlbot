"""Tests for gallery-dl service."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from app.services.gallery_dl.service import GalleryDlService, SlideshowResult


class TestGalleryDlService(unittest.TestCase):
    """Tests for GalleryDlService."""

    @patch("app.services.gallery_dl.service.subprocess.run")
    def test_download_success_command_construction(self, mock_run):
        """Verify gallery-dl command includes correct flags."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        with patch.object(GalleryDlService, "_collect_files") as mock_collect:
            mock_collect.return_value = (
                SlideshowResult(images=["/tmp/a.jpg"], title="Test"),
                None,
            )
            result, error = GalleryDlService.download_slideshow(
                "https://tiktok.com/@user/video/123",
                cookies_path="/tmp/cookies.txt",
            )

        self.assertIsNotNone(result)
        self.assertIsNone(error)

        # Verify command
        args = mock_run.call_args
        cmd = args[0][0]
        self.assertIn("gallery-dl", cmd)
        self.assertIn("--cookies", cmd)
        self.assertIn("/tmp/cookies.txt", cmd)
        self.assertIn("--no-mtime", cmd)
        self.assertIn("https://tiktok.com/@user/video/123", cmd)

    @patch("app.services.gallery_dl.service.subprocess.run")
    def test_download_without_cookies(self, mock_run):
        """Command should not include --cookies when no cookies path."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        with patch.object(GalleryDlService, "_collect_files") as mock_collect:
            mock_collect.return_value = (
                SlideshowResult(images=["/tmp/a.jpg"]),
                None,
            )
            GalleryDlService.download_slideshow(
                "https://tiktok.com/@user/video/123",
                cookies_path=None,
            )

        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--cookies", cmd)

    @patch("app.services.gallery_dl.service.subprocess.run")
    def test_download_failure_returns_error(self, mock_run):
        """Non-zero return code should return error message."""
        mock_run.return_value = MagicMock(
            returncode=1, stderr="Error: something went wrong"
        )
        result, error = GalleryDlService.download_slideshow(
            "https://tiktok.com/@user/video/123",
        )
        self.assertIsNone(result)
        self.assertIn("Ошибка", error)

    @patch("app.services.gallery_dl.service.subprocess.run")
    def test_download_login_required_error(self, mock_run):
        """Login-related error should return auth error message."""
        mock_run.return_value = MagicMock(returncode=1, stderr="Error: login required")
        result, error = GalleryDlService.download_slideshow(
            "https://tiktok.com/@user/video/123",
        )
        self.assertIsNone(result)
        self.assertIn("авторизация", error.lower())

    @patch(
        "app.services.gallery_dl.service.subprocess.run",
        side_effect=FileNotFoundError,
    )
    def test_gallery_dl_not_installed(self, mock_run):
        """FileNotFoundError should return 'not installed' error."""
        result, error = GalleryDlService.download_slideshow(
            "https://tiktok.com/@user/video/123",
        )
        self.assertIsNone(result)
        self.assertIn("gallery-dl", error)

    @patch(
        "app.services.gallery_dl.service.subprocess.run",
        side_effect=TimeoutError,
    )
    def test_download_timeout(self, mock_run):
        """Subprocess timeout should return timeout error."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gallery-dl", timeout=120)
        result, error = GalleryDlService.download_slideshow(
            "https://tiktok.com/@user/video/123",
        )
        self.assertIsNone(result)
        self.assertIn("Время", error)

    def test_collect_files_with_images(self):
        """_collect_files should find images and audio."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create fake image files
            for name in ["001.jpg", "002.png", "003.webp"]:
                with open(os.path.join(tmpdir, name), "w") as f:
                    f.write("fake image")

            # Create fake audio
            with open(os.path.join(tmpdir, "audio.mp3"), "w") as f:
                f.write("fake audio")

            # Create metadata
            meta = {"description": "Test slideshow description"}
            with open(os.path.join(tmpdir, "meta.json"), "w") as f:
                json.dump(meta, f)

            result, error = GalleryDlService._collect_files(tmpdir)

        self.assertIsNone(error)
        self.assertIsNotNone(result)
        self.assertEqual(len(result.images), 3)
        self.assertIsNotNone(result.audio)
        self.assertEqual(result.title, "Test slideshow description")

    def test_collect_files_no_images(self):
        """_collect_files should return error when no images found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Only audio, no images
            with open(os.path.join(tmpdir, "audio.mp3"), "w") as f:
                f.write("fake audio")

            result, error = GalleryDlService._collect_files(tmpdir)

        self.assertIsNone(result)
        self.assertIn("фото", error.lower())

    def test_collect_files_sorted_order(self):
        """Images should be returned in sorted order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ["003.jpg", "001.jpg", "002.jpg"]:
                with open(os.path.join(tmpdir, name), "w") as f:
                    f.write("fake")

            result, error = GalleryDlService._collect_files(tmpdir)

        self.assertIsNone(error)
        basenames = [os.path.basename(p) for p in result.images]
        self.assertEqual(basenames, ["001.jpg", "002.jpg", "003.jpg"])


if __name__ == "__main__":
    unittest.main()
