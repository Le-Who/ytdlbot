import unittest
import os
import sys

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock BOT_TOKEN before importing app.main
os.environ["BOT_TOKEN"] = "test_token"

from app.main import render_progressbar

class TestProgressBar(unittest.TestCase):
    def test_render_progressbar(self):
        self.assertEqual(render_progressbar(0), "[░░░░░░░░░░]")
        self.assertEqual(render_progressbar(50), "[█████░░░░░]")
        self.assertEqual(render_progressbar(100), "[██████████]")
        self.assertEqual(render_progressbar(25), "[██░░░░░░░░]")

    def test_custom_length(self):
        self.assertEqual(render_progressbar(50, length=20), "[██████████░░░░░░░░░░]")

    def test_bounds(self):
        self.assertEqual(render_progressbar(-10), "[░░░░░░░░░░]")
        self.assertEqual(render_progressbar(150), "[██████████]")

if __name__ == '__main__':
    unittest.main()
