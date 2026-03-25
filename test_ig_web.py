import asyncio
from curl_cffi.requests import AsyncSession

async def check():
    url = "https://www.instagram.com/reel/DWNvapCiCLW/?__a=1&__d=dis"
    async with AsyncSession(impersonate="chrome110") as session:
        resp = await session.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
                "X-IG-App-ID": "936619743392459",
            }
        )
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items", [])
            if items:
                print(f"Items found! {items[0].get('video_versions')}")
            else:
                print(f"No items? Keys: {list(data.keys())}")
        else:
            try:
                print(resp.json())
            except Exception:
                print(resp.text[:500])
                    
if __name__ == "__main__":
    asyncio.run(check())
