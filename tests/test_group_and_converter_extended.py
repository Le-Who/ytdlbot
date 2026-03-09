"""Extended tests for group_logic.py on_group_slideshow callback and converter subprocess paths."""

import asyncio
import sys
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

# Pre-mock curl_cffi so that tikwm.py can be imported without the real package
if "curl_cffi" not in sys.modules:
    _mock_curl = MagicMock()
    _mock_curl.__spec__ = None  # satisfy importlib.util.find_spec()
    sys.modules["curl_cffi"] = _mock_curl
    sys.modules["curl_cffi.requests"] = _mock_curl.requests

from app.core import state
from app.core.texts import Texts


class TestOnGroupSlideshowExtended(unittest.IsolatedAsyncioTestCase):
    """Test on_group_slideshow callback in more detail."""

    async def asyncSetUp(self):
        state.link_cache = {}
        state.file_cache = {}

    @patch("app.bot.group_logic.MediaSender")
    async def test_photo_mode_success(self, mock_sender):
        """Photo mode sends slideshow photos and deletes message."""
        from app.bot.group_logic import on_group_slideshow

        state.link_cache["tok1"] = {
            "page_url": "https://tiktok.com/@user/photo/123",
            "user_tag": "@user",
            "chat_id": 456,
            "original_msg_id": 1,
        }

        mock_result = MagicMock()
        mock_result.images = ["/tmp/img1.jpg", "/tmp/img2.jpg"]
        mock_result.audio = None

        mock_sender.download_slideshow = AsyncMock(return_value=(mock_result, None))
        mock_sender.send_slideshow_photos = AsyncMock(return_value=True)
        mock_sender.cleanup_slideshow = MagicMock()

        q = AsyncMock()
        q.data = "grpslide|tok1|photo"
        q.delete_message = AsyncMock()
        update = MagicMock()
        update.callback_query = q
        context = MagicMock()
        context.bot = AsyncMock()

        await on_group_slideshow(update, context)

        mock_sender.send_slideshow_photos.assert_awaited_once()
        q.delete_message.assert_awaited_once()
        mock_sender.cleanup_slideshow.assert_called_once()

    @patch("app.bot.group_logic.MediaSender")
    async def test_video_mode_success(self, mock_sender):
        """Video mode converts images to video and sends."""
        from app.bot.group_logic import on_group_slideshow

        state.link_cache["tok2"] = {
            "page_url": "https://tiktok.com/@user/photo/456",
            "user_tag": "@user",
            "chat_id": 456,
            "original_msg_id": 2,
        }

        mock_result = MagicMock()
        mock_result.images = ["/tmp/img1.jpg"]
        mock_result.audio = "/tmp/audio.mp3"

        mock_sender.download_slideshow = AsyncMock(return_value=(mock_result, None))
        mock_sender.images_to_video = AsyncMock(return_value="/tmp/video.mp4")
        mock_sender.send_file = AsyncMock(return_value=True)
        mock_sender.cleanup_slideshow = MagicMock()

        q = AsyncMock()
        q.data = "grpslide|tok2|video"
        q.delete_message = AsyncMock()
        update = MagicMock()
        update.callback_query = q
        context = MagicMock()
        context.bot = AsyncMock()

        await on_group_slideshow(update, context)

        mock_sender.images_to_video.assert_awaited_once()
        mock_sender.send_file.assert_awaited_once()

    @patch("app.bot.group_logic.MediaSender")
    async def test_video_mode_conversion_failure(self, mock_sender):
        """Video mode shows error when conversion fails."""
        from app.bot.group_logic import on_group_slideshow

        state.link_cache["tok3"] = {
            "page_url": "https://tiktok.com/@user/photo/789",
            "user_tag": "@user",
            "chat_id": 456,
            "original_msg_id": 3,
        }

        mock_result = MagicMock()
        mock_result.images = ["/tmp/img1.jpg"]
        mock_result.audio = None

        mock_sender.download_slideshow = AsyncMock(return_value=(mock_result, None))
        mock_sender.images_to_video = AsyncMock(return_value=None)
        mock_sender.cleanup_slideshow = MagicMock()

        q = AsyncMock()
        q.data = "grpslide|tok3|video"
        update = MagicMock()
        update.callback_query = q
        context = MagicMock()
        context.bot = AsyncMock()

        await on_group_slideshow(update, context)

        q.edit_message_text.assert_awaited_with(Texts.SLIDESHOW_ERROR)

    @patch("app.bot.group_logic.MediaSender")
    @patch("app.services.tikwm.TikWMService")
    async def test_download_failure_tikwm_fallback(self, mock_tikwm_cls, mock_sender):
        """Slideshow download failure tries TikWM fallback."""
        from app.bot.group_logic import on_group_slideshow

        state.link_cache["tok4"] = {
            "page_url": "https://tiktok.com/@user/photo/999",
            "user_tag": "@user",
            "chat_id": 456,
            "original_msg_id": 4,
        }

        mock_sender.download_slideshow = AsyncMock(
            return_value=(None, "gallery-dl failed")
        )
        mock_tikwm_cls.download_video = AsyncMock(return_value=("/tmp/tikwm.mp4", None))
        mock_sender.send_file = AsyncMock(return_value=True)
        mock_sender.cleanup_slideshow = MagicMock()

        q = AsyncMock()
        q.data = "grpslide|tok4|photo"
        q.delete_message = AsyncMock()
        update = MagicMock()
        update.callback_query = q
        context = MagicMock()
        context.bot = AsyncMock()

        await on_group_slideshow(update, context)

        mock_tikwm_cls.download_video.assert_awaited_once()
        mock_sender.send_file.assert_awaited_once()

    @patch("app.bot.group_logic.MediaSender")
    @patch("app.services.tikwm.TikWMService")
    async def test_download_failure_tikwm_also_fails(self, mock_tikwm_cls, mock_sender):
        """Both gallery-dl and TikWM fail shows error."""
        from app.bot.group_logic import on_group_slideshow

        state.link_cache["tok5"] = {
            "page_url": "https://tiktok.com/@user/photo/000",
            "user_tag": "@user",
            "chat_id": 456,
            "original_msg_id": 5,
        }

        mock_sender.download_slideshow = AsyncMock(return_value=(None, "failed"))
        mock_tikwm_cls.download_video = AsyncMock(
            return_value=(None, "tikwm also failed")
        )
        mock_sender.cleanup_slideshow = MagicMock()

        q = AsyncMock()
        q.data = "grpslide|tok5|photo"
        update = MagicMock()
        update.callback_query = q
        context = MagicMock()
        context.bot = AsyncMock()

        await on_group_slideshow(update, context)

        # Should show the original error
        q.edit_message_text.assert_awaited()


