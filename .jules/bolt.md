## 2026-01-31 - [Subprocess Deadlock with Pipes]
**Learning:** When using `asyncio.subprocess` with `stderr=PIPE`, you MUST consume the stderr stream concurrently while reading stdout or waiting for the process. Failure to do so can cause the subprocess to block (deadlock) if the stderr buffer fills up (typically 64KB).
**Action:** Always drain stderr asynchronously using `asyncio.create_task` when streaming stdout, or redirect it to `DEVNULL` if logs are not needed.

## 2026-01-31 - [Mocking Subprocesses for Performance Testing]
**Learning:** To verify streaming optimizations without heavy dependencies (like ffmpeg), mocking `asyncio.create_subprocess_exec` is effective. However, the mock script must accurately simulate the behavior of the real tool (e.g., handling stdout vs file output) to avoid false positives/negatives in tests.
**Action:** When mocking CLI tools, ensure the mock script parses arguments to mimic the output destination (stdout or file) correctly.

## 2026-01-31 - [Blocking File I/O in Async Loop]
**Learning:** Synchronous file system operations (specifically `os.unlink`, `os.rename`, and `os.path.getsize`) are blocking calls that halt the asyncio event loop. In high-load async applications, this causes latency and can starve other concurrent tasks.
**Action:** Offload these blocking file operations to a separate thread using `asyncio.to_thread`, preventing them from blocking the main event loop.
