import unittest

# Ensure app can be imported
from app.bot.keyboards import build_format_keyboard
from app.services.ytdlp.models import FormatItem


from unittest.mock import patch, MagicMock


class TestBuildFormatKeyboard(unittest.TestCase):
    def setUp(self):
        self.patcher = patch("app.bot.keyboards.format_label")
        self.mock_format_label = self.patcher.start()
        self.mock_format_label.side_effect = lambda x: getattr(
            x, "_test_label", "Unknown"
        )

    def tearDown(self):
        self.patcher.stop()

    def _make_format(self, label: str, fid: str) -> MagicMock:
        return MagicMock(spec=FormatItem, format_id=fid, _test_label=label)

    def test_empty_formats(self):
        audio = self._make_format("Audio Only", "audio_id")
        markup = build_format_keyboard([], audio)
        buttons = markup.inline_keyboard

        self.assertEqual(len(buttons), 1)  # One row (audio)
        self.assertEqual(len(buttons[0]), 1)
        self.assertEqual(buttons[0][0].text, "Audio Only")
        self.assertEqual(buttons[0][0].callback_data, "pick|0")

    def test_single_format(self):
        formats = [self._make_format("720p", "fmt_1")]
        audio = self._make_format("Audio Only", "audio_id")
        markup = build_format_keyboard(formats, audio)
        buttons = markup.inline_keyboard

        self.assertEqual(len(buttons), 2)  # 1 video row + 1 audio row
        self.assertEqual(len(buttons[0]), 1)
        self.assertEqual(len(buttons[1]), 1)
        self.assertEqual(buttons[0][0].text, "720p")
        self.assertEqual(buttons[1][0].text, "Audio Only")

    def test_multiple_formats_even(self):
        formats = [
            self._make_format("1080p", "f1"),
            self._make_format("720p", "f2"),
            self._make_format("480p", "f3"),
            self._make_format("360p", "f4"),
        ]
        audio = self._make_format("Audio", "aud")
        markup = build_format_keyboard(formats, audio)
        buttons = markup.inline_keyboard

        # Row 1: f1, f2 | Row 2: f3, f4 | Row 3: Audio
        self.assertEqual(len(buttons), 3)
        self.assertEqual(len(buttons[0]), 2)
        self.assertEqual(len(buttons[1]), 2)
        self.assertEqual(len(buttons[2]), 1)

    def test_multiple_formats_odd(self):
        formats = [
            self._make_format("1080p", "f1"),
            self._make_format("720p", "f2"),
            self._make_format("480p", "f3"),
        ]
        audio = self._make_format("Audio", "aud")
        markup = build_format_keyboard(formats, audio)
        buttons = markup.inline_keyboard

        # Row 1: f1, f2 | Row 2: f3 | Row 3: Audio
        self.assertEqual(len(buttons), 3)
        self.assertEqual(len(buttons[0]), 2)
        self.assertEqual(len(buttons[1]), 1)
        self.assertEqual(len(buttons[2]), 1)

    def test_formats_limit(self):
        formats = [self._make_format(f"fmt_{i}", f"id_{i}") for i in range(10)]
        audio = self._make_format("Audio", "aud")
        markup = build_format_keyboard(formats, audio)
        buttons = markup.inline_keyboard

        # 4 rows of 2 (8 items) + 1 row of audio = 5 rows
        self.assertEqual(len(buttons), 5)

        # Verify 9th item (id_8) was NOT added
        all_data = [btn.callback_data for row in buttons for btn in row]
        self.assertIn("pick|7", all_data)
        self.assertNotIn("pick|8", all_data[:-1])  # Audio append at the very end will be pick|8
        self.assertEqual(all_data[-1], "pick|8")  # Audio is index 8

    def test_callback_data_structure(self):
        formats = [self._make_format("Label", "MY_FORMAT_ID")]
        audio = self._make_format("Audio", "MY_AUDIO_ID")
        markup = build_format_keyboard(formats, audio)
        buttons = markup.inline_keyboard

        all_data = [btn.callback_data for row in buttons for btn in row]
        self.assertIn("pick|0", all_data)
        self.assertIn("pick|1", all_data)


if __name__ == "__main__":
    unittest.main()
