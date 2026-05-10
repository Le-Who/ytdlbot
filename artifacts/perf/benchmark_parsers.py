import time
import json
import statistics
import sys
from app.services.ytdlp.parsers import parse_format_metadata, deduplicate_formats

def run_benchmark():
    # Simulate a large yt-dlp format list (100 formats)
    formats = []
    for i in range(100):
        formats.append({
            "format_id": f"fmt_{i}",
            "ext": "mp4" if i % 2 == 0 else "webm",
            "vcodec": "avc1.4d401e" if i % 3 == 0 else ("none" if i % 4 == 0 else "vp9"),
            "acodec": "mp4a.40.2" if i % 2 == 0 else "none",
            "height": (1080 if i % 5 == 0 else (720 if i % 3 == 0 else None)),
            "format_note": f"{720 if i % 2 == 0 else 480}p",
            "filesize": 1000000 * (i % 10),
            "protocol": "https"
        })

    repeats = 4
    times = []
    
    for _ in range(repeats):
        start_time = time.perf_counter()
        for _ in range(5000):
            formats_meta = []
            for raw_fmt in formats:
                fmt = parse_format_metadata(raw_fmt, 100.0, False)
                if fmt:
                    formats_meta.append(fmt)
            
            formats_meta.sort(key=lambda x: (x.height or 0, x.filesize or 0), reverse=True)
            formats_meta = deduplicate_formats(formats_meta, False)
            
        end_time = time.perf_counter()
        times.append((end_time - start_time) * 1000)
        
    p95 = statistics.quantiles(times, n=100)[94] if len(times) >= 100 else sorted(times)[int(len(times)*0.95)] if len(times) > 1 else times[0]
    p99 = statistics.quantiles(times, n=100)[98] if len(times) >= 100 else sorted(times)[int(len(times)*0.99)] if len(times) > 1 else times[0]
    
    report_name = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    report = {
        "repeats": repeats,
        "p50": statistics.median(times),
        "p95": p95,
        "p99": p99,
        "raw": times
    }
    
    with open(f"artifacts/perf/{report_name}.json", "w") as f:
        json.dump(report, f, indent=2)
        
    print(f"[{report_name}] p95: {p95:.2f}ms | p99: {p99:.2f}ms")

if __name__ == "__main__":
    run_benchmark()
