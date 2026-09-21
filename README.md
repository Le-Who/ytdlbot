# YTDL Bot

YTDL Bot is a Telegram media downloader built with Python 3.12, FastAPI,
python-telegram-bot, yt-dlp, gallery-dl, and ffmpeg. One media pipeline normalizes
requests, races eligible providers, validates equivalent candidates, streams or
transforms the selected media, and records the Telegram delivery result.

## Verification status

The repository contains implemented provider adapters, deterministic offline
contracts, durable job handling, and immutable bot-only deployment scripts.
Those facts do **not** establish current production reachability of third-party
providers.

The 12-Short + 12-video production-IP run is intentionally pending Task 14. In
particular, no checked-in evidence currently proves complete delivery through a
free independent external YouTube route without cookies, a proxy, payment, or
manual CAPTCHA. See [media release acceptance](docs/acceptance.md) for the exact
boundary and evidence format.

## Capabilities

- Canonical YouTube identity across watch, Shorts, mobile, and `youtu.be` URLs,
  with clip, quality, audio, album, transform, caller, and authorization scope in
  the versioned cache key.
- A bounded provider race: at most two cheap external resolves at once and one
  heavy local extractor for a request. Invalid or non-equivalent candidates do
  not win because they returned first.
- Streaming materialization with redirect/DNS/private-address checks, byte caps,
  disk reservation, renewable leases, stall failover, and partial-file cleanup.
- Telegram `file_id` reuse before provider work. Public results may be shared
  across users for the same bot; authorized results remain isolated.
- Exact media requests: quality, audio format/language, clip interval, watermark
  policy, media kind, album selection, and item order are not silently changed.
- Video, audio, photo, animation, document, mixed album, MP3, MP4, GIF-file, and
  explicit slideshow delivery paths.
- Cancellation-safe queue leases and supervised yt-dlp, gallery-dl, ffmpeg, and
  ffprobe process groups.
- Durable SQLite WAL inbox with `update_id` deduplication, job recovery, delivery
  receipts, drain/checkpoint handling, and bounded update-payload retention.
- Prometheus metrics for pipeline phase/result latency, cache hits, provider wins,
  retries, wasted bytes, transform workload, queue state, and orphan processes.

Media processing is conditional, not free: compatible files may be delivered or
remuxed directly, while clips, strict MP3, GIFs, slideshows, incompatible codecs,
and other requested transforms use disk, network, and/or CPU.

## Provider state

“Implemented” means an adapter and offline contracts exist. “Eligible” means the
current configuration allows the route. Neither means production-IP verified.

| Route | Implemented behavior | Eligibility | Production-IP evidence in this repository |
|---|---|---|---|
| YouTube / Shorts local | yt-dlp with pinned EJS/Deno/PO-token support, exact quality/audio/clip handling | Enabled when runtime dependencies are healthy | Pending controlled 24-link run |
| TikTok | TikWM + SSSTik race; eligible Cobalt; one local fallback | Adapters declare support; endpoint/circuit state may disable a route | Pending controlled live run |
| X/Twitter | FxTwitter + eligible Cobalt; yt-dlp fallback | Endpoint/circuit state applies | Pending controlled live run |
| Instagram/Facebook public | Contract-gated SnapSave + eligible Cobalt; gallery-dl/yt-dlp fallback | SnapSave and Cobalt require explicit contract-verification flags | Pending controlled live run |
| Pinterest | Native provider + eligible Cobalt; gallery-dl/yt-dlp fallback | Endpoint/circuit state applies | Pending controlled live run |
| Other supported URLs | yt-dlp and, where declared, gallery-dl | Tool/provider capability applies | Not implied by offline tests |

Personal cookies are not part of the ordinary public-provider race. Configured
authorized Instagram/session or legacy cookie paths remain separate scopes and
must not populate public cache entries.

No provider quota is promised here. HTTP 403/429 and `Retry-After` are observed
and classified at runtime; provider health and commercial terms can change.

## Production delivery profile

Production requires a functional Local Telegram Bot API and a shared persistent
media path:

- bot: `/srv/ytdlbot/media` read/write;
- Local Bot API: the same absolute path read-only;
- durable state: `/srv/ytdlbot/state/jobs.sqlite3` on a separate state volume.

`MAX_MEDIA_FILE_MB=2000` is the single upper policy limit, with decimal MB
(2,000,000,000 bytes maximum). It is a protocol/policy ceiling, not a guarantee of
free disk, source availability, transformation time, or successful delivery for
every file. Cloud Bot API mode is an explicit degraded profile with its own lower
limit; the bot does not silently reduce a selected large file to fit it.

