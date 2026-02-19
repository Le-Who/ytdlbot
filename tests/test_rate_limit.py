import unittest
import sys
import os
import importlib
from unittest.mock import MagicMock, patch

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestRateLimit(unittest.TestCase):
    def setUp(self):
        # Mock app.core.state
        self.mock_state = MagicMock()
        # Use a real dict for user_rates to test logic
        self.mock_state.user_rates = {}
        self.mock_state.active_processes_lock = MagicMock()
        self.mock_state.active_processes = set()

        self.modules_patcher = patch.dict(sys.modules, {
            "app.core.state": self.mock_state,
        })
        self.modules_patcher.start()

        # Import/Reload the module under test to use the mocked state
        import app.core.utils
        importlib.reload(app.core.utils)
        self.utils = app.core.utils

    def tearDown(self):
        self.modules_patcher.stop()

    def test_rate_limit_logic(self):
        # Mock time.time to return a fixed value
        with patch('app.core.utils.time.time') as mock_time:
            # T=600 (Minute 10)
            mock_time.return_value = 600.0
            user_id = 123
            limit = 3

            # Req 1
            self.assertTrue(self.utils.check_rate_limit(user_id, limit))
            # Req 2
            self.assertTrue(self.utils.check_rate_limit(user_id, limit))
            # Req 3
            self.assertTrue(self.utils.check_rate_limit(user_id, limit))
            # Req 4 (Blocked)
            self.assertFalse(self.utils.check_rate_limit(user_id, limit))

            # Check key
            key = "123:10"
            self.assertEqual(self.mock_state.user_rates.get(key), 3)

            # Advance to T=660 (Minute 11)
            mock_time.return_value = 660.0

            # Req 5 (Allowed, new minute)
            self.assertTrue(self.utils.check_rate_limit(user_id, limit))
            key_new = "123:11"
            self.assertEqual(self.mock_state.user_rates.get(key_new), 1)

    def test_consistent_usage_no_block(self):
        """Verify that 1 req every 30s (limit 3/min) is never blocked."""
        with patch('app.core.utils.time.time') as mock_time:
            user_id = 444
            limit = 3
            start_time = 1000.0 # 1000/60 = 16.66 (Min 16)

            # Req 1 (T=1000, Min 16)
            mock_time.return_value = start_time
            self.assertTrue(self.utils.check_rate_limit(user_id, limit))

            # Req 2 (T=1030, Min 17) -> 1030/60 = 17.16
            mock_time.return_value = start_time + 30
            self.assertTrue(self.utils.check_rate_limit(user_id, limit))

            # Req 3 (T=1060, Min 17) -> 1060/60 = 17.66
            mock_time.return_value = start_time + 60
            self.assertTrue(self.utils.check_rate_limit(user_id, limit))

            # Req 4 (T=1090, Min 18) -> 1090/60 = 18.16
            mock_time.return_value = start_time + 90
            self.assertTrue(self.utils.check_rate_limit(user_id, limit))

            # Check counts
            self.assertEqual(self.mock_state.user_rates.get("444:16"), 1)
            self.assertEqual(self.mock_state.user_rates.get("444:17"), 2)
            self.assertEqual(self.mock_state.user_rates.get("444:18"), 1)

if __name__ == "__main__":
    unittest.main()
