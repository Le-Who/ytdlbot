import unittest
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
os.environ.setdefault("BOT_TOKEN", "test_token")

from app.core.utils import render_progressbar


class TestUI(unittest.TestCase):
    def test_render_progressbar_zero(self):
        """Test 0% progress"""
        result = render_progressbar(0, length=10)
        self.assertEqual(result, "░░░░░░░░░░ 0.0%")

    def test_render_progressbar_half(self):
        """Test 50% progress"""
        result = render_progressbar(50, length=10)
        self.assertEqual(result, "█████░░░░░ 50.0%")

    def test_render_progressbar_full(self):
        """Test 100% progress"""
        result = render_progressbar(100, length=10)
        self.assertEqual(result, "██████████ 100.0%")

    def test_render_progressbar_overflow(self):
        """Test > 100% progress (clamping)"""
        result = render_progressbar(120, length=10)
        self.assertEqual(result, "██████████ 100.0%")

    def test_render_progressbar_underflow(self):
        """Test < 0% progress (clamping)"""
        result = render_progressbar(-10, length=10)
        self.assertEqual(result, "░░░░░░░░░░ 0.0%")

    def test_render_progressbar_custom_length(self):
        """Test custom length"""
        result = render_progressbar(50, length=4)
        self.assertEqual(result, "██░░ 50.0%")


if __name__ == "__main__":
    unittest.main()
