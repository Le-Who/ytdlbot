"""Authentication checks at the actual webhook admission boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.core import state


@pytest.mark.parametrize(
    ("header", "status", "accepted"),
    [
        ("test-secret", 200, [{"update_id": 73}]),
        ("wrong-token-value", 401, []),
        (None, 401, []),
        ("", 401, []),
        ("test-", 401, []),
        ("test-secretextra", 401, []),
    ],
)
def test_webhook_auth_controls_persistence(monkeypatch, header, status, accepted):
    """A weakened auth check must not admit an unauthorized update."""
    persisted = []

    class Store:
        async def accept_update(self, payload):
            persisted.append(payload)

    monkeypatch.setattr(routes, "TELEGRAM_SECRET_TOKEN", "test-secret")
    monkeypatch.setattr(routes, "_job_store", Store())
    monkeypatch.setattr(
        state, "limiter", SimpleNamespace(allow_ip=AsyncMock(return_value=True))
    )
    app = FastAPI()
    app.include_router(routes.router)
    headers = {"X-Telegram-Bot-Api-Secret-Token": header} if header is not None else {}

    with TestClient(app) as client:
        response = client.post("/webhook", json={"update_id": 73}, headers=headers)

    assert response.status_code == status
    assert persisted == accepted
