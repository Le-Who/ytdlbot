import unittest
import asyncio
import os
from unittest.mock import MagicMock

# Add repo root to path
os.environ.setdefault("BOT_TOKEN", "123456:ABC-DEF")
os.environ.setdefault("BASE_URL", "http://localhost:8000")

from app.core.state import inflight_parsing


class TestLogicStability(unittest.IsolatedAsyncioTestCase):
    async def test_parsing_deduplication(self):
        url = "https://youtube.com/watch?v=unique_123"

        # Use a real dict for info_cache in this test
        test_info_cache = {}

        def mock_list_formats(u):
            import time

            time.sleep(0.3)
            return ("Title", [], MagicMock(), "1:00")

        async def simulated_on_message():
            if url in inflight_parsing:
                await inflight_parsing[url].wait()
                return test_info_cache.get(url)
            else:
                event = asyncio.Event()
                inflight_parsing[url] = event
                try:
                    res = await asyncio.to_thread(mock_list_formats, url)
                    test_info_cache[url] = res
                    return res
                finally:
                    event.set()
                    inflight_parsing.pop(url, None)

        t1 = asyncio.create_task(simulated_on_message())
        t2 = asyncio.create_task(simulated_on_message())

        results = await asyncio.gather(t1, t2)

        self.assertEqual(results[0][0], "Title")
        self.assertEqual(results[1][0], "Title")
        self.assertEqual(len(inflight_parsing), 0)


if __name__ == "__main__":
    unittest.main()
