## 2026-01-31 - [Subprocess Deadlock with Pipes]
**Learning:** When using `asyncio.subprocess` with `stderr=PIPE`, you MUST consume the stderr stream concurrently while reading stdout or waiting for the process. Failure to do so can cause the subprocess to block (deadlock) if the stderr buffer fills up (typically 64KB).
**Action:** Always drain stderr asynchronously using `asyncio.create_task` when streaming stdout, or redirect it to `DEVNULL` if logs are not needed.

## 2026-01-31 - [Mocking Subprocesses for Performance Testing]
**Learning:** To verify streaming optimizations without heavy dependencies (like ffmpeg), mocking `asyncio.create_subprocess_exec` is effective. However, the mock script must accurately simulate the behavior of the real tool (e.g., handling stdout vs file output) to avoid false positives/negatives in tests.
**Action:** When mocking CLI tools, ensure the mock script parses arguments to mimic the output destination (stdout or file) correctly.

## 2026-02-01 - [Pre-compiling Regex in Hot Loops]
**Learning:** Using `re.search` inside a frequently called method causes repeated cache lookups or recompilation of the regex pattern. Pre-compiling the regex into a module-level constant avoids this overhead and speeds up execution significantly (measured ~40% improvement in micro-benchmarks).
**Action:** Identify regex patterns used in hot paths and compile them once using `re.compile()` at module or class level.
