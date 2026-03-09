"""Lightweight application metrics — zero-dependency counters, gauges, and histograms.

Provides Prometheus-compatible text format via `render_metrics()`.
"""

import time
import contextlib
import threading
import logging
from collections import defaultdict
from typing import Generator, Union

logger = logging.getLogger("app.core.metrics")


class _Counter:
    """Thread-safe monotonic counter with labels."""

    def __init__(self, name: str, help_text: str):
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._values: dict[tuple, float] = defaultdict(float)

    def inc(self, value: float = 1.0, **labels: str) -> None:
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

    def set(self, value: float, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] = value

    def inc(self, value: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] += value

    def dec(self, value: float = 1.0, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] -= value

    def collect(self) -> list[tuple[dict, float]]:
        with self._lock:
            return [(dict(k), v) for k, v in self._values.items()]


class _Histogram:
    """Lightweight timer/histogram — tracks count and total sum per label set.

    Usage:
        with metrics.extraction_duration.time(platform="youtube"):
            do_work()
    """

    def __init__(self, name: str, help_text: str):
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._counts: dict[tuple, int] = defaultdict(int)
        self._sums: dict[tuple, float] = defaultdict(float)

    def observe(self, duration: float, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._counts[key] += 1
            self._sums[key] += duration

    @contextlib.contextmanager
    def time(self, **labels: str) -> Generator[None, None, None]:
        """Context manager that measures elapsed time and records it."""
        start = time.monotonic()
        try:
            yield
        finally:
            self.observe(time.monotonic() - start, **labels)

    def collect(self) -> list[tuple[dict, int, float]]:
        """Returns list of (labels, count, sum) tuples."""
        with self._lock:
            keys = set(self._counts) | set(self._sums)
            return [
                (dict(k), self._counts.get(k, 0), self._sums.get(k, 0.0)) for k in keys
            ]


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

        # Histograms (phase timing)
        self.extraction_duration = _Histogram(
            "ytdlbot_extraction_duration_seconds",
            "Time spent extracting video metadata",
        )
        self.download_duration = _Histogram(
            "ytdlbot_download_duration_seconds",
            "Time spent downloading video files",
        )
        self.conversion_duration = _Histogram(
            "ytdlbot_conversion_duration_seconds",
            "Time spent in ffmpeg conversions",
        )
        self.upload_duration = _Histogram(
            "ytdlbot_upload_duration_seconds",
            "Time spent uploading to Telegram",
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

        counter_and_gauge_metrics: list[Union[_Counter, _Gauge]] = [
            self.downloads_total,
            self.downloads_success,
            self.downloads_failed,
            self.cache_hits,
            self.rate_limit_rejections,
            self.parse_requests,
            self.parse_cancellations,
            self.active_downloads,
        ]
        for metric in counter_and_gauge_metrics:
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
                        label_str = ",".join(
                            f'{k}="{v}"' for k, v in sorted(labels.items())
                        )
                        lines.append(f"{metric.name}{{{label_str}}} {value}")
                    else:
                        lines.append(f"{metric.name} {value}")
            lines.append("")

        # Histograms (count + sum)
        for hist in [
            self.extraction_duration,
            self.download_duration,
            self.conversion_duration,
            self.upload_duration,
        ]:
            lines.append(f"# HELP {hist.name} {hist.help_text}")
            lines.append(f"# TYPE {hist.name} summary")
            hist_entries = hist.collect()
            if not hist_entries:
                lines.append(f"{hist.name}_count 0")
                lines.append(f"{hist.name}_sum 0")
            else:
                for labels, count, total in hist_entries:
                    if labels:
                        label_str = ",".join(
                            f'{k}="{v}"' for k, v in sorted(labels.items())
                        )
                        lines.append(f"{hist.name}_count{{{label_str}}} {count}")
                        lines.append(f"{hist.name}_sum{{{label_str}}} {total:.3f}")
                    else:
                        lines.append(f"{hist.name}_count {count}")
                        lines.append(f"{hist.name}_sum {total:.3f}")
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
        # Add average durations per phase
        for hist in [
            self.extraction_duration,
            self.download_duration,
            self.conversion_duration,
            self.upload_duration,
        ]:
            entries = hist.collect()
            total_count = sum(c for _, c, _ in entries)
            total_sum = sum(s for _, _, s in entries)
            if total_count:
                summary[f"{hist.name}_avg_s"] = round(total_sum / total_count, 2)
                summary[f"{hist.name}_count"] = total_count
        logger.info("Metrics summary", extra={"metrics": summary})


# Global singleton
metrics = MetricsCollector()
