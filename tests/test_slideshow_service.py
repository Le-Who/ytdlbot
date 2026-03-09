"""Tests for app.services.slideshow — SlideshowPipeline."""

import os
import tempfile
import shutil
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

from app.services.slideshow import SlideshowPipeline


class TestCleanupSlideshow(unittest.TestCase):
    """Test SlideshowPipeline.cleanup_slideshow."""

    def test_none_result_noop(self):
        """None result does nothing."""
        SlideshowPipeline.cleanup_slideshow(None)

    def test_empty_images_noop(self):
        """Result with no images does nothing."""
        result = MagicMock()
        result.images = []
        SlideshowPipeline.cleanup_slideshow(result)

    def test_cleans_slideshow_dir(self):
        """Removes slideshow directory when it exists."""
        tmpdir = tempfile.mkdtemp()
        slideshow_dir = os.path.join(tmpdir, "slideshow_abc123")
        os.makedirs(slideshow_dir)
        img_path = os.path.join(slideshow_dir, "img.jpg")
        with open(img_path, "w") as f:
            f.write("test")

        result = MagicMock()
        result.images = [img_path]

        SlideshowPipeline.cleanup_slideshow(result)
        self.assertFalse(os.path.exists(slideshow_dir))
        shutil.rmtree(tmpdir, ignore_errors=True)

    def test_non_slideshow_dir_not_removed(self):
        """Directory without 'slideshow_' in name is NOT removed."""
        tmpdir = tempfile.mkdtemp()
        regular_dir = os.path.join(tmpdir, "regular_dir")
        os.makedirs(regular_dir)
        img_path = os.path.join(regular_dir, "img.jpg")
        with open(img_path, "w") as f:
            f.write("test")

        result = MagicMock()
        result.images = [img_path]

        SlideshowPipeline.cleanup_slideshow(result)
        self.assertTrue(os.path.exists(regular_dir))
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestDownloadSlideshow(unittest.IsolatedAsyncioTestCase):
    """Test SlideshowPipeline.download_slideshow."""

    async def test_calls_gallery_dl_service(self):
        """download_slideshow delegates to GalleryDlService."""
        from app.core import state

        state.ytdlp = MagicMock()
        state.ytdlp.cookies_path = "/tmp/cookies.txt"
        state.ytdlp.tiktok_proxy = None

        mock_result = MagicMock()
        with patch(
            "app.services.slideshow.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=(mock_result, None),
        ):
            result, error = await SlideshowPipeline.download_slideshow(
                "https://tiktok.com/@user/photo/123"
            )

        self.assertEqual(result, mock_result)
        self.assertIsNone(error)

    async def test_returns_error_on_failure(self):
        """download_slideshow returns error from gallery-dl."""
        from app.core import state

        state.ytdlp = MagicMock()
        state.ytdlp.cookies_path = None
        state.ytdlp.tiktok_proxy = None

        with patch(
            "app.services.slideshow.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=(None, "gallery-dl failed"),
        ):
            result, error = await SlideshowPipeline.download_slideshow(
                "https://tiktok.com/@user/photo/123"
            )

        self.assertIsNone(result)
        self.assertEqual(error, "gallery-dl failed")


if __name__ == "__main__":
    unittest.main()
