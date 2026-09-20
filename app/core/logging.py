import contextvars
import json
import logging
import os
import uuid
from datetime import datetime, timezone

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = (
            datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        correlation_id = correlation_id_var.get()
        event = getattr(record, "event", None) or getattr(record, "op", None) or "log"
        payload = {
            "schema_version": 1,
            "timestamp": timestamp,
            "ts": timestamp,
            "service": os.getenv("LOG_SERVICE", "ytdlbot").strip() or "ytdlbot",
            "environment": os.getenv("LOG_ENVIRONMENT", "development").strip()
            or "development",
            "release": os.getenv("APP_RELEASE", "dev").strip() or "dev",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "msg": record.getMessage(),
            "event": event,
            "event_id": uuid.uuid4().hex,
            "request_id": correlation_id,
            "correlation_id": correlation_id,
        }
        for key in (
            "op",
            "duration_ms",
            "error_type",
            "token",
            "chat_id",
            "user_id",
            "url_host",
            "stderr",
            "returncode",
            "error",
            "path",
            "metrics",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def set_correlation_id(value: str | None = None) -> str:
    corr = value or uuid.uuid4().hex
    correlation_id_var.set(corr)
    return corr
