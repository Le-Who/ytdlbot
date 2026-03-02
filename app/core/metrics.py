"""Lightweight application metrics — zero-dependency counters and gauges.

Provides Prometheus-compatible text format via `render_metrics()`.
"""

import time
import threading
import logging
from collections import defaultdict

logger = logging.getLogger("app.core.metrics")


class _Counter:
    """Thread-safe monotonic counter with labels."""

    def __init__(self, name: str, help_text: str):
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._values: dict[tuple, float] = defaultdict(float)

    def inc(self, value: float = 1.0, **labels) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] += value

    def collect(self) -> list[tuple[dict, float]]:
        with self._lock:
            return [(dict(k), v) for k, v in self._values.items()]


class _Gauge:
    """Thread-safe gauge (can go up and down)."""

    def __init__(self, name: str, help_text: str):
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._values: dict[tuple, float] = defaultdict(float)

    def set(self, value: float, **labels) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] = value

    def inc(self, value: float = 1.0, **labels) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] += value

    def dec(self, value: float = 1.0, **labels) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] -= value

    def collect(self) -> list[tuple[dict, float]]:
        with self._lock:
            return [(dict(k), v) for k, v in self._values.items()]


class MetricsCollector:
    """Central metrics registry with pre-defined application counters."""

    def __init__(self):
        self.start_time = time.time()

        # Counters
        self.downloads_total = _Counter(
            "ytdlbot_downloads_total",
            "Total download attempts",
        )
        self.downloads_success = _Counter(
            "ytdlbot_downloads_success_total",
            "Successful downloads",
        )
        self.downloads_failed = _Counter(
            "ytdlbot_downloads_failed_total",
            "Failed downloads",
        )
        self.cache_hits = _Counter(
            "ytdlbot_cache_hits_total",
            "Cache hit count by cache type",
        )
        self.rate_limit_rejections = _Counter(
            "ytdlbot_rate_limit_rejections_total",
            "Rate limiter rejection count",
        )
        self.parse_requests = _Counter(
            "ytdlbot_parse_requests_total",
            "URL parse requests",
        )
        self.parse_cancellations = _Counter(
            "ytdlbot_parse_cancellations_total",
            "User-initiated parse cancellations",
        )

        # Gauges
        self.active_downloads = _Gauge(
            "ytdlbot_active_downloads",
            "Currently active downloads",
        )

    def render(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        lines = []
        uptime = time.time() - self.start_time

        # Uptime gauge
        lines.append("# HELP ytdlbot_uptime_seconds Bot uptime in seconds")
        lines.append("# TYPE ytdlbot_uptime_seconds gauge")
        lines.append(f"ytdlbot_uptime_seconds {uptime:.1f}")
        lines.append("")

        for metric in [
            self.downloads_total,
            self.downloads_success,
            self.downloads_failed,
            self.cache_hits,
            self.rate_limit_rejections,
            self.parse_requests,
            self.parse_cancellations,
            self.active_downloads,
        ]:
            is_gauge = isinstance(metric, _Gauge)
            metric_type = "gauge" if is_gauge else "counter"
            lines.append(f"# HELP {metric.name} {metric.help_text}")
            lines.append(f"# TYPE {metric.name} {metric_type}")

            entries = metric.collect()
            if not entries:
                lines.append(f"{metric.name} 0")
            else:
                for labels, value in entries:
                    if labels:
                        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
                        lines.append(f"{metric.name}{{{label_str}}} {value}")
                    else:
                        lines.append(f"{metric.name} {value}")
            lines.append("")

        return "\n".join(lines)

    def log_summary(self) -> None:
        """Log a human-readable summary of metrics."""
        summary = {
            "downloads": sum(v for _, v in self.downloads_total.collect()),
            "success": sum(v for _, v in self.downloads_success.collect()),
            "failed": sum(v for _, v in self.downloads_failed.collect()),
            "cache_hits": sum(v for _, v in self.cache_hits.collect()),
            "rate_limited": sum(v for _, v in self.rate_limit_rejections.collect()),
            "active": sum(v for _, v in self.active_downloads.collect()),
            "uptime_h": (time.time() - self.start_time) / 3600,
        }
        logger.info("Metrics summary", extra={"metrics": summary})


# Global singleton
metrics = MetricsCollector()
