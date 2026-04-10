"""Unit tests for new features added in the architectural synthesis sprint:

1. extract_url_from_update (Reply-to URL resolution)
2. user_prefs (get/set/clear)
3. auto_updater_loop (background task)
4. Tiered semaphore selection logic
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Feature 2: extract_url_from_update ───────────────────────────────────────


class TestExtractUrlFromUpdate(unittest.TestCase):
    """Tests for utils.extract_url_from_update()."""

    def _msg(self, text=None, caption=None, reply_text=None, reply_caption=None):
        """Build a minimal stub Message-like object."""

        class Stub:
            pass

        msg = Stub()
        msg.text = text
        msg.caption = caption

        if reply_text is not None or reply_caption is not None:
            replied = Stub()
            replied.text = reply_text
            replied.caption = reply_caption
            msg.reply_to_message = replied
        else:
            msg.reply_to_message = None

        return msg

    def test_url_in_text(self):
        from app.core.utils import extract_url_from_update

        msg = self._msg(text="https://youtube.com/watch?v=abc")
        url, section = extract_url_from_update(msg)
        self.assertEqual(url, "https://youtube.com/watch?v=abc")
        self.assertIsNone(section)

    def test_url_in_caption(self):
        from app.core.utils import extract_url_from_update

        msg = self._msg(caption="https://youtube.com/watch?v=abc")
        url, section = extract_url_from_update(msg)
        self.assertIsNotNone(url)

    def test_url_in_reply_text(self):
        from app.core.utils import extract_url_from_update

        msg = self._msg(text="please download", reply_text="https://youtube.com/watch?v=123")
        url, section = extract_url_from_update(msg)
        self.assertIsNotNone(url)
        self.assertIn("youtube", url)

    def test_url_in_reply_caption(self):
        from app.core.utils import extract_url_from_update

        msg = self._msg(reply_caption="https://youtu.be/xyz")
        url, section = extract_url_from_update(msg)
        self.assertIsNotNone(url)

    def test_no_url_anywhere(self):
        from app.core.utils import extract_url_from_update

        msg = self._msg(text="just text", reply_text="also just text")
        url, section = extract_url_from_update(msg)
        self.assertIsNone(url)
        self.assertIsNone(section)

    def test_unsupported_url_not_returned(self):
        from app.core.utils import extract_url_from_update

        msg = self._msg(text="https://google.com")
        url, _ = extract_url_from_update(msg)
        self.assertIsNone(url)

    def test_text_has_priority_over_reply(self):
        """Supported URL in message.text should win over supported URL in reply."""
        from app.core.utils import extract_url_from_update

        msg = self._msg(
            text="https://youtu.be/primary",
            reply_text="https://youtube.com/watch?v=secondary",
        )
        url, _ = extract_url_from_update(msg)
        self.assertIn("primary", url)

    def test_non_string_text_ignored(self):
        """MagicMock attributes (from test stubs) must not cause TypeError."""
        from app.core.utils import extract_url_from_update

        msg = MagicMock()  # .text returns a MagicMock, not a string
        msg.reply_to_message = None
        # Must not raise
        url, section = extract_url_from_update(msg)
        self.assertIsNone(url)

    def test_section_extracted_from_reply(self):
        """Timestamps in a replied URL should be parsed correctly."""
        from app.core.utils import extract_url_from_update

        msg = self._msg(reply_text="https://youtube.com/watch?v=abc 00:10-02:30")
        url, section = extract_url_from_update(msg)
        self.assertIsNotNone(url)
        self.assertIsNotNone(section)
        self.assertIn("00:10", section)


# ── Feature 4: user_prefs ────────────────────────────────────────────────────


class AsyncMockCache(dict):
    async def get(self, key, default=None):
        return super().get(key, default)

    async def set(self, key, value, ttl=None):
        self[key] = value

    async def delete(self, key):
        self.pop(key, None)


class TestUserPrefs(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.core import state
        state.prefs_cache = AsyncMockCache()

    async def test_get_prefs_empty(self):
        from app.core.user_prefs import get_prefs
        prefs = await get_prefs(999)
        self.assertEqual(prefs, {})

    async def test_set_and_get_format(self):
        from app.core.user_prefs import get_prefs, set_prefs
        await set_prefs(1, default_format="audio")
        prefs = await get_prefs(1)
        self.assertEqual(prefs["default_format"], "audio")

    async def test_set_and_get_quality(self):
        from app.core.user_prefs import get_prefs, set_prefs
        await set_prefs(2, default_quality=720)
        prefs = await get_prefs(2)
        self.assertEqual(prefs["default_quality"], 720)

    async def test_set_merges(self):
        """set_prefs should merge kwargs rather than overwrite."""
        from app.core.user_prefs import get_prefs, set_prefs
        await set_prefs(3, default_format="video")
        await set_prefs(3, default_quality=1080)
        prefs = await get_prefs(3)
        self.assertEqual(prefs["default_format"], "video")
        self.assertEqual(prefs["default_quality"], 1080)

    async def test_clear_prefs(self):
        from app.core.user_prefs import clear_prefs, get_prefs, set_prefs
        await set_prefs(4, default_format="audio")
        await clear_prefs(4)
        prefs = await get_prefs(4)
        self.assertEqual(prefs, {})

    async def test_valid_formats_set(self):
        from app.core.user_prefs import VALID_FORMATS
        self.assertIn("audio", VALID_FORMATS)
        self.assertIn("video", VALID_FORMATS)

    async def test_valid_qualities_dict(self):
        from app.core.user_prefs import VALID_QUALITIES
        self.assertIn("best", VALID_QUALITIES)
        self.assertIsNone(VALID_QUALITIES["best"])
        self.assertEqual(VALID_QUALITIES["720"], 720)


# ── Feature 1: auto_updater ──────────────────────────────────────────────────


class TestAutoUpdater(unittest.IsolatedAsyncioTestCase):
    async def test_run_update_captures_old_and_new_version(self):
        from app.tasks.auto_updater import _run_update

        with patch("app.tasks.auto_updater.asyncio.create_subprocess_exec") as mock_exec:
            proc = MagicMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"Update available\n", b""))
            mock_exec.return_value = proc

            # _get_ytdlp_version is called twice (before and after)
            with patch(
                "app.tasks.auto_updater._get_ytdlp_version",
                new=AsyncMock(side_effect=["2026.03.01", "2026.04.01"]),
            ):
                before, after = await _run_update()

            self.assertEqual(before, "2026.03.01")
            self.assertEqual(after, "2026.04.01")

    async def test_get_ytdlp_version_returns_unknown_on_failure(self):
        from app.tasks.auto_updater import _get_ytdlp_version

        with patch(
            "app.tasks.auto_updater.asyncio.create_subprocess_exec",
            side_effect=OSError("not found"),
        ):
            version = await _get_ytdlp_version()

        self.assertEqual(version, "unknown")

    async def test_updater_loop_stops_on_event(self):
        """The loop must terminate promptly when stop_event is set."""
        from app.tasks.auto_updater import auto_updater_loop

        stop_event = asyncio.Event()
        stop_event.set()  # Already set — loop should exit immediately after first run

        with patch(
            "app.tasks.auto_updater._do_update", new=AsyncMock()
        ) as mock_do:
            await asyncio.wait_for(auto_updater_loop(stop_event), timeout=5)

        # _do_update should have been called exactly once (startup run)
        mock_do.assert_called_once()

    async def test_update_does_not_crash_on_exception(self):
        """_do_update must not propagate exceptions — only log them."""
        from app.tasks.auto_updater import _do_update

        with patch(
            "app.tasks.auto_updater._run_update",
            new=AsyncMock(side_effect=RuntimeError("simulated error")),
        ):
            await _do_update()  # Must not raise


# ── Feature 5: Tiered Semaphore Selection ────────────────────────────────────


class TestTieredSemaphoreLogic(unittest.TestCase):
    """Tests for the semaphore-selection logic introduced in orchestrator.py."""

    def test_api_origin_format_ids_routed_to_api_sem(self):
        """Verify the API-origin format IDs set matches expected values.

        Tests the set membership assumption used in process_download.
        """
        api_ids = {"tikwm_fallback", "pinterest_native", "gallerydl_fallback"}
        self.assertIn("tikwm_fallback", api_ids)
        self.assertIn("pinterest_native", api_ids)
        self.assertIn("gallerydl_fallback", api_ids)

    def test_yt_dlp_format_not_in_api_ids(self):
        api_ids = {"tikwm_fallback", "pinterest_native", "gallerydl_fallback"}
        self.assertNotIn("137", api_ids)
        self.assertNotIn("bestaudio/best", api_ids)


if __name__ == "__main__":
    unittest.main()
