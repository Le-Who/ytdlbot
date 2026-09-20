from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.core import state


@dataclass
class HealthyStore:
    version: int = 3

    async def schema_version(self) -> int:
        return self.version

    async def journal_mode(self) -> str:
        return "wal"

    async def busy_timeout(self) -> int:
        return 5_000

    async def write_probe(self) -> None:
        return None


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes.config, "APP_RELEASE", "sha-test")
    monkeypatch.setattr(routes.config, "MAX_MEDIA_FILE_MB", 2_000)
    monkeypatch.setattr(routes.config, "TELEGRAM_CLOUD_MAX_FILE_MB", 50)
    monkeypatch.setattr(
        routes.config, "TELEGRAM_LOCAL_ENDPOINT", "http://tg-api:8081"
    )
    monkeypatch.setattr(routes.config, "TELEGRAM_LOCAL_REQUIRED", True)
    bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=42)))
    monkeypatch.setattr(state, "bot_app", SimpleNamespace(bot=bot))
    routes.configure_job_store(HealthyStore())  # type: ignore[arg-type]
    with TestClient(app) as test_client:
        yield test_client
    routes.configure_job_store(None)


def test_live_is_independent_of_runtime_dependencies(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_ready_reports_release_limit_store_and_required_local_api(
    client: TestClient,
) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "ready": True,
        "release": "sha-test",
        "max_media_file_mb": 2_000,
        "delivery_profile": {
            "name": "local_bot_api",
            "upload_limit_mb": 2_000,
        },
        "durable_store": {
            "ready": True,
            "schema_version": 3,
            "journal_mode": "wal",
            "busy_timeout_ms": 5_000,
            "writable": True,
        },
        "local_bot_api": {
            "required": True,
            "configured": True,
            "functional_probe": True,
        },
    }


def test_ready_fails_when_durable_store_is_unavailable(client: TestClient) -> None:
    routes.configure_job_store(None)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["durable_store"] == {"ready": False}


def test_ready_rejects_non_current_durable_schema(client: TestClient) -> None:
    routes.configure_job_store(HealthyStore(version=2))  # type: ignore[arg-type]

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["durable_store"]["schema_version"] == 2


def test_ready_requires_current_durable_store_write_capability(
    client: TestClient,
) -> None:
    class ReadOnlyStore(HealthyStore):
        async def write_probe(self) -> None:
            raise OSError("read-only")

    routes.configure_job_store(ReadOnlyStore())  # type: ignore[arg-type]

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["durable_store"] == {"ready": False}
    assert "read-only" not in response.text


def test_ready_bounds_the_complete_durable_store_probe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowStore(HealthyStore):
        async def write_probe(self) -> None:
            await asyncio.sleep(0.05)

    routes.configure_job_store(SlowStore())  # type: ignore[arg-type]
    monkeypatch.setattr(routes, "_DURABLE_STORE_PROBE_TIMEOUT_SECONDS", 0.001)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["durable_store"] == {"ready": False}


def test_ready_fails_when_required_local_api_probe_did_not_complete(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state, "bot_app", None)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["local_bot_api"] == {
        "required": True,
        "configured": True,
        "functional_probe": False,
    }


def test_optional_provider_outage_does_not_fail_readiness(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    unavailable_optional_provider = type(
        "Pipeline", (), {"optional_providers_available": False}
    )()
    monkeypatch.setattr(state, "media_pipeline", unavailable_optional_provider)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_ready_never_exposes_local_api_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "super-secret-bot-token"
    monkeypatch.setattr(routes.config, "BOT_TOKEN", secret)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert secret not in response.text


def test_ready_fails_when_live_authenticated_local_api_probe_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = SimpleNamespace(get_me=AsyncMock(side_effect=OSError("local api down")))
    monkeypatch.setattr(state, "bot_app", SimpleNamespace(bot=bot))

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["local_bot_api"]["functional_probe"] is False
    assert "local api down" not in response.text


def test_ready_bounds_live_authenticated_local_api_probe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow_get_me() -> object:
        await asyncio.sleep(0.05)
        return object()

    monkeypatch.setattr(routes, "_LOCAL_API_PROBE_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(
        state,
        "bot_app",
        SimpleNamespace(bot=SimpleNamespace(get_me=slow_get_me)),
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["local_bot_api"]["functional_probe"] is False
