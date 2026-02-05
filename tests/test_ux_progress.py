import unittest
import sys
import os

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# We need to mock environment variables before importing app.main
# because it loads them at module level and might fail if missing
os.environ['BOT_TOKEN'] = 'test_token'
os.environ['BASE_URL'] = 'http://test.com'

from app.main import render_progressbar

class TestUXProgress(unittest.TestCase):
    def test_zero_percent(self):
        # 0% -> [░░░░░░░░░░]
        bar = render_progressbar(0, length=10)
        self.assertEqual(bar, "░░░░░░░░░░ 0.0%")

    def test_fifty_percent(self):
        # 50% -> [█████░░░░░]
        bar = render_progressbar(50, length=10)
        self.assertEqual(bar, "█████░░░░░ 50.0%")

    def test_hundred_percent(self):
        # 100% -> [██████████]
        bar = render_progressbar(100, length=10)
        self.assertEqual(bar, "██████████ 100.0%")

    def test_custom_width(self):
        # 50% width 4 -> [██░░]
        bar = render_progressbar(50, length=4)
        self.assertEqual(bar, "██░░ 50.0%")

    def test_rounding(self):
        # 25% of 10 is 2.5 -> 2 blocks
        bar = render_progressbar(25, length=10)
        self.assertEqual(bar, "██░░░░░░░░ 25.0%")
        # 29% of 10 is 2.9 -> 2 blocks (floor)
        bar = render_progressbar(29, length=10)
        self.assertEqual(bar, "██░░░░░░░░ 29.0%")

if __name__ == '__main__':
    unittest.main()
