import asyncio
from curl_cffi.requests import AsyncSession
import re

async def check():
    url = "https://www.instagram.com/reel/DWNvapCiCLW/"
    async with AsyncSession(impersonate="chrome110") as session:
        resp = await session.get(url)
        # Search for media_id or media id in the HTML
        match = re.search(r'"media_id":"(\d+)"', resp.text)
        if match:
            print(f"HTML media_id: {match.group(1)}")
        else:
            match = re.search(r'content="instagram://media\?id=(\d+)"', resp.text)
            if match:
                print(f"HTML media id from intent: {match.group(1)}")
            else:
                match = re.search(r'media\?id=(\d+)', resp.text)
                if match:
                    print(f"HTML media id from other: {match.group(1)}")
                else:
                    print("Could not find media_id in HTML.")
                    
if __name__ == "__main__":
    asyncio.run(check())
