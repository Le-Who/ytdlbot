# Repository instructions

YTDL Bot shares one media pipeline across Telegram and HTTP entrypoints, with a
durable SQLite inbox and Local Bot API delivery. Use [README.md](README.md) for
the module map and setup.

## Read for the task

- **Code, tests, dependencies, or configuration:** Before editing, read
  [changes and verification](CODING_STANDARDS.md#changes-and-verification).
- **Media pipeline, requests, providers, cache, transport, callbacks, or sends:** Read
  [media contracts](CODING_STANDARDS.md#media-contracts).
- **Webhooks, jobs, drain, recovery, or Telegram sends:** Read
  [durable delivery](CODING_STANDARDS.md#durable-delivery).
- **Settings, dependencies, logging, readiness, Compose, or releases:** Read
  [runtime and release boundaries](CODING_STANDARDS.md#runtime-and-release-boundaries).
- **Optimization:** Read [performance evidence](CODING_STANDARDS.md#performance-evidence).
- **Live provider checks or production delivery/performance claims:** Read
  [verification boundaries](docs/acceptance.md#current-verification-boundary),
  [controlled runs](docs/acceptance.md#controlled-task-14-run), and
  [redacted evidence](docs/acceptance.md#redacted-evidence-format).

Keep commits, code comments, and work reports emoji-free.
