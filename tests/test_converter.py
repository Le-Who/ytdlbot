"""Tests for app.services.converter — MediaConverter."""

import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import os

from app.services.converter import MediaConverter


class TestMediaConverter(unittest.IsolatedAsyncioTestCase):
    """Test MediaConverter FFmpeg wrappers."""

    def setUp(self):
        from app.core import state

        # Mock the state semaphore
        state.conversion_sem = MagicMock()
        state.conversion_sem.__aenter__ = AsyncMock()
        state.conversion_sem.__aexit__ = AsyncMock()

    @patch("app.services.converter.os.path.exists", return_value=True)
    @patch("app.services.converter.os.path.getsize", return_value=1024)
    @patch("app.services.converter.asyncio.create_subprocess_exec")
    async def test_convert_to_gif_ffmpeg_success(
        self, mock_exec, mock_size, mock_exists
    ):
        """Test successful conversion to GIF returns the path."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"stdout", b"stderr"))
        mock_exec.return_value = mock_proc

        result = await MediaConverter.convert_to_gif_ffmpeg("/tmp/video.mp4")

        self.assertEqual(result, "/tmp/video_gif.mp4")
        mock_exec.assert_called_once()
        self.assertIn("ffmpeg", mock_exec.call_args[0])

    @patch(
        "app.services.converter.os.path.exists", side_effect=[True, False]
    )  # First for video exists, second for gif
    @patch("app.services.converter.asyncio.create_subprocess_exec")
    async def test_convert_to_gif_ffmpeg_failure(self, mock_exec, mock_exists):
        """Test failed conversion returns None."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_exec.return_value = mock_proc

        result = await MediaConverter.convert_to_gif_ffmpeg("/tmp/video.mp4")

        self.assertIsNone(result)

    @patch("app.services.converter.os.path.exists", return_value=True)
    @patch("app.services.converter.os.path.getsize", return_value=1024)
    @patch("app.services.converter.asyncio.create_subprocess_exec")
    @patch("app.services.converter.open", new_callable=unittest.mock.mock_open)
    @patch("app.services.converter.safe_remove")
    async def test_images_to_video_success_with_audio(
        self, mock_safe_remove, mock_open_file, mock_exec, mock_size, mock_exists
    ):
        """Test successful slideshow generation from images and audio."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_exec.return_value = mock_proc

        images = ["/tmp/img1.jpg", "/tmp/img2.jpg"]

        with patch("app.services.converter.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value.hex = "1234"
            result = await MediaConverter.images_to_video(images, "/tmp/audio.mp3")

        self.assertIsNotNone(result)
        self.assertTrue("slideshow_1234.mp4" in result)
        mock_exec.assert_called_once()
        mock_safe_remove.assert_called_once()

    @patch("app.services.converter.os.path.exists", return_value=True)
    @patch("app.services.converter.os.path.getsize", return_value=1024)
    @patch("app.services.converter.asyncio.create_subprocess_exec")
    @patch("app.services.converter.open", new_callable=unittest.mock.mock_open)
    @patch("app.services.converter.safe_remove")
    async def test_images_to_video_success_no_audio(
        self, mock_safe_remove, mock_open_file, mock_exec, mock_size, mock_exists
    ):
        """Test successful slideshow generation from images only (no audio)."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_exec.return_value = mock_proc

        images = ["/tmp/img1.jpg", "/tmp/img2.jpg"]

        with patch("app.services.converter.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value.hex = "5678"
            result = await MediaConverter.images_to_video(images, None)

        self.assertIsNotNone(result)
        self.assertTrue("slideshow_5678.mp4" in result)

        # Audio args should not be in the command
        cmd_args = mock_exec.call_args[0]
        self.assertNotIn("-c:a", cmd_args)


if __name__ == "__main__":
    unittest.main()