`/health/ready` exposes the release SHA, active profile/limit, durable-store
write/schema state, worker ownership, and a bounded authenticated Local Bot API
probe. Optional provider failure does not make the whole bot unready; required
store or Local Bot API failure does.

## Durable delivery semantics

Webhook updates are acknowledged only after transactional insertion. A single
worker claims accepted jobs. During deploy drain, new updates are still persisted
while no new heavy work starts. Checkpointed or abandoned work is recovered by
the next worker.

Per-item receipt outcomes are important:

- `success` is never replayed;
- known `failed` work may be recovered by the bounded startup policy;
- `uncertain` is not retried automatically because Telegram may already have
  accepted the send.

Normal deployment shutdown keeps the webhook configured.

## Setup

Prerequisites:

- Python 3.12;
- ffmpeg and ffprobe;
- Redis for the production cache/limiter profile;
- Docker Compose for the documented production topology;
- a Local Telegram Bot API server for production delivery.

Local setup:

```sh
python -m venv .venv
# Linux/macOS: . .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-ci.txt
cp .env.example .env
```

Set at least `BOT_TOKEN`. Webhook mode also needs `WEBHOOK_URL` and
`TELEGRAM_SECRET_TOKEN`; without a webhook URL the application may use its local
polling path.

Selected configuration:

| Variable | Meaning |
|---|---|
| `APP_RELEASE` | Exact release SHA reported by readiness |
| `MAX_MEDIA_FILE_MB` | Decimal-MB media ceiling, maximum 2000 |
| `TELEGRAM_CLOUD_MAX_FILE_MB` | Explicit degraded cloud-profile ceiling |
| `TELEGRAM_LOCAL_ENDPOINT` | Internal Local Bot API origin |
| `TELEGRAM_LOCAL_REQUIRED` | Require Local Bot API for readiness |
| `MEDIA_DIR` | Persistent shared media directory |
| `YTDLBOT_JOB_DB` | Persistent SQLite job-store path |
| `COBALT_API_URL` | Comma-separated exact Cobalt origins; no origin is trusted automatically |
| `COBALT_CONTRACT_VERIFIED` | Enables configured Cobalt routes only after contract verification |
| `SNAPSAVE_CONTRACT_VERIFIED` | Enables SnapSave only after contract verification |

Review `.env.example` and `app/core/config.py` for the complete set. Do not commit
tokens, cookies, sessions, provider keys, or signed media URLs.

Run locally:

```sh
uvicorn app.main:api --reload
```

Useful HTTP routes:

- `GET /health/live` — process liveness;
- `GET /health/ready` — release and required dependency readiness;
- `GET /metrics` — Prometheus exposition;
- `POST /webhook` — authenticated Telegram webhook admission;
- `GET /dl/{token}` — bounded cached media download route.

## Testing

Deterministic local suite:

```sh
python -m pytest tests -q
ruff check app tests
mypy app
```

Offline release acceptance:

```sh
python -m pytest tests/acceptance/test_media_release.py --no-cov -q
```

Network diagnostics are excluded by default and opt-in in
`.github/workflows/integration.yml`. GitHub-hosted diagnostics do not count as
production-IP acceptance. The controlled production procedure and redacted
evidence fields are documented in [docs/acceptance.md](docs/acceptance.md).

## Deployment

Routine releases build one immutable bot image and replace only the bot service.
The first shared-media/state-volume migration is a separately authorized
bootstrap operation. A single bot instance has a bounded drain/restart pause;
zero downtime is not promised.

See [docs/deployment.md](docs/deployment.md) for bootstrap, readiness, manual
activation, rollback, and recovery commands. Rollback restores image and
configuration but never rolls SQLite or Redis data backward. Production JSON
logs opt into the existing shared Alloy/Loki stack without changing its other
bot streams; the deployment guide documents the labels and redaction contract.

## Repository layout

| Path | Purpose |
|---|---|
| `app/services/media/` | Canonical contract, registry, race, transport, cache integration, pipeline, delivery, providers |
| `app/core/` | Configuration, durable store/drain, resource budgets, queues, metrics, storage |
| `app/bot/` | Telegram commands, messages, callbacks, and compatibility adapters |
| `app/api/` | Health, metrics, webhook admission, and download routes |
| `scripts/` | Bootstrap, immutable release, preflight, rollback, and local debugging scripts |
| `tests/` | Unit, contract, deployment, offline acceptance, and opt-in integration tests |

## License

Distributed under the MIT License.
