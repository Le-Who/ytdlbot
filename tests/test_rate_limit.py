import unittest
import sys
import os
import time
from unittest.mock import MagicMock, patch

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestRateLimit(unittest.TestCase):
    def setUp(self):
        self.mock_state = MagicMock()
        self.mock_state.user_rates = {} # Mock cache
        self.mock_state.active_processes_lock = MagicMock()
        self.mock_state.active_processes = set()

        self.modules_patcher = patch.dict(sys.modules, {
            "app.core.state": self.mock_state,
        })
        self.modules_patcher.start()

        # Reload utils to pick up the mock
        import app.core.utils
        import importlib
        importlib.reload(app.core.utils)
        self.utils = app.core.utils

    def tearDown(self):
        self.modules_patcher.stop()

    def test_rate_limit_enforcement(self):
        user_id = 456
        limit = 3

        # 1. First 3 requests should pass
        self.assertTrue(self.utils.check_rate_limit(user_id, limit))
        self.assertTrue(self.utils.check_rate_limit(user_id, limit))
        self.assertTrue(self.utils.check_rate_limit(user_id, limit))

        # 2. 4th request should fail
        self.assertFalse(self.utils.check_rate_limit(user_id, limit))

        # Verify cache state
        self.assertEqual(len(self.mock_state.user_rates[user_id]), 3)

    @patch('time.time')
    def test_rate_limit_expiration(self, mock_time):
        user_id = 789
        limit = 2

        # Start at t=100
        mock_time.return_value = 100.0
        self.assertTrue(self.utils.check_rate_limit(user_id, limit)) # count=1

        mock_time.return_value = 110.0
        self.assertTrue(self.utils.check_rate_limit(user_id, limit)) # count=2

        mock_time.return_value = 120.0
        self.assertFalse(self.utils.check_rate_limit(user_id, limit)) # Blocked

        # Advance time past 60s from first request (100+60=160)
        # But second request was at 110 (expires at 170).

        mock_time.return_value = 165.0
        # First request (100) expired. Second (110) still valid.
        # Active timestamps: [110]. count=1.
        # Should allow 1 more.

        self.assertTrue(self.utils.check_rate_limit(user_id, limit)) # count=2 (110, 165)

        # Now active: [110, 165]
        self.assertFalse(self.utils.check_rate_limit(user_id, limit)) # Blocked

        # Advance past 170 (second request expires)
        mock_time.return_value = 175.0
        # Active: [165]. count=1.
        self.assertTrue(self.utils.check_rate_limit(user_id, limit)) # count=2 (165, 175)

if __name__ == '__main__':
    unittest.main()
