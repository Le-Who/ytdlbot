import time
import asyncio
from unittest.mock import AsyncMock
from app.bot.messages import on_message

class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.entities = []

    async def reply_text(self, *args, **kwargs):
        pass

class FakeUpdate:
    def __init__(self, text):
        self.message = FakeMessage(text)
        self.effective_user = AsyncMock()
        self.effective_chat = AsyncMock()
        self.effective_chat.id = 1
        self.effective_user.id = 1
        self.effective_user.username = "test"

async def run_benchmark():
    from app.services.tikwm import TikWMService
    from app.services.cobalt import CobaltService
    from app.services.instagram import InstagramService
    from app.core import state
    # Mock limits to avoid rate limit exits
    state.limiter.allow_user = AsyncMock(return_value=True)
    state.limiter.allow_chat = AsyncMock(return_value=True)
    TikWMService.process = AsyncMock(return_value=AsyncMock(status="error"))
    CobaltService.process = AsyncMock(return_value=AsyncMock(status="error"))
    InstagramService.get_profile_media = AsyncMock(return_value=AsyncMock(error="mocked"))

    # Some typical URLs that pass through the router
    urls = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.tiktok.com/@user/video/1234567890",
        "https://twitter.com/user/status/1234567890",
        "https://instagram.com/p/1234567890",
        "https://reddit.com/r/videos/comments/1234567890",
        "https://example.com/video.mp4",
        "Just some random text without url"
    ]

    context = AsyncMock()
    context.bot = AsyncMock()

    # Pre-warm to ensure all modules are loaded
    for url in urls:
        update = FakeUpdate(url)
        try:
            await asyncio.wait_for(on_message(update, context), timeout=0.1)
        except Exception:
            pass
            
    # Run benchmark repetitions
    import sys
    import json
    import logging
    import statistics
    
    logging.getLogger("app.bot.messages").setLevel(logging.CRITICAL)
    
    repeats = 4
    times = []
    
    for _ in range(repeats):
        start_time = time.perf_counter()
        for _ in range(500):
            for url in urls:
                update = FakeUpdate(url)
                try:
                    await asyncio.wait_for(on_message(update, context), timeout=0.1)
                except Exception:
                    pass
        end_time = time.perf_counter()
        times.append((end_time - start_time) * 1000)
        
    p95 = statistics.quantiles(times, n=100)[94] if len(times) >= 100 else sorted(times)[int(len(times)*0.95)] if len(times) > 1 else times[0]
    p99 = statistics.quantiles(times, n=100)[98] if len(times) >= 100 else sorted(times)[int(len(times)*0.99)] if len(times) > 1 else times[0]
    
    report_name = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    report = {
        "repeats": repeats,
        "p50": statistics.median(times),
        "p95": p95,
        "p99": p99,
        "raw": times
    }
    
    with open(f"artifacts/perf/{report_name}.json", "w") as f:
        json.dump(report, f, indent=2)
        
    print(f"[{report_name}] p95: {p95:.2f}ms | p99: {p99:.2f}ms")

if __name__ == "__main__":
    asyncio.run(run_benchmark())
