"""Tests for app.services.converter — MediaConverter."""

import unittest
from unittest.mock import patch, MagicMock, AsyncMock


from app.services.converter import MediaConverter


class TestMediaConverter(unittest.IsolatedAsyncioTestCase):
    """Test MediaConverter FFmpeg wrappers."""

    def setUp(self):
        from app.core import state

        # Mock the state semaphore
        state.conversion_sem = MagicMock()
        state.conversion_sem.__aenter__ = AsyncMock()
        state.conversion_sem.__aexit__ = AsyncMock()

    # ── Helper to build subprocess mocks ──────────────────────────────

    @staticmethod
    def _make_proc(returncode=0, stdout=b"", stderr=b""):
        proc = AsyncMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        return proc

    # ── convert_to_gif_ffmpeg ─────────────────────────────────────────

    @patch("app.services.converter.os.path.exists", return_value=True)
    @patch("app.services.converter.os.path.getsize", return_value=1024)
    @patch("app.services.converter.asyncio.create_subprocess_exec")
    @patch("app.services.converter._probe_video_codec", new_callable=AsyncMock)
    async def test_convert_to_gif_h264_uses_copy(
        self, mock_probe, mock_exec, mock_size, mock_exists
    ):
        """h264 file → stream-copy (no re-encoding)."""
        mock_probe.return_value = "h264"
        mock_exec.return_value = self._make_proc(0, stderr=b"")

        result = await MediaConverter.convert_to_gif_ffmpeg("/tmp/video.mp4")

        self.assertEqual(result, "/tmp/video_gif.mp4")
        # ffmpeg should have been called once (copy mode)
        mock_exec.assert_called_once()
        cmd_args = mock_exec.call_args[0]
        self.assertIn("-c:v", cmd_args)
        self.assertIn("copy", cmd_args)

    @patch("app.services.converter.os.path.exists", return_value=True)
    @patch("app.services.converter.os.path.getsize", return_value=1024)
    @patch("app.services.converter.asyncio.create_subprocess_exec")
    @patch("app.services.converter._probe_video_codec", new_callable=AsyncMock)
    async def test_convert_to_gif_vp9_uses_libx264(
        self, mock_probe, mock_exec, mock_size, mock_exists
    ):
        """vp9 file → transcode with libx264."""
        mock_probe.return_value = "vp9"
        mock_exec.return_value = self._make_proc(0, stderr=b"")

        result = await MediaConverter.convert_to_gif_ffmpeg("/tmp/video.webm")

        self.assertEqual(result, "/tmp/video_gif.mp4")
        mock_exec.assert_called_once()
        cmd_args = mock_exec.call_args[0]
        self.assertIn("libx264", cmd_args)

    @patch("app.services.converter.os.path.exists", return_value=True)
    @patch("app.services.converter.os.path.getsize", return_value=1024)
    @patch("app.services.converter.safe_remove")
    @patch("app.services.converter.asyncio.create_subprocess_exec")
    @patch("app.services.converter._probe_video_codec", new_callable=AsyncMock)
    async def test_convert_to_gif_copy_fails_fallback_to_transcode(
        self, mock_probe, mock_exec, mock_safe_remove, mock_size, mock_exists
    ):
        """Stream-copy fail → auto-fallback to libx264 transcode."""
        mock_probe.return_value = "h264"
        # First call (copy) fails, second call (transcode) succeeds
        mock_exec.side_effect = [
            self._make_proc(1, stderr=b"copy error"),
            self._make_proc(0, stderr=b""),
        ]

        result = await MediaConverter.convert_to_gif_ffmpeg("/tmp/video.mp4")

        self.assertEqual(result, "/tmp/video_gif.mp4")
        self.assertEqual(mock_exec.call_count, 2)
        # Second call should use libx264
        second_call_args = mock_exec.call_args_list[1][0]
        self.assertIn("libx264", second_call_args)

    @patch("app.services.converter.os.path.exists", side_effect=[True, False])
    @patch("app.services.converter.asyncio.create_subprocess_exec")
    @patch("app.services.converter._probe_video_codec", new_callable=AsyncMock)
    async def test_convert_to_gif_ffmpeg_failure(
        self, mock_probe, mock_exec, mock_exists
    ):
        """Both attempts fail → returns None."""
        mock_probe.return_value = "vp9"
        mock_exec.return_value = self._make_proc(1, stderr=b"error details here")

        result = await MediaConverter.convert_to_gif_ffmpeg("/tmp/video.mp4")

        self.assertIsNone(result)

    @patch("app.services.converter.os.path.exists", return_value=True)
    @patch("app.services.converter.os.path.getsize", return_value=1024)
    @patch("app.services.converter.asyncio.create_subprocess_exec")
    @patch("app.services.converter._probe_video_codec", new_callable=AsyncMock)
    async def test_convert_to_gif_probe_fails_falls_back_to_transcode(
        self, mock_probe, mock_exec, mock_size, mock_exists
    ):
        """Probe fails (returns None) → uses transcode (safe default)."""
        mock_probe.return_value = None
        mock_exec.return_value = self._make_proc(0, stderr=b"")

        result = await MediaConverter.convert_to_gif_ffmpeg("/tmp/video.mp4")

        self.assertEqual(result, "/tmp/video_gif.mp4")
        mock_exec.assert_called_once()
        cmd_args = mock_exec.call_args[0]
        self.assertIn("libx264", cmd_args)

    # ── images_to_video ───────────────────────────────────────────────

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


