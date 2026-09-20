"""Tests for app.services.converter — MediaConverter."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class TestConvertToGifFfmpeg(unittest.IsolatedAsyncioTestCase):
    """Test MediaConverter.convert_to_gif_ffmpeg."""

    async def test_none_input_returns_none(self):
        from app.services.converter import MediaConverter

        result = await MediaConverter.convert_to_gif_ffmpeg(None)
        self.assertIsNone(result)

    async def test_empty_string_returns_none(self):
        from app.services.converter import MediaConverter

        result = await MediaConverter.convert_to_gif_ffmpeg("")
        self.assertIsNone(result)

    async def test_nonexistent_file_returns_none(self):
        from app.services.converter import MediaConverter

        result = await MediaConverter.convert_to_gif_ffmpeg("/nonexistent/path.mp4")
        self.assertIsNone(result)


class TestImagesToVideo(unittest.IsolatedAsyncioTestCase):
    """Test MediaConverter.images_to_video."""

    async def test_empty_images_returns_none(self):
        from app.services.converter import MediaConverter

        result = await MediaConverter.images_to_video([])
        self.assertIsNone(result)

    async def test_none_images_returns_none(self):
        from app.services.converter import MediaConverter

        result = await MediaConverter.images_to_video(None)
        self.assertIsNone(result)


class TestAudioContainerConversion(unittest.IsolatedAsyncioTestCase):
    @patch("app.core.process.process_supervisor.run", new_callable=AsyncMock)
    @patch("app.services.converter.os.path.getsize", return_value=128)
    @patch("app.services.converter.os.path.exists", return_value=True)
    async def test_webm_opus_is_remuxed_to_ogg_with_stream_copy(
        self, _exists, _size, run
    ):
        """Catches relabeling WebM bytes as Ogg instead of remuxing them."""
        from app.services.converter import MediaConverter

        run.return_value = MagicMock(returncode=0, stderr=b"")
        result = await MediaConverter.remux_webm_opus("/tmp/audio.webm")

        self.assertEqual(result, "/tmp/audio.ogg")
        command = run.await_args.args[0]
        self.assertIn("-c:a", command)
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertIn("ogg", command)

    @patch("app.core.process.process_supervisor.run", new_callable=AsyncMock)
    @patch("app.services.converter.os.path.getsize", return_value=128)
    @patch("app.services.converter.os.path.exists", return_value=True)
    async def test_strict_mp3_transcodes_non_mp3_input(self, _exists, _size, run):
        """Catches strict MP3 delivery returning Opus/AAC under an MP3 request."""
        from app.services.converter import MediaConverter

        run.return_value = MagicMock(returncode=0, stderr=b"")
        result = await MediaConverter.convert_to_mp3("/tmp/audio.webm")

        self.assertEqual(result, "/tmp/audio.mp3")
        command = run.await_args.args[0]
        self.assertIn("libmp3lame", command)
        self.assertEqual(command[command.index("-f") + 1], "mp3")


if __name__ == "__main__":
    unittest.main()
