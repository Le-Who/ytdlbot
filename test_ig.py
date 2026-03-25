import asyncio
from curl_cffi.requests import AsyncSession
import logging

logging.basicConfig(level=logging.INFO)

_IG_SHORTCODE_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)

def _shortcode_to_media_pk(shortcode: str) -> str:
    media_pk = 0
    for char in shortcode:
        media_pk = media_pk * 64 + _IG_SHORTCODE_ALPHABET.index(char)
    return str(media_pk)

async def check():
    shortcode = "DWNvapCiCLW"
    media_pk = _shortcode_to_media_pk(shortcode)
    print(f"media_pk: {media_pk}")

    # No cookies - just to see what the API responds with initially
    async with AsyncSession(impersonate="chrome110") as session:
        resp = await session.get(
            f"https://i.instagram.com/api/v1/media/{media_pk}/info/",
            headers={
                "User-Agent": "Instagram 219.0.0.12.117 Android",
                "X-IG-App-ID": "936619743392459",
            },
        )
        print(f"Status: {resp.status_code}")
        try:
            print("Response:", resp.json())
        except Exception:
            print("Response text:", resp.text)

if __name__ == "__main__":
    asyncio.run(check())
