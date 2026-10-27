"""Exercise actual libcurl routing with isolated child environments and local peers."""

from __future__ import annotations

import json
import os
import socketserver
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@contextmanager
def http_peer(marker: str) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = marker.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=2)


@contextmanager
def socks_peer() -> Iterator[str]:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(5)
            version, methods = self.rfile.read(2)
            assert version == 5
            self.rfile.read(methods)
            self.wfile.write(b"\x05\x00")
            self.wfile.flush()
            version, command, _, address_type = self.rfile.read(4)
            assert version == 5 and command == 1
            if address_type == 1:
                self.rfile.read(4)
            elif address_type == 4:
                self.rfile.read(16)
            else:
                self.rfile.read(self.rfile.read(1)[0])
            self.rfile.read(2)
            self.wfile.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            self.wfile.flush()
            while self.rfile.readline() not in {b"\r\n", b""}:
                pass
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 14\r\n"
                b"Connection: close\r\n\r\nselected-socks"
            )

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"socks5://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            thread.join(timeout=2)


_CHILD = """
import asyncio
import ipaddress
import json
import sys
from app.services.media.providers.http import CurlProviderTransport
from app.services.media.transport import CurlStreamingClient

async def run():
    mode, url, proxy = sys.argv[1:]
    if mode == 'native':
        response = await CurlProviderTransport().request('GET', url, timeout=3)
        body = response.text
    else:
        response = await CurlStreamingClient(proxy or None).open(
            'GET', url, headers={}, resolved_ip=ipaddress.ip_address('127.0.0.1'), timeout=3
        )
        try:
            body = b''.join([chunk async for chunk in response.iter_bytes(1024)]).decode()
        finally:
            await response.close()
    print(json.dumps({'status': response.status_code, 'body': body}))

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
asyncio.run(run())
"""


def child_request(
    mode: str, origin: str, ambient_proxy: str, *, selected_proxy="", no_proxy=""
):
    env = os.environ.copy()
    for key in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        env[key] = ambient_proxy
    for key in ("no_proxy", "NO_PROXY"):
        env[key] = no_proxy
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, mode, f"{origin}/media", selected_proxy],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("mode", ["native", "stream"])
def test_direct_curl_route_ignores_environment_proxy(mode):
    with http_peer("direct-origin") as origin, http_peer("ambient-proxy") as proxy:
        result = child_request(mode, origin, proxy)

    assert result == {"status": 200, "body": "direct-origin"}


def test_configured_socks_route_ignores_environment_no_proxy():
    with http_peer("bypassed-socks") as origin, socks_peer() as selected_proxy:
        result = child_request(
            "stream", origin, origin, selected_proxy=selected_proxy, no_proxy="*"
        )

    assert result == {"status": 200, "body": "selected-socks"}
