import httpx
import re
import sys

url = sys.argv[1]
resp = httpx.get(url, follow_redirects=True)
text = resp.text

for match in re.finditer(r'<meta property="([^"]+)" content="([^"]+)"', text):
    print(match.group(1), match.group(2))
for match in re.finditer(r'<meta name="([^"]+)" content="([^"]+)"', text):
    print(match.group(1), match.group(2))
