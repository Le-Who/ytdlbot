import requests
import yt_dlp

url = 'https://www.tiktok.com/@hamster_planet4u/video/7614546152075103502'
res = requests.get(f'https://www.tikwm.com/api/?url={url}&hd=1').json().get('data', {})

opts = {'dumpjson': True, 'quiet': True}
with yt_dlp.YoutubeDL(opts) as ydl:
    for key in ['hdplay', 'play', 'wmplay']:
        v_url = res.get(key)
        if not v_url: continue
        try:
            info = ydl.extract_info(v_url, download=False)
            codec = info.get('vcodec')
            print(f'{key} codec: {codec}')
        except Exception as e:
            print(f'{key} error: {e}')
