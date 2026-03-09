"""Tests for app.services.sender — TelegramSender."""

import unittest
from unittest.mock import AsyncMock, patch
import tempfile
import os

from app.services.sender import TelegramSender, MAX_TELEGRAM_ALBUM_SIZE


class TestSendFile(unittest.IsolatedAsyncioTestCase):
    """Test TelegramSender.send_file."""

    async def asyncSetUp(self):
        self.bot = AsyncMock()
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        self.tmp.write(b"fake video data")
        self.tmp.close()

    async def asyncTearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    async def test_sends_video_by_default(self):
        """Default: send_video is called."""
        with patch("app.services.sender._m", create=True):
            result = await TelegramSender.send_file(self.bot, 123, self.tmp.name)
        self.assertTrue(result)
        self.bot.send_video.assert_awaited_once()

    async def test_sends_audio(self):
        """is_audio=True: send_audio is called."""
        with patch("app.services.sender._m", create=True):
            result = await TelegramSender.send_file(
                self.bot, 123, self.tmp.name, is_audio=True
            )
        self.assertTrue(result)
        self.bot.send_audio.assert_awaited_once()

    async def test_sends_animation(self):
        """is_gif=True: send_animation is called."""
        with patch("app.services.sender._m", create=True):
            result = await TelegramSender.send_file(
                self.bot, 123, self.tmp.name, is_gif=True
            )
        self.assertTrue(result)
        self.bot.send_animation.assert_awaited_once()

    async def test_returns_false_on_network_error(self):
        """NetworkError returns False."""
        from telegram.error import NetworkError

        self.bot.send_video.side_effect = NetworkError("timeout")
        with patch("app.services.sender._m", create=True):
            result = await TelegramSender.send_file(self.bot, 123, self.tmp.name)
        self.assertFalse(result)

    async def test_returns_false_on_generic_error(self):
        """Generic exception returns False."""
        self.bot.send_video.side_effect = RuntimeError("boom")
        with patch("app.services.sender._m", create=True):
            result = await TelegramSender.send_file(self.bot, 123, self.tmp.name)
        self.assertFalse(result)


class TestSendSlideshowPhotos(unittest.IsolatedAsyncioTestCase):
    """Test TelegramSender.send_slideshow_photos."""

    async def asyncSetUp(self):
        self.bot = AsyncMock()
        self.tmpdir = tempfile.mkdtemp()
        self.images = []
        for i in range(3):
            p = os.path.join(self.tmpdir, f"img_{i}.jpg")
            with open(p, "wb") as f:
                f.write(b"\xff\xd8\xff" + b"\x00" * 100)
            self.images.append(p)

    async def asyncTearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_sends_media_group(self):
        result = await TelegramSender.send_slideshow_photos(
            self.bot, 123, self.images, caption="test"
        )
        self.assertTrue(result)
        self.bot.send_media_group.assert_awaited_once()

    async def test_empty_images_returns_false(self):
        result = await TelegramSender.send_slideshow_photos(
            self.bot, 123, [], caption="test"
        )
        self.assertFalse(result)

    async def test_network_error_returns_false(self):
        from telegram.error import NetworkError

        self.bot.send_media_group.side_effect = NetworkError("timeout")
        result = await TelegramSender.send_slideshow_photos(self.bot, 123, self.images)
        self.assertFalse(result)

    async def test_caps_at_max_album_size(self):
        """More than MAX_TELEGRAM_ALBUM_SIZE images are truncated."""
        many_images = self.images * 5  # 15 images
        result = await TelegramSender.send_slideshow_photos(
            self.bot, 123, many_images, caption="test"
        )
        self.assertTrue(result)
        call_args = self.bot.send_media_group.call_args
        media = call_args.kwargs.get("media") or call_args[1].get("media")
        self.assertLessEqual(len(media), MAX_TELEGRAM_ALBUM_SIZE)


if __name__ == "__main__":
    unittest.main()
