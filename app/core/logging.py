import contextvars
import json
import logging
import time
import uuid

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
        }
        for key in ("op", "duration_ms", "error_type", "token", "chat_id", "user_id", "url_host"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
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


class TimedOp:
    def __init__(self, logger: logging.Logger, op: str):
        self.logger = logger
        self.op = op
        self.started = 0.0

    def __enter__(self):
        self.started = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb):
        duration_ms = int((time.monotonic() - self.started) * 1000)
        extra = {"op": self.op, "duration_ms": duration_ms}
        if exc_type:
            extra["error_type"] = exc_type.__name__
            self.logger.error(f"{self.op} failed", extra=extra)
        else:
            self.logger.info(f"{self.op} completed", extra=extra)
        return False
