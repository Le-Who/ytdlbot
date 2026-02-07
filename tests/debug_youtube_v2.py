
import asyncio
import sys
import os
import json
import subprocess

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.ytdlp.service import YtDlpService

async def debug_youtube_video(url):
    service = YtDlpService()
    print(f"DEBUG: Extracting formats for {url}")
    
    # 1. Test via main extract (YoutubeDL API)
    print("\n--- Testing via YoutubeDL API ---")
    try:
        opts = service._base_opts(for_list_formats=True)
        print(f"Opts: {opts}")
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            print(f"SUCCESS (API): Found {len(info.get('formats', []))} formats")
    except Exception as e:
        print(f"FAILURE (API): {e}")

    # 2. Test via subprocess fallback
    print("\n--- Testing via Subprocess Fallback ---")
    try:
        info = service._extract_youtube_via_subprocess(url)
        if info:
            print(f"SUCCESS (Subprocess): Found {len(info.get('formats', []))} formats")
        else:
            print("FAILURE (Subprocess): Returned None")
    except Exception as e:
        print(f"FAILURE (Subprocess): {e}")

if __name__ == "__main__":
    url = "https://www.youtube.com/watch?v=UNo0TG9LwwI"
    if len(sys.argv) > 1:
        url = sys.argv[1]
    asyncio.run(debug_youtube_video(url))
