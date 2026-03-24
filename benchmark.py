import asyncio
import time
import os
import tempfile
from unittest.mock import AsyncMock

from app.services.sender import TelegramSender


async def run_benchmark():
    # Create temp images
    images = []
    for i in range(10):
        fd, path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(1024 * 1024))  # 1MB fake images
        images.append(path)

    bot = AsyncMock()

    start_time = time.perf_counter()
    for _ in range(100):
        await TelegramSender.send_slideshow_photos(bot, 123, images)
    end_time = time.perf_counter()

    print(f"Elapsed time: {end_time - start_time:.4f} seconds")

    for img in images:
        os.remove(img)


if __name__ == "__main__":
    asyncio.run(run_benchmark())
