from unittest.mock import AsyncMock

"""
conftest.py — runs before any test module is collected.
Mocks FastAPI if not genuinely installed so tests that import app.api.routes
or app.main can collect without ImportError.

Also provides shared fixtures to DRY up test setup boilerplate.
"""

import os
import sys
import pytest
from unittest.mock import MagicMock

# Ensure project root is on sys.path (centralised — no need in individual test files)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from telegram import Message


class MockHTTPException(Exception):
    """Real Exception subclass standing in for fastapi.HTTPException."""

    def __init__(self, status_code: int = 500, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _is_real_fastapi() -> bool:
    try:
        import fastapi

        return hasattr(fastapi, "__file__") and fastapi.__file__ is not None
    except ImportError:
        return False


if not _is_real_fastapi():
    _mock = MagicMock()
    _mock.HTTPException = MockHTTPException
    _mock.Request = MagicMock

    _mock_router = MagicMock()
    _mock.APIRouter.return_value = _mock_router
    _mock.FastAPI = MagicMock

    sys.modules["fastapi"] = _mock
    sys.modules["fastapi.responses"] = MagicMock()
    sys.modules["fastapi.testclient"] = MagicMock()

# Ensure default env vars
os.environ.setdefault("BOT_TOKEN", "test_token")
os.environ.setdefault("WEBHOOK_URL", "https://example.com")
os.environ.setdefault("TELEGRAM_SECRET_TOKEN", "test-secret")


# ── Shared Fixtures ──────────────────────────────────────────────


@pytest.fixture()
def mock_state():
    """Pre-configure app.core.state with common test mocks."""
    from app.core import state
    from app.core.storage.memory import MemoryStorage

    # We use MemoryStorage implicitly typed as AsyncMockCache
    state.info_cache = MemoryStorage()
    state.link_cache = MemoryStorage()
    state.cancel_cache = MemoryStorage()
    state.limiter = MagicMock()
    state.limiter.allow_user = AsyncMock(return_value=True)
    state.limiter.allow_chat = AsyncMock(return_value=True)
    state.limiter.allow_ip = AsyncMock(return_value=True)
    state.limiter.allow_token = AsyncMock(return_value=True)
    state.parsing_sem = MagicMock()
    state.parsing_sem.__aenter__ = AsyncMock(return_value=None)
    state.parsing_sem.__aexit__ = AsyncMock(return_value=None)
    state.tasks_sem = MagicMock()
    state.tasks_sem.locked.return_value = False
    state.tasks_sem.__aenter__ = AsyncMock(return_value=None)
    state.tasks_sem.__aexit__ = AsyncMock(return_value=None)
    state.inflight_parsing = {}
    state.ytdlp = MagicMock()
    state.bot_app = MagicMock()
    state.bot_app.bot = MagicMock()
    state.bot_app.process_update = AsyncMock()
    return state


@pytest.fixture()
def mock_update():
    """Create a MagicMock telegram Update with pre-wired async methods."""
    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_user.username = "testuser"
    update.effective_chat.id = 99999
    update.effective_chat.type = "private"
    update.message.text = ""
    update.message.reply_text = AsyncMock()
    update.message.delete = AsyncMock()
    update.callback_query.data = ""
    update.callback_query.from_user.id = 12345
    update.callback_query.message = MagicMock(spec=Message)
    update.callback_query.message.chat_id = 99999
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.callback_query.delete_message = AsyncMock()
    return update


@pytest.fixture()
def mock_context():
    """Create a MagicMock telegram context with pre-wired user_data."""
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context
