import asyncio
import subprocess


async def probe(url) -> bytes | None:
    print(f"Fetching from {url}")
    # Using TikWM directly
    from app.services.tikwm import TikWMService

    res = await TikWMService.process(url)
    print("TikWM process result:", res)
    if res.url:
        print("Downloading...")
        from curl_cffi.requests import AsyncSession

        async with AsyncSession() as session:
            resp = await session.get(res.url, impersonate="chrome")
            return resp.content
    print("No URL found")
    return None


def inspect_download(content: bytes) -> None:
    """Finish this one-shot CLI's file/process work after the async session closes."""
    with open("test_bvc2.mp4", "wb") as f:
        f.write(content)
    print("Downloaded as test_bvc2.mp4")

    # Run ffprobe and report stdout even when its exit status is nonzero.
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "test_bvc2.mp4",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    print("FFprobe outputs:")
    print(proc.stdout)


if __name__ == "__main__":
    content = asyncio.run(
        probe("https://www.tiktok.com/@wagsfamily/video/7630543735620783380")
    )
    if content is not None:
        inspect_download(content)
