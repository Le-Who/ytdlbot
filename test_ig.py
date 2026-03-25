import asyncio
import logging
import base64
import pickle
from curl_cffi.requests import AsyncSession

# from app.core.config import IG_SESSION_B64
import os
import sys

# Need to load the bot's environment variables to get IG_SESSION_B64
from dotenv import load_dotenv
load_dotenv(dotenv_path='d:/ytdlbot-1/.env')

IG_SESSION_B64 = os.environ.get("IG_SESSION_B64")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test")

def get_cookies():
    if not IG_SESSION_B64:
        return None
    data = base64.b64decode(IG_SESSION_B64)
    cookies = pickle.loads(data)
    result_cookies = {}
    if isinstance(cookies, dict):
        for k, v in cookies.items():
            if isinstance(v, str) and v:
                result_cookies[k] = v
    else:
        for cookie in cookies:
            if hasattr(cookie, "name") and hasattr(cookie, "value") and cookie.value:
                result_cookies[cookie.name] = cookie.value
    return result_cookies

async def test_endpoints(username: str):
    cookies = get_cookies()
    if not cookies:
        print("No cookies found!")
        return

    APP_ID = "936619743392459"
    IMPERSONATE = "chrome110"
    
    async with AsyncSession(impersonate=IMPERSONATE, cookies=cookies) as session:
        # Test 1: web_profile_info authenticated
        print("\\n=== TEST 1: web_profile_info authenticated ===")
        doc1 = await session.get(
            f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
            headers={
                "X-IG-App-ID": APP_ID,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        print(f"Status: {doc1.status_code}")
        if doc1.status_code == 200:
            data = doc1.json()
            user_data = data.get("data", {}).get("user", {})
            print(f"User ID: {user_data.get('id')}")
        else:
            print(doc1.text[:200])

        # Test 2: mobile api usernameinfo
        print("\\n=== TEST 2: mobile usernameinfo ===")
        doc2 = await session.get(
            f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}",
            headers={
                "User-Agent": "Instagram 219.0.0.12.117 Android",
            },
        )
        print(f"Status: {doc2.status_code}")
        try:
            print(f"Response: {str(doc2.json())[:200]}")
        except:
            print(doc2.text[:200])

        # Test 3: mobile api usernameinfo (correct endpoint)
        print("\\n=== TEST 3: mobile api info by username ===")
        doc3 = await session.get(
            f"https://i.instagram.com/api/v1/users/{username}/usernameinfo/",
            headers={
                "User-Agent": "Instagram 219.0.0.12.117 Android",
            },
        )
        print(f"Status: {doc3.status_code}")
        try:
            print(f"Response: {str(doc3.json())[:200]}")
        except:
            print(doc3.text[:200])


if __name__ == "__main__":
    asyncio.run(test_endpoints("max_barskih"))
