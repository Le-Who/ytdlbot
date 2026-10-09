import asyncio
import os
import sys

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.ytdlp.service import YtDlpService


async def debug_youtube():
    service = YtDlpService()
    url = "https://www.youtube.com/watch?v=NjW8iy5OP2g"
    print(f"DEBUG: Extracting formats for {url}")
    try:
        result = await service.list_formats(url)
        print(f"SUCCESS: Found {len(result.formats)} formats for '{result.title}'")
    # This CLI reports arbitrary provider/plugin failures; cancellation still propagates.
    except Exception as e:  # noqa: BLE001
        print(f"FAILURE: {e}")


if __name__ == "__main__":
    asyncio.run(debug_youtube())
