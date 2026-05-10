"""
Benchmark harness for app/services/instagram.py URL parsing.

Targets:
  - parse_instagram_url
  - is_instagram_url

Usage:
  python artifacts/perf/bench_instagram.py

Output:
  JSON report to artifacts/perf/<run_label>.json
  Summary table to stdout
"""
import json
import os
import sys
import time
from pathlib import Path
from statistics import mean, median, quantiles

# Ensure repo root is importable
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.services.instagram import parse_instagram_url, is_instagram_url

# ── Fixtures ──────────────────────────────────────────────────────────────────

NON_IG_URLS = [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://www.tiktok.com/@user/video/12345678",
    "https://www.pinterest.com/pin/123456/",
    "https://pin.it/abc123",
    "https://www.facebook.com/watch/?v=12345678",
    "https://fb.watch/abc123",
    "https://vk.com/video-123456_789",
    "https://twitter.com/i/status/1234567890",
]

IG_URLS = [
    "https://www.instagram.com/p/C123456789/",
    "https://www.instagram.com/reel/C123456789/",
    "https://www.instagram.com/stories/username/123456789/",
    "https://www.instagram.com/stories/highlights/123456789/",
    "https://www.instagram.com/username/",
]

ALL_URLS = NON_IG_URLS + IG_URLS

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

def bench_parse_all_urls():
    def run():
        for u in ALL_URLS:
            parse_instagram_url(u)
    return _bench(run)


def bench_is_instagram_url_all():
    def run():
        for u in ALL_URLS:
            is_instagram_url(u)
    return _bench(run)


def bench_is_instagram_url_non_ig():
    def run():
        for u in NON_IG_URLS:
            is_instagram_url(u)
    return _bench(run)


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
        "target": "app/services/instagram.py",
        "environment": f"Python {sys.version}",
        "repeat": REPEAT,
        "benches": {
            "parse_all_urls_14calls": bench_parse_all_urls(),
            "is_instagram_url_14calls": bench_is_instagram_url_all(),
            "is_instagram_url_non_ig_9calls": bench_is_instagram_url_non_ig(),
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
