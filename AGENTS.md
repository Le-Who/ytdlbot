# Repository instructions

YTDL Bot has one Telegram media pipeline, a durable SQLite job inbox, and a
shared Local Telegram Bot API delivery profile. Start with the affected path;
the module map and setup are in [README.md](README.md).

## Read for the task

- **Code, tests, dependencies, or configuration:** Read
  [CODING_STANDARDS.md](CODING_STANDARDS.md) before editing, then use its relevant
  sections.
- **Media requests, providers, cache, transport, callbacks, or delivery:** Read
  [media acceptance](docs/acceptance.md) for compatibility obligations and the
  evidence needed to establish provider behavior.
- **Releases, Compose, readiness, or production operations:** Read
  [deployment and rollback](docs/deployment.md) and the current
  [release workflow](.github/workflows/deploy.yml). The release branch is `vps`.
- **Initial shared media/state volume migration:** Also read
  [production bootstrap](docs/bootstrap-production.md). This is a separately
  authorized operation with a different scope from a routine bot release.

## Evidence and reporting

Provider adapters, offline contracts, and GitHub network diagnostics establish
different things. Keep production reachability, full Telegram delivery, and
performance claims within the measured scope defined by media acceptance.

Use the existing documentation as the home for changes to setup, contracts,
acceptance, and deployment procedures. Keep tokens, cookies, sessions, provider
credentials, signed media URLs, and unredacted user data out of Git and reports.

Keep commits, code comments, and work reports free of emojis. Describe the
changed behavior and the verification performed; include any unrun relevant
check with its concrete limitation.
