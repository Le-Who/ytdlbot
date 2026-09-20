import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.tikwm import TikWMResult, TikWMService


class TestTikWMDownloadVideoOptimized(unittest.IsolatedAsyncioTestCase):
    """Test TikWMService.download_video with optimized async I/O."""

    def test_collection_does_not_replace_installed_telegram_module(self):
        import telegram

        self.assertNotIsInstance(telegram, MagicMock)

    def test_collection_preserves_real_dependency_modules_and_session_symbol(self):
        import cachetools
        import curl_cffi
        import dotenv
        import fastapi
        import telegram
        import yt_dlp
        from curl_cffi.requests import AsyncSession as RealAsyncSession

        from app.services import tikwm

        for module in (
            dotenv,
            cachetools,
            curl_cffi,
            fastapi,
            telegram,
            yt_dlp,
        ):
            self.assertNotIsInstance(module, MagicMock)
            self.assertIsNotNone(module.__spec__)
        self.assertIs(tikwm.AsyncSession, RealAsyncSession)

    async def test_download_success_writes_file_async(self):
        import tempfile

        tmpdir = tempfile.mkdtemp()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def video_chunks(chunk_size):
            del chunk_size
            yield b"fake video data" * 1024

        mock_resp.aiter_content = video_chunks

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch.object(
            TikWMService,
            "process",
            new_callable=AsyncMock,
            return_value=TikWMResult(
                status="video", url="https://cdn.tikwm.com/video.mp4", title="Title"
            ),
        ):
            with patch("app.services.tikwm.TEMP_DIR", tmpdir):
                with patch(
                    "app.services.tikwm.AsyncSession", return_value=mock_session
                ):
                    with patch("uuid.uuid4") as mock_uuid:
                        mock_uuid.return_value.hex = "test"
                        expected_path = os.path.join(tmpdir, "tikwm_test.mp4")

                        path, error = await TikWMService.download_video(
                            "https://tiktok.com/video/123"
                        )

        self.assertIsNone(error)
        self.assertEqual(path, expected_path)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"fake video data" * 1024)

        os.unlink(path)
        os.rmdir(tmpdir)

    async def test_download_failure_cleanup_async(self):
        import tempfile

        tmpdir = tempfile.mkdtemp()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        # Force an error during download
        mock_session.get = AsyncMock(side_effect=Exception("Download failed"))

        with patch.object(
            TikWMService,
            "process",
            new_callable=AsyncMock,
            return_value=TikWMResult(
                status="video", url="https://cdn.tikwm.com/video.mp4", title="Title"
            ),
        ):
            with patch("app.services.tikwm.TEMP_DIR", tmpdir):
                with patch(
                    "app.services.tikwm.AsyncSession", return_value=mock_session
                ):
                    with patch("uuid.uuid4") as mock_uuid:
                        mock_uuid.return_value.hex = "failure_test"
                        path, error = await TikWMService.download_video(
                            "https://tiktok.com/video/123"
                        )

        self.assertIsNone(path)
        self.assertIn("Download failed", error)

        expected_path = os.path.join(tmpdir, "tikwm_failure_test.mp4")
        self.assertFalse(os.path.exists(expected_path))
        os.rmdir(tmpdir)


if __name__ == "__main__":
    unittest.main()
