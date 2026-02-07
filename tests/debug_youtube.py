
import asyncio
import sys
import os
from unittest.mock import MagicMock

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.ytdlp_service import YtDlpService

async def debug_youtube():
    service = YtDlpService()
    url = "https://www.youtube.com/watch?v=NjW8iy5OP2g"
    print(f"DEBUG: Extracting formats for {url}")
    try:
        title, formats, audio, duration = service.list_formats(url)
        print(f"SUCCESS: Found {len(formats)} formats for '{title}'")
    except Exception as e:
        print(f"FAILURE: {e}")

if __name__ == "__main__":
    asyncio.run(debug_youtube())
