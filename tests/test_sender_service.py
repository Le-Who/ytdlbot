"""Tests for app.services.sender — TelegramSender."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.media.models import DeliveryStatus
from app.services.sender import TelegramSender


class TestSendFile(unittest.IsolatedAsyncioTestCase):
    """Test TelegramSender.send_file."""

    async def asyncSetUp(self):
        self.bot = AsyncMock()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as self.tmp:
            self.tmp.write(b"fake video data")

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
        self.assertTrue(result.success)
        self.assertIs(result.status, DeliveryStatus.SUCCESS)
        self.bot.send_video.assert_awaited_once()

    async def test_default_sender_uses_configured_local_profile(self):
        """Catches legacy callers silently forcing cloud uploads in production."""
        media_dir = os.path.dirname(self.tmp.name)
        with (
            patch("app.core.config.TELEGRAM_LOCAL_ENDPOINT", "http://tg-api:8081"),
            patch("app.core.config.MEDIA_DIR", media_dir),
        ):
            result = await TelegramSender.send_file(self.bot, 123, self.tmp.name)

        self.assertTrue(result)
        self.assertEqual(
            self.bot.send_video.await_args.kwargs["video"],
            f"file://{os.path.abspath(self.tmp.name)}",
        )

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
        self.assertIs(result.status, DeliveryStatus.UNCERTAIN)
        self.assertEqual(self.bot.send_video.await_count, 1)

    async def test_returns_false_on_generic_error(self):
        """Generic exception returns False."""
        self.bot.send_video.side_effect = RuntimeError("boom")
        with patch("app.services.sender._m", create=True):
            result = await TelegramSender.send_file(self.bot, 123, self.tmp.name)
        self.assertFalse(result)
        self.assertIs(result.status, DeliveryStatus.FAILED)


class TestSendSlideshowPhotos(unittest.IsolatedAsyncioTestCase):
    """Test TelegramSender.send_slideshow_photos."""

    async def asyncSetUp(self):
        self.bot = AsyncMock()
        self.tmpdir = tempfile.mkdtemp()
        self.images = []
        for i in range(3):
            p = os.path.join(self.tmpdir, f"img_{i}.jpg")
            # Small synthetic fixture I/O completes inline; no runtime worker ownership.
            with open(p, "wb") as f:  # noqa: ASYNC230
                f.write(b"\xff\xd8\xff" + b"\x00" * 100)
            self.images.append(p)

        async def send_media_group(**kwargs):
            return [
                SimpleNamespace(
                    message_id=index,
                    photo=[
                        SimpleNamespace(
                            file_id=f"photo-{index}",
                            file_unique_id=f"unique-{index}",
                        )
                    ],
                )
                for index, _ in enumerate(kwargs["media"])
            ]

        self.bot.send_media_group.side_effect = send_media_group

    async def asyncTearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_sends_media_group(self):
        result = await TelegramSender.send_slideshow_photos(
            self.bot, 123, self.images, caption="test"
        )
        self.assertTrue(result)
        self.assertTrue(result.success)
        self.bot.send_media_group.assert_awaited_once()

    async def test_empty_images_returns_false(self):
        result = await TelegramSender.send_slideshow_photos(
            self.bot, 123, [], caption="test"
        )
        self.assertFalse(result)
        self.assertIs(result.status, DeliveryStatus.FAILED)

    async def test_network_error_returns_false(self):
        from telegram.error import NetworkError

        self.bot.send_media_group.side_effect = NetworkError("timeout")
        result = await TelegramSender.send_slideshow_photos(self.bot, 123, self.images)
        self.assertFalse(result)
        self.assertIs(result.status, DeliveryStatus.UNCERTAIN)

    async def test_sends_every_image_across_multiple_album_chunks(self):
        """All 15 unique photos are delivered, with caption on the first only."""
        many_images = []
        expected = []
        for index in range(15):
            content = b"\xff\xd8\xff" + bytes([index]) * 100
            path = os.path.join(self.tmpdir, f"album_{index}.jpg")
            await asyncio.to_thread(Path(path).write_bytes, content)
            many_images.append(path)
            expected.append(content)
        delivered = []
        original_send = self.bot.send_media_group.side_effect

        async def record_media(**kwargs):
            for media in kwargs["media"]:
                content = media.media.input_file_content
                delivered.append(
                    content.read() if hasattr(content, "read") else content
                )
            return await original_send(**kwargs)

        self.bot.send_media_group.side_effect = record_media
        result = await TelegramSender.send_slideshow_photos(
            self.bot, 123, many_images, caption="test"
        )
        self.assertTrue(result)
        groups = [
            call.kwargs["media"] for call in self.bot.send_media_group.await_args_list
        ]
        self.assertEqual([len(group) for group in groups], [10, 5])
        self.assertEqual(delivered, expected)
        self.assertEqual(len(result.items), 15)
        self.assertTrue(
            all(item.status is DeliveryStatus.SUCCESS for item in result.items)
        )
        self.assertEqual(
            [media.caption or None for group in groups for media in group],
            ["test"] + [None] * 14,
        )


if __name__ == "__main__":
    unittest.main()
