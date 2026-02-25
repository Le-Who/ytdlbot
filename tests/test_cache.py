import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
import sys

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))

from app.core.cache import FileTTLCache
from app.core.utils import safe_remove

class TestFileTTLCache(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.files = []

    def tearDown(self):
        for f in self.files:
            if os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass
        os.rmdir(self.temp_dir)

    def create_file(self):
        fd, path = tempfile.mkstemp(dir=self.temp_dir)
        os.close(fd)
        self.files.append(path)
        return path

    def test_sync_clear(self):
        # Verify sync behavior (no loop)
        cache = FileTTLCache(maxsize=10, ttl=60, on_eviction=safe_remove)
        f1 = self.create_file()
        f2 = self.create_file()
        cache['k1'] = f1
        cache['k2'] = f2

        cache.clear()

        self.assertFalse(os.path.exists(f1))
        self.assertFalse(os.path.exists(f2))
        self.assertEqual(len(cache), 0)

    def test_async_slow_eviction(self):
        # Mock slow eviction to verify non-blocking behavior
        def slow_eviction(val):
            time.sleep(0.5)
            safe_remove(val)

        async def run_test():
            cache = FileTTLCache(maxsize=10, ttl=60, on_eviction=slow_eviction)
            f1 = self.create_file()
            cache['k1'] = f1

            start = time.time()
            cache.clear()
            end = time.time()

            # This should be fast if optimized, slow if not
            self.assertLess(end - start, 0.2, "clear() blocked for too long")

            # Wait for background task
            await asyncio.sleep(0.6)
            self.assertFalse(os.path.exists(f1))

        asyncio.run(run_test())

    def test_async_popitem(self):
        def slow_eviction(val):
            time.sleep(0.1)
            safe_remove(val)

        async def run_test():
            cache = FileTTLCache(maxsize=10, ttl=60, on_eviction=slow_eviction)
            f1 = self.create_file()
            cache['k1'] = f1

            start = time.time()
            cache.popitem()
            end = time.time()

            self.assertLess(end - start, 0.05, "popitem() blocked for too long")

            # Should be gone eventually
            await asyncio.sleep(0.2)
            self.assertFalse(os.path.exists(f1))

        asyncio.run(run_test())

if __name__ == '__main__':
    unittest.main()
