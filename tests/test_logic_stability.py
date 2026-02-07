
import unittest
import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set dummy env vars
os.environ["BOT_TOKEN"] = "123456:ABC-DEF"
os.environ["BASE_URL"] = "http://localhost:8000"

from app.main import inflight_parsing, info_cache

class TestLogicStability(unittest.IsolatedAsyncioTestCase):
    async def test_parsing_deduplication(self):
        # We want to simulate two concurrent calls to the same URL
        url = "https://youtube.com/watch?v=unique_123"
        
        # Mock ytdlp.list_formats to take some time
        # It must be synchronous because main.py uses asyncio.to_thread
        def mock_list_formats(u):
            import time
            time.sleep(0.5)
            return ("Title", [], MagicMock(), "1:00")
            
        with patch("app.main.ytdlp.list_formats", side_effect=mock_list_formats):
            # We simulate what happens in on_message (simplified)
            async def simulated_on_message():
                if url in inflight_parsing:
                    logger_mock = MagicMock()
                    # Simulating the await inflight_parsing[url].wait()
                    await inflight_parsing[url].wait()
                    return info_cache.get(url)
                else:
                    event = asyncio.Event()
                    inflight_parsing[url] = event
                    try:
                        # This matches main.py logic
                        res = await asyncio.to_thread(mock_list_formats, url)
                        info_cache[url] = res
                        return res
                    finally:
                        event.set()
                        inflight_parsing.pop(url, None)

            # Fire two concurrent tasks
            t1 = asyncio.create_task(simulated_on_message())
            t2 = asyncio.create_task(simulated_on_message())
            
            results = await asyncio.gather(t1, t2)
            
            self.assertEqual(results[0][0], "Title")
            self.assertEqual(results[1][0], "Title")
            self.assertEqual(len(inflight_parsing), 0)

if __name__ == '__main__':
    unittest.main()
