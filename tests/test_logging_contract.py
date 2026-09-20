from __future__ import annotations

import json
import logging
from datetime import datetime

from app.core.logging import JsonFormatter, set_correlation_id, setup_logging


def test_json_formatter_emits_shared_loki_contract(monkeypatch) -> None:
    monkeypatch.setenv("LOG_ENVIRONMENT", "production")
    monkeypatch.setenv("LOG_SERVICE", "ytdlbot")
    monkeypatch.setenv("APP_RELEASE", "a" * 40)
    set_correlation_id("request-123")
    record = logging.LogRecord(
        name="app.media",
        level=logging.WARNING,
        pathname=__file__,
        lineno=20,
        msg="provider slowed down",
        args=(),
        exc_info=None,
    )
    record.op = "provider.attempt"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["schema_version"] == 1
    assert payload["service"] == "ytdlbot"
    assert payload["environment"] == "production"
    assert payload["release"] == "a" * 40
    assert payload["level"] == "warning"
    assert payload["message"] == "provider slowed down"
    assert payload["event"] == "provider.attempt"
    assert payload["request_id"] == "request-123"
    assert payload["correlation_id"] == "request-123"
    assert len(payload["event_id"]) == 32
    assert payload["timestamp"].endswith("Z")
    datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))


def test_json_formatter_uses_safe_local_defaults(monkeypatch) -> None:
    monkeypatch.delenv("LOG_ENVIRONMENT", raising=False)
    monkeypatch.delenv("LOG_SERVICE", raising=False)
    monkeypatch.delenv("APP_RELEASE", raising=False)
    set_correlation_id(None)
    record = logging.LogRecord(
        name="app",
        level=logging.INFO,
        pathname=__file__,
        lineno=45,
        msg="ready",
        args=(),
        exc_info=None,
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["service"] == "ytdlbot"
    assert payload["environment"] == "development"
    assert payload["release"] == "dev"
    assert payload["event"] == "log"


def test_setup_logging_routes_uvicorn_records_through_json_formatter() -> None:
    root = logging.getLogger()
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    named = [logging.getLogger(name) for name in names]
    root_state = (list(root.handlers), root.level)
    named_state = [
        (logger, list(logger.handlers), logger.propagate, logger.level)
        for logger in named
    ]
    try:
        for logger in named:
            logger.handlers[:] = [logging.StreamHandler()]
            logger.propagate = False

        setup_logging()

        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        for logger in named:
            assert logger.handlers == []
            assert logger.propagate is True
    finally:
        root.handlers[:] = root_state[0]
        root.setLevel(root_state[1])
        for logger, handlers, propagate, level in named_state:
            logger.handlers[:] = handlers
            logger.propagate = propagate
            logger.setLevel(level)