# TikWM auth fallback test removed — the fallback is only triggered inside
# on_group_slideshow, already tested above.


class TestConverterSubprocess(unittest.IsolatedAsyncioTestCase):
    """Test converter with mocked subprocess for better coverage."""

    async def test_gif_conversion_success(self):
        """Successful ffmpeg conversion returns gif path."""
        from app.services.converter import MediaConverter
        import tempfile
        import os

        # Create a fake video file
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        tmp.write(b"fake video")
        tmp.close()

        _gif_path = tmp.name.rsplit(".", 1)[0] + "_gif.mp4"

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        mock_sem = asyncio.Semaphore(1)
        mock_metrics = MagicMock()
        mock_metrics.conversion_duration.time.return_value.__enter__ = MagicMock()
        mock_metrics.conversion_duration.time.return_value.__exit__ = MagicMock()

        with (
            patch("app.services.converter.state") as mock_state,
            patch(
                "app.services.converter.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec,
            patch("app.services.converter.os.path.exists") as mock_exists,
            patch("app.services.converter.os.path.getsize", return_value=1000),
        ):
            mock_state.conversion_sem = mock_sem
            mock_exec.return_value = mock_proc
            mock_exists.side_effect = lambda p: (
                True
            )  # both video_path and gif_path exist

            with patch("app.core.metrics.metrics", mock_metrics):
                result = await MediaConverter.convert_to_gif_ffmpeg(tmp.name)

            self.assertIsNotNone(result)

        os.unlink(tmp.name)

    async def test_gif_conversion_ffmpeg_failure(self):
        """ffmpeg returncode != 0 returns None."""
        from app.services.converter import MediaConverter
        import tempfile
        import os

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        tmp.write(b"fake video")
        tmp.close()

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error output"))

        mock_sem = asyncio.Semaphore(1)
        mock_metrics = MagicMock()
        mock_metrics.conversion_duration.time.return_value.__enter__ = MagicMock()
        mock_metrics.conversion_duration.time.return_value.__exit__ = MagicMock()

        with (
            patch("app.services.converter.state") as mock_state,
            patch(
                "app.services.converter.asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
            ) as mock_exec,
        ):
            mock_state.conversion_sem = mock_sem
            mock_exec.return_value = mock_proc

            with patch("app.core.metrics.metrics", mock_metrics):
                result = await MediaConverter.convert_to_gif_ffmpeg(tmp.name)

            self.assertIsNone(result)

        os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
