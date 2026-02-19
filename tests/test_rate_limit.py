import unittest
from unittest.mock import MagicMock, patch
import time
import sys
import importlib
import types

# Create a mock state module
mock_state = MagicMock()
mock_state.user_rates = {}
mock_state.active_processes_lock = MagicMock()
mock_state.active_processes = set()

# Pre-mock cachetools to avoid import error
sys.modules["cachetools"] = MagicMock()

class TestRateLimit(unittest.TestCase):
    def setUp(self):
        self.mock_state = MagicMock()
        # Ensure user_rates behaves like a dict but we can mock get/set
        self.mock_state.user_rates = MagicMock()
        # By default get returns None or a value.
        # We need it to behave like a dict for normal tests.
        # So we use a side_effect that delegates to a real dict?
        # Or just use a real dict for normal tests and mock it for exception test.
        self._real_dict = {}

        def get_side_effect(key, default=None):
            return self._real_dict.get(key, default)

        def setitem_side_effect(key, value):
            self._real_dict[key] = value

        self.mock_state.user_rates.get.side_effect = get_side_effect
        self.mock_state.user_rates.__setitem__.side_effect = setitem_side_effect

        self.mock_state.active_processes_lock = MagicMock()
        self.mock_state.active_processes = set()

    @patch("time.time")
    def test_rate_limit_enforcement(self, mock_time):
        # Freeze time at 1000.0
        mock_time.return_value = 1000.0
        user_id = 123
        limit = 5

        with patch.dict(sys.modules, {"app.core.state": self.mock_state}):
            import app.core.utils
            importlib.reload(app.core.utils)
            from app.core.utils import check_rate_limit

            # First 5 requests should pass
            for _ in range(limit):
                self.assertTrue(check_rate_limit(user_id, limit))

            # 6th request should fail
            self.assertFalse(check_rate_limit(user_id, limit))

    @patch("time.time")
    def test_rate_limit_window_reset(self, mock_time):
        user_id = 456
        limit = 2

        with patch.dict(sys.modules, {"app.core.state": self.mock_state}):
            import app.core.utils
            importlib.reload(app.core.utils)
            from app.core.utils import check_rate_limit

            # Minute 0: 2 requests ok
            mock_time.return_value = 60.0 # minute 1
            self.assertTrue(check_rate_limit(user_id, limit))
            self.assertTrue(check_rate_limit(user_id, limit))
            self.assertFalse(check_rate_limit(user_id, limit))

            # Minute 1: should be ok again
            mock_time.return_value = 125.0 # minute 2
            self.assertTrue(check_rate_limit(user_id, limit))

    def test_rate_limit_exception_logging(self):
        # We need to patch sys.modules BEFORE importing app.core.utils
        with patch.dict(sys.modules, {"app.core.state": self.mock_state}):
             import app.core.utils
             importlib.reload(app.core.utils)

             # Now patch logger on the reloaded module
             with patch("app.core.utils.logger") as mock_logger:
                 from app.core.utils import check_rate_limit

                 # Make user_rates.get raise exception
                 self.mock_state.user_rates.get.side_effect = Exception("DB Fail")

                 # Should return True (fail open)
                 result = check_rate_limit(789, 5)
                 self.assertTrue(result)

                 # Should log error
                 mock_logger.error.assert_called()
                 self.assertIn("Rate limit check failed", mock_logger.error.call_args[0][0])

if __name__ == "__main__":
    unittest.main()
