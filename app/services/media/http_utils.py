"""Shared HTTP response metadata parsing for resolvers and media streams."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from email.utils import parsedate_to_datetime


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    value = next(
        (value for key, value in headers.items() if key.lower() == "retry-after"), None
    )
    if not value:
        return None
    try:
        delay = float(value)
        return max(0.0, delay) if math.isfinite(delay) else None
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return max(0.0, parsed.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None
