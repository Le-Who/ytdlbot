import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add repo root to path
from app.core.utils import is_supported_url


class TestMainUtils(unittest.TestCase):
    def test_is_supported_url(self):
        # Supported exact
        self.assertTrue(is_supported_url("https://youtube.com/watch?v=123"))
        self.assertTrue(is_supported_url("https://tiktok.com/@user/video/123"))

        # Supported subdomain
        self.assertTrue(is_supported_url("https://www.youtube.com/watch?v=123"))
        self.assertTrue(is_supported_url("https://m.tiktok.com/v/123"))
        self.assertTrue(is_supported_url("https://vm.tiktok.com/123"))

        # Unsupported
        self.assertFalse(is_supported_url("https://google.com"))
        self.assertFalse(is_supported_url("https://example.com"))

        # Tricky cases (Suffix but not subdomain)
        self.assertFalse(is_supported_url("https://notyoutube.com"))
        self.assertFalse(is_supported_url("https://myyoutube.com"))
        self.assertFalse(is_supported_url("https://faketiktok.com"))

        # Malformed
        self.assertFalse(is_supported_url("not a url"))
        self.assertFalse(is_supported_url(""))

        # Case insensitivity (urlparse handles domain as lowercase usually, but good to check)
        self.assertTrue(is_supported_url("https://YOUTUBE.COM/watch"))

    def test_local_bot_api_builder_has_file_url_local_mode_and_media_timeouts(self):
        """Catches configuring only the request URL while uploads stay cloud-mode."""
        from app.main import _build_telegram_application_builder

        builder = MagicMock()
        for method in (
            "token",
            "concurrent_updates",
            "base_url",
            "base_file_url",
            "local_mode",
            "media_write_timeout",
            "read_timeout",
            "write_timeout",
            "connect_timeout",
            "pool_timeout",
        ):
            getattr(builder, method).return_value = builder

        with (
            patch("app.main.Application.builder", return_value=builder),
            patch("app.main.config.TELEGRAM_LOCAL_ENDPOINT", "http://tg-api:8081"),
            patch("app.main.config.TELEGRAM_MEDIA_WRITE_TIMEOUT", 901.0),
            patch("app.main.config.TELEGRAM_READ_TIMEOUT", 902.0),
            patch("app.main.config.TELEGRAM_WRITE_TIMEOUT", 903.0),
            patch("app.main.config.TELEGRAM_CONNECT_TIMEOUT", 31.0),
            patch("app.main.config.TELEGRAM_POOL_TIMEOUT", 32.0),
        ):
            result = _build_telegram_application_builder()

        self.assertIs(result, builder)
        builder.base_url.assert_called_once_with("http://tg-api:8081/bot")
        builder.base_file_url.assert_called_once_with("http://tg-api:8081/file/bot")
        builder.local_mode.assert_called_once_with(True)
        builder.media_write_timeout.assert_called_once_with(901.0)
        builder.read_timeout.assert_called_once_with(902.0)
        builder.write_timeout.assert_called_once_with(903.0)
        builder.connect_timeout.assert_called_once_with(31.0)
        builder.pool_timeout.assert_called_once_with(32.0)

    def test_consistent_legacy_media_limits_migrate_to_one_setting(self):
        """Catches keeping two independently effective legacy limits."""
        env = os.environ.copy()
        env.update(
            BOT_TOKEN="test-token",
            MAX_DL_MB="900",
            MAX_TG_UPLOAD_MB="900",
        )
        env.pop("MAX_MEDIA_FILE_MB", None)

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from app.core.config import MAX_MEDIA_FILE_MB, MAX_DL_MB, "
                    "MAX_TG_UPLOAD_MB; print(MAX_MEDIA_FILE_MB, MAX_DL_MB, "
                    "MAX_TG_UPLOAD_MB)"
                ),
            ],
            cwd=os.getcwd(),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "900 900 900")

    def test_conflicting_legacy_media_limits_fail_startup(self):
        """Catches silently choosing one of two conflicting deployment limits."""
        env = os.environ.copy()
        env.update(
            BOT_TOKEN="test-token",
            MAX_DL_MB="1000",
            MAX_TG_UPLOAD_MB="900",
        )
        env.pop("MAX_MEDIA_FILE_MB", None)

        result = subprocess.run(
            [sys.executable, "-c", "import app.core.config"],
            cwd=os.getcwd(),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicting media file limits", result.stderr)


if __name__ == "__main__":
    unittest.main()
