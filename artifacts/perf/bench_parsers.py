"""
Benchmark harness for app/services/ytdlp/parsers.py hot paths.

Targets:
  - deduplicate_formats (non-TikTok path, N=50 YouTube-like formats)
  - _is_tiktok / _is_youtube / _is_pinterest / _is_facebook (URL classifiers)
  - classify_tiktok_content (called per-message for TikTok URLs)

Usage:
  python artifacts/perf/bench_parsers.py

Output:
  JSON report to artifacts/perf/<run_label>.json
  Summary table to stdout
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from statistics import mean, median, quantiles

# Ensure repo root is importable
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.services.ytdlp.parsers import (
    deduplicate_formats,
    classify_tiktok_content,
    _is_tiktok,
    _is_youtube,
    _is_pinterest,
    _is_facebook,
    FormatMetadata,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_youtube_formats(n: int = 50) -> list[FormatMetadata]:
    """Simulate N YouTube formats: mix of heights, codecs, muxed vs. dash."""
    heights = [2160, 1440, 1080, 720, 480, 360, 240, 144]
    codecs = [
        ("avc1.42001E", "mp4a.40.2"),
        ("vp9", "opus"),
        ("av01.0.05M.08", "opus"),
        ("avc1.64001F", "mp4a.40.2"),
        ("vp9.2", "none"),
        ("avc1", "none"),
    ]
    formats = []
    for i in range(n):
        vc, ac = codecs[i % len(codecs)]
        formats.append(
            FormatMetadata(
                format_id=f"fmt{i:03d}",
                ext="mp4",
                height=heights[i % len(heights)],
                filesize=10_000_000 + i * 100_000,
                protocol="https",
                vcodec=vc,
                acodec=ac,
            )
        )
    return formats


YOUTUBE_FORMATS_50 = _make_youtube_formats(50)

SAMPLE_URLS = [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://www.tiktok.com/@user/video/12345678",
    "https://www.pinterest.com/pin/123456/",
    "https://pin.it/abc123",
    "https://www.facebook.com/watch/?v=12345678",
    "https://fb.watch/abc123",
    "https://vk.com/video-123456_789",
    "https://www.instagram.com/reel/abc123",
]

TIKTOK_URLS = [
    "https://www.tiktok.com/@user/video/12345678",
    "https://www.tiktok.com/@user/photo/99887766",
    "https://vm.tiktok.com/shortlink",
]

# ── Timer ─────────────────────────────────────────────────────────────────────

REPEAT = 4
INNER = 10_000  # iterations per repeat


def _bench(fn, *args, inner: int = INNER, repeat: int = REPEAT) -> dict:
    """Return {samples: list[float], p50, p95, p99} in µs/iteration."""
    samples_us: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        for _ in range(inner):
            fn(*args)
        elapsed = time.perf_counter() - t0
        samples_us.append(elapsed / inner * 1_000_000)

    qs = quantiles(samples_us, n=100)
    return {
        "samples_us": samples_us,
        "mean_us": mean(samples_us),
        "p50_us": median(samples_us),
        "p95_us": qs[94],
        "p99_us": qs[98],
        "repeat": repeat,
        "inner": inner,
    }


# ── Benchmark targets ─────────────────────────────────────────────────────────

def bench_deduplicate_non_tiktok():
    return _bench(deduplicate_formats, YOUTUBE_FORMATS_50, False)


def bench_classify_tiktok_content():
    # Mix of video and photo URLs
    urls = TIKTOK_URLS * 4
    def run():
        for u in urls:
            classify_tiktok_content(u)
    return _bench(run, inner=1000, repeat=REPEAT)


def bench_url_classifiers():
    """All four _is_* functions across 9 URLs."""
    def run():
        for u in SAMPLE_URLS:
            _is_tiktok(u)
            _is_youtube(u)
            _is_pinterest(u)
            _is_facebook(u)
    return _bench(run, inner=5000, repeat=REPEAT)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_all(label: str) -> dict:
    import subprocess
    git_sha = "unknown"
    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT), text=True
        ).strip()
    except Exception:
        pass

    results = {
        "label": label,
        "git_sha": git_sha,
        "target": "app/services/ytdlp/parsers.py",
        "environment": f"Python {sys.version}",
        "repeat": REPEAT,
        "benches": {
            "deduplicate_non_tiktok_50formats": bench_deduplicate_non_tiktok(),
            "classify_tiktok_content_12calls": bench_classify_tiktok_content(),
            "url_classifiers_9urls_x4": bench_url_classifiers(),
        },
    }

    out_dir = Path(__file__).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{label}.json"
    out_path.write_text(json.dumps(results, indent=2))

    # Stdout summary
    print(f"\n{'='*60}")
    print(f"  Perf report: {label}  (sha={git_sha})")
    print(f"{'='*60}")
    for name, r in results["benches"].items():
        print(
            f"  {name}\n"
            f"    p50={r['p50_us']:.3f}µs  p95={r['p95_us']:.3f}µs  "
            f"p99={r['p99_us']:.3f}µs  mean={r['mean_us']:.3f}µs\n"
            f"    samples={[f'{s:.3f}' for s in r['samples_us']]}"
        )
        print()
    print(f"  Report written: {out_path}\n")

    return results


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    run_all(label)
