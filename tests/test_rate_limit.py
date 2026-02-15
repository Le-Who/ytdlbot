import unittest
from unittest.mock import patch, MagicMock
from app.core import utils, state
import time

class TestRateLimit(unittest.TestCase):
    def setUp(self):
        # Clear rate limit cache before each test
        state.user_rates.clear()

    def test_check_rate_limit(self):
        user_id = 12345
        limit = 5

        # Perform 5 allowed requests
        for i in range(limit):
            self.assertTrue(utils.check_rate_limit(user_id, limit), f"Request {i+1} should be allowed")

        # The 6th request should be denied
        self.assertFalse(utils.check_rate_limit(user_id, limit), "Request 6 should be denied")
