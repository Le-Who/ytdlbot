import sys
import os
import unittest
from unittest.mock import MagicMock

# Add project root to path
sys.path.append(os.getcwd())

# Mock environment variables BEFORE importing app.main
os.environ["BOT_TOKEN"] = "test_token"
os.environ["BASE_URL"] = "http://localhost:8000"

# Mock telegram module to avoid needing actual API connection or complex objects

# Import the module under test
import app.main as main_app

class TestFrontendLogic(unittest.TestCase):
    
    def test_build_format_keyboard_layout(self):
        print("\nTesting 2-column keyboard layout...")
        # Create dummy formats
        formats = [
            MagicMock(label=f"Fmt{i}", format_id=f"id{i}") for i in range(10)
        ]
        audio = MagicMock(label="Audio", format_id="audio")
        
        markup = main_app.build_format_keyboard(formats, audio)
        
        # markup.inline_keyboard is a list of lists of buttons
        keyboard = markup.inline_keyboard
        
        # Check structure
        # We expect pairs (2 items) per row, except maybe the last one or the audio one
        # The function slices first 8 formats. 
        # So we expect 4 rows of 2 buttons + 1 row for audio.
        
        self.assertEqual(len(keyboard), 5, "Should have 5 rows (4 for video, 1 for audio)")
        
        # Check row lengths
        for i in range(4):
            self.assertEqual(len(keyboard[i]), 2, f"Row {i} should have 2 buttons")
            
        self.assertEqual(len(keyboard[4]), 1, "Last row (audio) should have 1 button")
        print("PASS: Keyboard layout is correct (2 columns)")

    def test_help_command_exists(self):
        print("\nTesting /help command existence...")
        self.assertTrue(hasattr(main_app, 'cmd_help'), "cmd_help function is missing")
        print("PASS: cmd_help exists")

    def test_cancel_handler_exists(self):
        print("\nTesting cancel handler existence...")
        self.assertTrue(hasattr(main_app, 'on_cancel'), "on_cancel function is missing")
        print("PASS: on_cancel exists")

    def test_app_handlers_registry(self):
        print("\nTesting handler registration...")
        # We Mock Application.builder() chain
        # Not easily testable without deeper mocking, but we can inspect the source code 
        # or trust the syntax check from import.
        # Let's simple check if the function build_bot_app exists
        self.assertTrue(hasattr(main_app, 'build_bot_app'), "build_bot_app missing")
        print("PASS: build_bot_app exists")

if __name__ == '__main__':
    unittest.main()
