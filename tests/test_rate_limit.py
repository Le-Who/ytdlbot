import unittest
import os
import sys
import importlib
import time
from unittest.mock import MagicMock, patch

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestRateLimit(unittest.TestCase):
    def setUp(self):
        # Mock dependencies
        self.mock_user_rates = {}
        self.mock_state_module = MagicMock()
        # Mocking dictionary-like behavior for TTLCache
        self.mock_state_module.user_rates = self.mock_user_rates
        self.mock_state_module.active_processes_lock = MagicMock()
        self.mock_state_module.active_processes = set()

        self.modules_patcher = patch.dict(sys.modules, {
            "app.core.state": self.mock_state_module,
        })
        self.modules_patcher.start()

        # Import the module under test and reload to pick up mocks
        import app.core.utils
        importlib.reload(app.core.utils)
        self.utils = app.core.utils

    def tearDown(self):
        self.modules_patcher.stop()

    def test_check_rate_limit_enforcement(self):
        user_id = 123
        limit = 3

        # Mock time.time to control the "minute"
        with patch('time.time') as mock_time:
            current_time = 1000.0
            mock_time.return_value = current_time

            # First 3 requests should be allowed
            self.assertTrue(self.utils.check_rate_limit(user_id, limit=limit))
            self.assertTrue(self.utils.check_rate_limit(user_id, limit=limit))
            self.assertTrue(self.utils.check_rate_limit(user_id, limit=limit))

            # 4th request should be denied
            self.assertFalse(self.utils.check_rate_limit(user_id, limit=limit))

            # Move time forward by 30 seconds, still denied (within same minute)
            mock_time.return_value = current_time + 30
            self.assertFalse(self.utils.check_rate_limit(user_id, limit=limit))

            # Move time forward by 61 seconds from start, should be allowed again
            # Actually, if we have 3 requests at T=1000, at T=1061 they are all expired.
            mock_time.return_value = current_time + 61
            self.assertTrue(self.utils.check_rate_limit(user_id, limit=limit))

    def test_check_rate_limit_sliding_window(self):
        user_id = 456
        limit = 2

        with patch('time.time') as mock_time:
            mock_time.return_value = 1000.0
            self.assertTrue(self.utils.check_rate_limit(user_id, limit=limit)) # T=1000

            mock_time.return_value = 1030.0
            self.assertTrue(self.utils.check_rate_limit(user_id, limit=limit)) # T=1030

            # limit reached
            self.assertFalse(self.utils.check_rate_limit(user_id, limit=limit)) # T=1030

            mock_time.return_value = 1061.0
            # At T=1061, the T=1000 request is expired, but T=1030 is still there.
            # Count is 1, so 1 more allowed.
            self.assertTrue(self.utils.check_rate_limit(user_id, limit=limit)) # T=1061

            # Now count is 2 (T=1030, T=1061), next denied
            self.assertFalse(self.utils.check_rate_limit(user_id, limit=limit)) # T=1061

if __name__ == '__main__':
    unittest.main()
