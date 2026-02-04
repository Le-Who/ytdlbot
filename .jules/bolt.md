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

## 2026-02-04 - [Optimized Domain Matching]
**Learning:** Using `str.endswith(tuple)` is significantly faster (~20%) and cleaner than iterating through a list and doing string concatenation (`f".{suffix}"`) inside the loop, especially for hot paths like message filtering.
**Action:** Pre-compute suffixes as a tuple for `endswith` checks.
