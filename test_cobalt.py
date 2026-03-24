import asyncio
from app.services.cobalt import CobaltService
import app.services.cobalt as c

# Override URL to use public instance
c.COBALT_API_URLS = ['https://api.cobalt.tools']

async def main():
    url = 'https://www.tiktok.com/@hamster_planet4u/video/7614546152075103502'
    res = await CobaltService.process(url)
    print(f'Status: {res.status}')
    print(f'URL: {res.url}')

if __name__ == '__main__':
    asyncio.run(main())
