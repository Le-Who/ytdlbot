
import asyncio
import sys
import os
import json
import subprocess

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.ytdlp_service import YtDlpService

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
    base_cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-download",
        "--no-playlist",
    ]
    # We'll try the exact variant from the service
    variant = ["--extractor-args", "youtube:player_client=android,web,mweb,ios"]
    cmd = base_cmd + variant + [url]
    print(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            info = json.loads(result.stdout)
            print(f"SUCCESS (Subprocess): Found {len(info.get('formats', []))} formats")
        else:
            print(f"FAILURE (Subprocess): Return code {result.returncode}")
            print(f"STDERR: {result.stderr}")
    except Exception as e:
        print(f"FAILURE (Subprocess): {e}")

if __name__ == "__main__":
    url = "https://www.youtube.com/watch?v=UNo0TG9LwwI"
    asyncio.run(debug_youtube_video(url))
