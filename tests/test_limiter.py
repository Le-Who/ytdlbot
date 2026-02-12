import time
import unittest

from app.core.limiter import TokenBucketLimiter


class TestLimiter(unittest.TestCase):
    def test_token_consumption_and_refill(self):
        limiter = TokenBucketLimiter(capacity=2, refill_rate=1)
        self.assertTrue(limiter.allow("k"))
        self.assertTrue(limiter.allow("k"))
        self.assertFalse(limiter.allow("k"))
        time.sleep(1.1)
        self.assertTrue(limiter.allow("k"))


if __name__ == "__main__":
    unittest.main()
