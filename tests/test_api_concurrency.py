import sys
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

# 1. Setup mocks before importing app modules that use them
mock_state = MagicMock()
mock_state.link_cache = {}
mock_state.tasks_sem = MagicMock()
mock_state.tasks_sem.__aenter__ = AsyncMock()
mock_state.tasks_sem.__aexit__ = AsyncMock()
# Mock ytdlp
mock_ytdlp = MagicMock()
mock_ytdlp.build_command.return_value = ["echo", "test"]
mock_state.ytdlp = mock_ytdlp

# Inject mocks
sys.modules["app.core.state"] = mock_state

# 2. Import the router
from app.api.routes import router

app = FastAPI()
app.include_router(router)
client = TestClient(app)

@pytest.mark.asyncio
async def test_download_concurrency_limit():
    """
    Verifies that the download endpoint acquires the tasks_sem semaphore
    to limit concurrent downloads.
    """
    token = "concurrent_test_token"
    mock_state.link_cache[token] = {
        "page_url": "http://example.com/video",
        "format_id": "18",
        "title": "Concurrent Test",
        "height": 720
    }

    # Mock run_subprocess to simulate a download stream
    async def mock_run_subprocess(*args, **kwargs):
        proc = AsyncMock()
        proc.stdout.read = AsyncMock(side_effect=[b"data", b""])
        proc.wait = AsyncMock()
        proc.returncode = 0
        yield proc, []

    with patch("app.api.routes.run_subprocess", side_effect=mock_run_subprocess):
        response = client.get(f"/dl/{token}")
        assert response.status_code == 200

        # Iterate over the streaming response to execute the generator
        content = b"".join(response.iter_bytes())
        assert content == b"data"

        # ASSERTION: The semaphore must be acquired
        if mock_state.tasks_sem.__aenter__.call_count == 0:
            pytest.fail("Security Vulnerability: tasks_sem was NOT acquired for download stream!")
