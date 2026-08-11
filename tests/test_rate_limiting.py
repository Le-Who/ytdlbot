import unittest
import sys
import time
from unittest.mock import MagicMock, patch
import importlib

# Add repo root to path
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestRateLimiting(unittest.TestCase):
    def setUp(self):
        # Mock app.core.state
        self.mock_state = MagicMock()
        # Use a real dict for user_rates to test logic
        self.mock_state.user_rates = {}
        self.mock_state.active_processes_lock = MagicMock()
        self.mock_state.active_processes = set()

        self.modules_patcher = patch.dict(
            sys.modules,
            {
                "app.core.state": self.mock_state,
            },
        )
        self.modules_patcher.start()

        # Ensure app.constants is importable
        # We can just let it import normally if present, or mock it if dependencies issue.
        # app.constants has no dependencies, so it's safe.

        import app.core.utils

        importlib.reload(app.core.utils)
        self.utils = app.core.utils

    def tearDown(self):
        self.modules_patcher.stop()

    def test_rate_limit_enforcement(self):
        """Test that rate limit blocks requests after limit is reached."""
        user_id = 123
        limit = 3

        # Should allow 'limit' requests
        for i in range(limit):
            result = self.utils.check_rate_limit(user_id, limit)
            self.assertTrue(result, f"Request {i+1} failed but should pass")

        # Should block next request
        result = self.utils.check_rate_limit(user_id, limit)
        self.assertFalse(result, "Request exceeding limit passed but should fail")

    @patch("time.time")
    def test_rate_limit_expiry(self, mock_time):
        """Test that rate limit resets after window expires."""
        user_id = 456
        limit = 2

        # Start at time 1000
        mock_time.return_value = 1000.0

        # 1. Fill the limit
        self.assertTrue(self.utils.check_rate_limit(user_id, limit))
        self.assertTrue(self.utils.check_rate_limit(user_id, limit))
        self.assertFalse(self.utils.check_rate_limit(user_id, limit))

        # 2. Advance time by 30 seconds (still blocked)
        mock_time.return_value = 1000.0 + 30.0
        self.assertFalse(self.utils.check_rate_limit(user_id, limit))

        # 3. Advance time by 61 seconds (should be allowed)
        # 1000 + 61 = 1061
        mock_time.return_value = 1000.0 + 61.0
        self.assertTrue(self.utils.check_rate_limit(user_id, limit))


if __name__ == "__main__":
    unittest.main()
