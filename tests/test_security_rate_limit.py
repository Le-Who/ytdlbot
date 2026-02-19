import unittest
import sys
import os
import time
from unittest.mock import MagicMock, patch
import importlib

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestRateLimit(unittest.TestCase):
    def setUp(self):
        # Mock app.core.state
        self.mock_state = MagicMock()
        # Use a real dict for user_rates to simulate cache storage
        self.mock_state.user_rates = {}

        self.modules_patcher = patch.dict(sys.modules, {
            "app.core.state": self.mock_state,
        })
        self.modules_patcher.start()

        # Mock time to have consistent behavior
        self.time_patcher = patch("time.time")
        self.mock_time = self.time_patcher.start()
        self.mock_time.return_value = 1000.0 # Fixed time

        # Reload app.core.utils to pick up mocks
        import app.core.utils
        importlib.reload(app.core.utils)
        self.utils = app.core.utils

    def tearDown(self):
        self.modules_patcher.stop()
        self.time_patcher.stop()

    def test_check_rate_limit_enforcement(self):
        user_id = 12345
        limit = 3

        # 1. First request
        allowed = self.utils.check_rate_limit(user_id, limit)
        self.assertTrue(allowed, "1st request should be allowed")

        # 2. Second request
        allowed = self.utils.check_rate_limit(user_id, limit)
        self.assertTrue(allowed, "2nd request should be allowed")

        # 3. Third request
        allowed = self.utils.check_rate_limit(user_id, limit)
        self.assertTrue(allowed, "3rd request should be allowed")

        # 4. Fourth request - Should be blocked
        allowed = self.utils.check_rate_limit(user_id, limit)
        self.assertFalse(allowed, "4th request should be blocked")

    def test_check_rate_limit_reset(self):
        user_id = 67890
        limit = 1

        # 1. First request
        self.assertTrue(self.utils.check_rate_limit(user_id, limit))

        # 2. Blocked in same minute
        self.assertFalse(self.utils.check_rate_limit(user_id, limit))

        # Move time forward by 61 seconds (next minute window)
        self.mock_time.return_value = 1061.0

        # 3. Should be allowed again
        self.assertTrue(self.utils.check_rate_limit(user_id, limit))
