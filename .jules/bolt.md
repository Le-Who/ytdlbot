## 2026-01-31 - [Subprocess Deadlock with Pipes]
**Learning:** When using `asyncio.subprocess` with `stderr=PIPE`, you MUST consume the stderr stream concurrently while reading stdout or waiting for the process. Failure to do so can cause the subprocess to block (deadlock) if the stderr buffer fills up (typically 64KB).
**Action:** Always drain stderr asynchronously using `asyncio.create_task` when streaming stdout, or redirect it to `DEVNULL` if logs are not needed.

## 2026-01-31 - [Mocking Subprocesses for Performance Testing]
**Learning:** To verify streaming optimizations without heavy dependencies (like ffmpeg), mocking `asyncio.create_subprocess_exec` is effective. However, the mock script must accurately simulate the behavior of the real tool (e.g., handling stdout vs file output) to avoid false positives/negatives in tests.
**Action:** When mocking CLI tools, ensure the mock script parses arguments to mimic the output destination (stdout or file) correctly.

## 2026-01-31 - [Blocking File I/O in Async Loop]
**Learning:** Synchronous file system operations (specifically `os.unlink`, `os.rename`, and `os.path.getsize`) are blocking calls that halt the asyncio event loop. In high-load async applications, this causes latency and can starve other concurrent tasks.
**Action:** Offload these blocking file operations to a separate thread using `asyncio.to_thread`, preventing them from blocking the main event loop.
## 2026-01-31 - [Regex Compilation Overhead]
**Learning:** Compiling regex patterns inside frequently called handlers (like loops or API endpoints) introduces unnecessary overhead. Although Python caches compiled regexes, calling `re.sub` repeatedly still incurs dictionary lookup and potential cache eviction costs.
**Action:** Move constant regex patterns to the module level (global scope) using `re.compile()` to initialize them once at startup, achieving ~20% performance gain on those operations.
## 2026-02-01 - [Pre-compiling Regex in Hot Loops]
**Learning:** Using `re.search` inside a frequently called method causes repeated cache lookups or recompilation of the regex pattern. Pre-compiling the regex into a module-level constant avoids this overhead and speeds up execution significantly (measured ~40% improvement in micro-benchmarks).
**Action:** Identify regex patterns used in hot paths and compile them once using `re.compile()` at module or class level.
## 2026-01-31 - [Async Stderr Consumption for Reliability]
**Learning:** Blocking reads on subprocess stderr can cause deadlocks if the buffer fills up while the main loop reads stdout. This is critical when streaming output from tools like yt-dlp.
**Action:** Implemented asynchronous stderr consumption in `stream_video_subprocess` to ensure continuous stream processing and prevent deadlocks.

## 2026-02-01 - [Deque for Stream Buffering]
**Learning:** Using `list.pop(0)` to maintain a fixed-size buffer (e.g., for stderr logs) is O(N) because it shifts all elements. In high-throughput streaming loops, this adds unnecessary CPU overhead.
**Action:** Use `collections.deque(maxlen=N)` which provides O(1) appends and automatic eviction of old elements, simplifying code and improving performance.

## 2026-02-01 - [Optimizing Frequent String Checks]
**Learning:** Checking for suffixes using a loop with string concatenation (`domain.endswith(f".{p}")`) inside a hot path creates unnecessary string objects and is slow (O(N)). Using `str.endswith()` with a pre-computed tuple of suffixes pushes the iteration to C level, resulting in drastic performance improvements (~85% measured).
**Action:** When validating against a set of static prefixes or suffixes, pre-compute them into a tuple and use `startswith/endswith`.

## 2026-02-05 - [Caching Expensive yt-dlp Initialization]
**Learning:** Initializing `yt_dlp.YoutubeDL` is expensive (~100ms) due to loading many extractors. For frequent metadata requests, creating a new instance every time adds significant latency.
**Action:** Use `threading.local` to cache `YoutubeDL` instances per thread when options are stable, reducing overhead by ~95% for sequential requests.

## 2026-02-06 - [Redundant Validation in Hot Paths]
**Learning:** When validating input in a helper function (like `is_supported_url`), avoid re-validating preconditions (like "is it a URL?") that the caller has already enforced. Redundant regex checks in hot paths multiply overhead.
**Action:** Trust the caller for structural validation or pass parsed objects directly to the validator.

## 2026-02-06 - [HTML Injection in Telegram Messages]
**Learning:** When using `parse_mode='HTML'` in Telegram bots, ANY user-controlled content (like video titles) interpolated into the message string MUST be escaped using `html.escape()`. Failing to do so allows injection of tags, breaking the message format.
**Action:** Audit all `parse_mode='HTML'` usages and ensure `html.escape()` is applied to dynamic variables.

## 2026-02-07 - [Reusing YoutubeDL Instances with Thread Local]
**Learning:** Initializing `yt_dlp.YoutubeDL` takes ~120ms. Caching it in `threading.local` for read-only operations (like `list_formats`) eliminates this overhead. However, do NOT use `with instance:` when reusing, as `__exit__` might close resources you intend to keep open.
**Action:** Use `threading.local` to store `YoutubeDL` instances and call `extract_info` directly on the stored instance without a context manager for subsequent calls.

## 2026-02-07 - [Pre-computed String Interpolation]
**Learning:** In hot loops like progress bar rendering (called every ~3s per download), constructing strings with f-strings and multiplication (`"█" * N`) creates short-lived objects that pressure GC. For finite states (e.g., 0-15 blocks), a lookup table is ~14% faster and eliminates allocation overhead.
**Action:** Identify finite-state string generations in loops and replace them with pre-computed lookup tables or constants.

## 2026-02-07 - [Offloading Blocking File I/O]
**Learning:** Opening and closing files synchronously (`with open(...)`) in an `async` context blocks the event loop, causing latency for all concurrent tasks. Offloading these operations to a thread using `asyncio.to_thread` preserves event loop responsiveness even during disk I/O.
**Action:** Replace blocking `open()` and `f.close()` calls in async handlers with `asyncio.to_thread`.
