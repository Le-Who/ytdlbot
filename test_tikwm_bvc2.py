import asyncio
import json
import subprocess

async def probe(url):
    print(f"Fetching from {url}")
    # Using TikWM directly
    from app.services.tikwm import TikWMService
    res = await TikWMService.process(url)
    print("TikWM process result:", res)
    if res.url:
        print("Downloading...")
        import os
        from curl_cffi.requests import AsyncSession
        async with AsyncSession() as session:
            resp = await session.get(res.url, impersonate="chrome")
            with open("test_bvc2.mp4", "wb") as f:
                f.write(resp.content)
        print("Downloaded as test_bvc2.mp4")
        
        # Run ffprobe
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "test_bvc2.mp4"
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        print("FFprobe outputs:")
        print(proc.stdout)
    else:
        print("No URL found")

if __name__ == "__main__":
    asyncio.run(probe("https://www.tiktok.com/@wagsfamily/video/7630543735620783380"))