class TestConvertToNativeGif(unittest.IsolatedAsyncioTestCase):
    """Unit tests for MediaConverter.convert_to_native_gif."""

    def setUp(self):
        from app.core import state

        state.gif_file_sem = MagicMock()
        state.gif_file_sem.__aenter__ = AsyncMock()
        state.gif_file_sem.__aexit__ = AsyncMock()

    @staticmethod
    def _make_proc(returncode=0, stderr=b""):
        proc = AsyncMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(b"", stderr))
        return proc

    @patch("app.services.converter.safe_remove")
    @patch("app.services.converter.os.path.getsize", return_value=4096)
    @patch("app.services.converter.os.path.exists", return_value=True)
    @patch("app.services.converter.asyncio.create_subprocess_exec")
    async def test_convert_to_native_gif_success(
        self, mock_exec, mock_exists, mock_size, mock_safe_remove
    ):
        """Happy path: palettegen + paletteuse both succeed → returns gif path."""
        mock_exec.return_value = self._make_proc(0)

        with patch("app.services.converter.uuid.uuid4") as mock_uuid:
            mock_uuid.return_value.hex = "aabbcc"
            result = await MediaConverter.convert_to_native_gif("/tmp/test.mp4")

        self.assertIsNotNone(result)
        self.assertTrue(result.endswith("_native.gif"))
        # Two ffmpeg calls: palettegen + paletteuse
        self.assertEqual(mock_exec.call_count, 2)
        # palette png must be cleaned up
        mock_safe_remove.assert_called()

    @patch("app.services.converter.safe_remove")
    @patch("app.services.converter.os.path.exists", return_value=True)
    @patch("app.services.converter.asyncio.create_subprocess_exec")
    async def test_convert_to_native_gif_palettegen_failure(
        self, mock_exec, mock_exists, mock_safe_remove
    ):
        """Palettegen (pass 1) fails → returns None immediately."""
        mock_exec.return_value = self._make_proc(1, b"palettegen error")

        result = await MediaConverter.convert_to_native_gif("/tmp/test.mp4")

        self.assertIsNone(result)
        # Only one ffmpeg call (pass 1 bails early)
        self.assertEqual(mock_exec.call_count, 1)

    @patch("app.services.converter.safe_remove")
    @patch("app.services.converter.os.path.getsize", return_value=0)
    @patch("app.services.converter.os.path.exists", return_value=True)
    @patch("app.services.converter.asyncio.create_subprocess_exec")
    async def test_convert_to_native_gif_empty_output(
        self, mock_exec, mock_exists, mock_size, mock_safe_remove
    ):
        """Paletteuse succeeds but output is empty → returns None."""
        mock_exec.return_value = self._make_proc(0)

        result = await MediaConverter.convert_to_native_gif("/tmp/test.mp4")

        self.assertIsNone(result)

    @patch("app.services.converter.os.path.exists", return_value=False)
    async def test_convert_to_native_gif_missing_input(self, mock_exists):
        """Missing source file → returns None immediately."""
        result = await MediaConverter.convert_to_native_gif("/nonexistent/video.mp4")
        self.assertIsNone(result)

    async def test_convert_to_native_gif_empty_path(self):
        """Empty/None source path → returns None immediately."""
        self.assertIsNone(await MediaConverter.convert_to_native_gif(""))
        self.assertIsNone(await MediaConverter.convert_to_native_gif(None))  # type: ignore
