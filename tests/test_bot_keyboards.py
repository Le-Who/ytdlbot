import unittest
from unittest.mock import MagicMock
import sys
import os

# Mock external dependencies
telegram_mock = MagicMock()
sys.modules["telegram"] = telegram_mock
sys.modules["telegram.ext"] = MagicMock()

# Ensure app can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.bot.keyboards import build_format_keyboard

class TestBuildFormatKeyboard(unittest.TestCase):
    def setUp(self):
        # Reset mocks before each test
        telegram_mock.reset_mock()
        # Create a simple mock for FormatItem
        self.MockFormat = lambda label, fid: MagicMock(label=label, format_id=fid)

    def test_empty_formats(self):
        """Test with empty formats list."""
        audio = self.MockFormat("Audio Only", "audio_id")

        # Call function
        build_format_keyboard([], audio)

        # Verify InlineKeyboardMarkup was called
        # The structure should be [[Audio Button], [Close Button]]
        telegram_mock.InlineKeyboardMarkup.assert_called_once()
        args, _ = telegram_mock.InlineKeyboardMarkup.call_args
        buttons = args[0]

        self.assertEqual(len(buttons), 2) # 1 row (audio) + 1 row (close)
        self.assertEqual(len(buttons[0]), 1) # One button in audio row
        self.assertEqual(len(buttons[1]), 1) # One button in close row

        # Verify button creation
        telegram_mock.InlineKeyboardButton.assert_any_call(
            "Audio Only", callback_data="pick|audio_id"
        )
        telegram_mock.InlineKeyboardButton.assert_any_call(
            "❌ Закрыть", callback_data="close"
        )

    def test_single_format(self):
        """Test with a single video format."""
        formats = [self.MockFormat("720p", "fmt_1")]
        audio = self.MockFormat("Audio Only", "audio_id")

        build_format_keyboard(formats, audio)

        args, _ = telegram_mock.InlineKeyboardMarkup.call_args
        buttons = args[0]

        self.assertEqual(len(buttons), 3) # 1 video row + 1 audio row + 1 close row
        self.assertEqual(len(buttons[0]), 1) # 1 button in video row
        self.assertEqual(len(buttons[1]), 1) # 1 button in audio row
        self.assertEqual(len(buttons[2]), 1) # 1 button in close row

        telegram_mock.InlineKeyboardButton.assert_any_call("720p", callback_data="pick|fmt_1")
        telegram_mock.InlineKeyboardButton.assert_any_call("Audio Only", callback_data="pick|audio_id")
        telegram_mock.InlineKeyboardButton.assert_any_call("❌ Закрыть", callback_data="close")

    def test_multiple_formats_even(self):
        """Test with even number of formats (4)."""
        formats = [
            self.MockFormat("1080p", "f1"),
            self.MockFormat("720p", "f2"),
            self.MockFormat("480p", "f3"),
            self.MockFormat("360p", "f4"),
        ]
        audio = self.MockFormat("Audio", "aud")

        build_format_keyboard(formats, audio)

        args, _ = telegram_mock.InlineKeyboardMarkup.call_args
        buttons = args[0]

        # Expect:
        # Row 1: f1, f2
        # Row 2: f3, f4
        # Row 3: Audio
        # Row 4: Close
        self.assertEqual(len(buttons), 4)
        self.assertEqual(len(buttons[0]), 2)
        self.assertEqual(len(buttons[1]), 2)
        self.assertEqual(len(buttons[2]), 1)
        self.assertEqual(len(buttons[3]), 1)

    def test_multiple_formats_odd(self):
        """Test with odd number of formats (3)."""
        formats = [
            self.MockFormat("1080p", "f1"),
            self.MockFormat("720p", "f2"),
            self.MockFormat("480p", "f3"),
        ]
        audio = self.MockFormat("Audio", "aud")

        build_format_keyboard(formats, audio)

        args, _ = telegram_mock.InlineKeyboardMarkup.call_args
        buttons = args[0]

        # Expect:
        # Row 1: f1, f2
        # Row 2: f3
        # Row 3: Audio
        # Row 4: Close
        self.assertEqual(len(buttons), 4)
        self.assertEqual(len(buttons[0]), 2)
        self.assertEqual(len(buttons[1]), 1)
        self.assertEqual(len(buttons[2]), 1)
        self.assertEqual(len(buttons[3]), 1)

    def test_formats_limit(self):
        """Test that formats are limited to 8."""
        formats = [self.MockFormat(f"fmt_{i}", f"id_{i}") for i in range(10)]
        audio = self.MockFormat("Audio", "aud")

        build_format_keyboard(formats, audio)

        args, _ = telegram_mock.InlineKeyboardMarkup.call_args
        buttons = args[0]

        # Expect:
        # 4 rows of 2 (8 items)
        # 1 row of audio
        # 1 row of Close
        # Total 6 rows
        self.assertEqual(len(buttons), 6)

        # Check that the 9th item was NOT added
        calls = telegram_mock.InlineKeyboardButton.call_args_list
        # Extract callback_data from calls
        callback_datas = [c.kwargs.get('callback_data') for c in calls]

        self.assertIn("pick|id_7", callback_datas)
        self.assertNotIn("pick|id_8", callback_datas)
        self.assertIn("close", callback_datas)

    def test_callback_data_structure(self):
        """Verify callback data format."""
        formats = [self.MockFormat("Label", "MY_FORMAT_ID")]
        audio = self.MockFormat("Audio", "MY_AUDIO_ID")

        build_format_keyboard(formats, audio)

        telegram_mock.InlineKeyboardButton.assert_any_call(
            "Label", callback_data="pick|MY_FORMAT_ID"
        )
        telegram_mock.InlineKeyboardButton.assert_any_call(
            "Audio", callback_data="pick|MY_AUDIO_ID"
        )
        telegram_mock.InlineKeyboardButton.assert_any_call(
            "❌ Закрыть", callback_data="close"
        )

if __name__ == "__main__":
    unittest.main()
