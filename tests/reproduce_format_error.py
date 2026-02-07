
import yt_dlp
import sys

def debug_extraction():
    url = "https://www.youtube.com/watch?v=UNo0TG9LwwI"
    
    # 1. Simulate Current Service Options (WITH format selector)
    opts_current = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "force_ipv4": True,
        "prefer_free_formats": False,
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android", "web", "mweb"],
            }
        },
        "geo_bypass": True,
        "ignoreconfig": True,
        # This is the problematic line causing the error potentially
        "format": "best/bestvideo+bestaudio", 
    }
    
    print("\n--- Test 1: Current Options (WITH format selector) ---")
    try:
        with yt_dlp.YoutubeDL(opts_current) as ydl:
            info = ydl.extract_info(url, download=False)
            print(f"SUCCESS: Found {len(info.get('formats', []))} formats")
    except Exception as e:
        print(f"FAILURE: {e}")

    # 2. Simulate Proposed Fix (WITHOUT format selector)
    opts_fix = opts_current.copy()
    del opts_fix["format"]
    
    print("\n--- Test 2: Proposed Fix (WITHOUT format selector) ---")
    try:
        with yt_dlp.YoutubeDL(opts_fix) as ydl:
            info = ydl.extract_info(url, download=False)
            print(f"SUCCESS: Found {len(info.get('formats', []))} formats")
    except Exception as e:
        print(f"FAILURE: {e}")

if __name__ == "__main__":
    debug_extraction()
