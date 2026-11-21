# Tooling and configuration comment audit

Original line numbers refer to `17ac60c7171a`. Executable Python and shell
embedded in strings are included. Documentation, fixture payloads, archived
diagnostics, and cookie data are outside the source-comment inventory.

| Classification | Standalone | Inline | Total |
| --- | ---: | ---: | ---: |
| Tautology | 55 | 1 | 56 |
| Stale | 4 | 0 | 4 |
| Excuse | 6 | 0 | 6 |
| Decision Record | 51 | 2 | 53 |
| Directive retained | 8 | 5 | 13 |
| Total | 124 | 8 | 132 |

- 0020 records image and network boundaries. [Container deployment](../adr/0020-tooling-container-boundaries.md)
- 0021 records error redaction and timeout handling. [Acceptance evidence](../adr/0021-tooling-acceptance-evidence.md)
- 0022 records browser cookie export behavior. [Cookie export](../adr/0022-tooling-cookie-export.md)
- 0023 records the environment shared by type checks. [Type-check hooks](../adr/0023-tooling-type-check-hooks.md)
- 0024 records settings scope and links to operator guidance. [Runtime configuration](../adr/0024-tooling-operator-configuration.md)
- The cookie exporter changes global streams on import. [Issue #146](https://github.com/Le-Who/ytdlbot/issues/146)
- The WireGuard example installs or builds its SOCKS5 dependency at startup. [Issue #151](https://github.com/Le-Who/ytdlbot/issues/151)
- The candidate acceptance adapter cannot cancel an accepted job by correlation ID. [Issue #152](https://github.com/Le-Who/ytdlbot/issues/152)
- Cookie export passes a placeholder URL to yt-dlp. [Issue #153](https://github.com/Le-Who/ytdlbot/issues/153)

| File | Tautology | Stale | Excuse | Decision Record | Retained directive |
| --- | --- | --- | --- | --- | --- |
| `.dockerignore` | 1, 6, 10, 19, 29, 32, 36 | — | — | — | — |
| `.env.example` | — | 62 | — | 0024 (18, 32, 34–35, 38–39, 45–52, 58–61, 63) | — |
| `.gitignore` | 1, 9, 13, 18, 25, 31, 36, 46 | — | — | — | — |
| `.pre-commit-config.yaml` | 4, 12, 31 | — | — | 0023 (13–15) | — |
| `Dockerfile` | 1, 6, 16, 24, 38, 41, 45, 48, 59, 63 | 29 | — | 0020 (12, 52, 66) | — |
| `docker-compose.wireguard.example.yml` | 1–2, 4, 9–10, 28–29, 42, 45 | 11 | #151 (33, 36) | 0024 (3, 5–8, 24, 54–60) | — |
| `docker-compose.yml` | — | — | — | 0020 (3–4, 22, 52–53, 76, 89) | — |
| `pyproject.toml` | 39 | — | — | — | — |
| `scripts/bootstrap-migrate-production.sh` | — | — | — | — | 1 |
| `scripts/bootstrap-production.sh` | — | — | — | — | 1 |
| `scripts/collect-media-release-evidence.py` | — | — | — | 0021 (161) | 1 |
| `scripts/debug_youtube.py` | 5 | — | — | 0021 (18) | 19 |
| `scripts/deploy-release.sh` | — | — | — | — | 1 |
| `scripts/legacy-media-evidence-harness.py` | — | — | — | 0021 (384) | 345–346, 385 |
| `scripts/media-acceptance-docker-adapter.py` | — | — | #152 (608–609) | 0021 (859, 1660) | 1 |
| `scripts/preflight-production.sh` | — | — | — | — | 1 |
| `scripts/rollback-release.sh` | — | — | — | — | 1 |
| `tools/export_cookies.py` | 39, 42, 48, 94, 104, 141, 241, 248, 252, 262, 266, 329, 332, 383, 492, 504, 526 | 147 | #146 (30)<br>#153 (45) | 0022 (181, 246, 432) | 1, 40 |
