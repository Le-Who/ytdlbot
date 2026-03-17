import asyncio
import os
import time
from app.services.tikwm import TikWMService
from app.core.config import TEMP_DIR
import uuid

async def benchmark():
    # Use dummy audio download to measure IO blocking as it writes to a file
    # We'll use a local file test if possible or just run a bunch of async io to thread writes

    data = b"0" * (10 * 1024 * 1024) # 10MB dummy data

    def sync_write():
        output_path = os.path.join(TEMP_DIR, f"test_{uuid.uuid4().hex}.bin")
        with open(output_path, "wb") as f:
            f.write(data)
        os.unlink(output_path)

    async def async_write():
        output_path = os.path.join(TEMP_DIR, f"test_{uuid.uuid4().hex}.bin")
        def _write():
            with open(output_path, "wb") as f:
                f.write(data)
        await asyncio.to_thread(_write)
        os.unlink(output_path)

    # Measure sync writes blocking the event loop
    start = time.time()
    for _ in range(50):
        sync_write()
    sync_time = time.time() - start
    print(f"Sync writes (event loop blocked): {sync_time:.4f}s")

    # Measure async writes
    start = time.time()
    tasks = [async_write() for _ in range(50)]
    await asyncio.gather(*tasks)
    async_time = time.time() - start
    print(f"Async writes (concurrent): {async_time:.4f}s")

if __name__ == "__main__":
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)
    asyncio.run(benchmark())
