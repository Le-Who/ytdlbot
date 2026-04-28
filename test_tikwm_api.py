import asyncio
from curl_cffi.requests import AsyncSession

async def main():
    url = "https://www.tiktok.com/@wagsfamily/video/7630543735620783380"
    api_url = f"https://tikwm.com/api/?url={url}&hd=1"
    async with AsyncSession() as session:
        resp = await session.get(api_url, impersonate="chrome")
        data = resp.json()
        print(data.get("data", {}))

if __name__ == "__main__":
    asyncio.run(main())
