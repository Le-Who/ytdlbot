import os
import unittest
from unittest.mock import patch

from app.core import utils


class TestCoreUtils(unittest.TestCase):
    @patch("os.path.exists", return_value=True)
    @patch("os.unlink")
    def test_safe_remove(self, mock_unlink, _):
        utils.safe_remove("a")
        mock_unlink.assert_called_once_with("a")

    def test_render_progressbar(self):
        self.assertEqual(utils.render_progressbar(0), "░" * 15 + " 0.0%")
        self.assertEqual(utils.render_progressbar(100), "█" * 15 + " 100.0%")


if __name__ == "__main__":
    unittest.main()
