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

The October 4, 2026 checks downloaded the reported YouTube Short from the VPS
through both configured SOCKS5 routes. A separate check of the original-audio
fix matched the downloaded video's decoded audio against the Russian original
track. See the [dated verification record](docs/verification-2026-10-04.md) for
release identities, dependency versions, measurements, and their scope.

The full 12-Short + 12-video production-IP run remains pending Task 14. A verified
download of one Short does not establish statistical reliability or complete
Telegram delivery through a free independent external YouTube route without
cookies, a proxy, payment, or manual CAPTCHA. See
[media release acceptance](docs/acceptance.md) for that evidence contract.

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
- YouTube Shorts without an explicit quality prefer 720p, then 1080p; larger
  sources remain fallbacks. Different resolutions are tried in that order
  rather than raced against each other. Explicit quality and ordinary YouTube
  watch URLs retain their own selection behavior.
- YouTube video and audio downloads use the original audio track when yt-dlp
  identifies one. Automatic dubs are excluded from download fallbacks in that
  case. An explicitly requested audio language takes precedence; when no
  original is identified, the available tracks retain their usual ordering.
  Refreshed YouTube URLs must preserve a known selected audio language. Default
  audible YouTube requests have a separate cache policy identity so older
  translated downloads and Telegram file IDs are not reused.
- YouTube uses the configured primary/backup SOCKS5 pool for extraction and
  downloads. Other configured local extractors try direct access before the
  proxy fallback. Media streams keep the extraction route; native API providers
  retain their direct paths.
- HTTP streams honor yt-dlp's bounded range size, validate every partial
  response, and abort unread transfers during cancellation and failover.
- A direct FxTwitter MP4 may be delivered without an audio track when no audio
  was requested. Clips, transformed files, and explicit audio requests still
  require the expected audio stream.
- TikTok keeps a lightweight reserve resolver running while a selected yt-dlp
  source downloads, so a failed transfer can use the ready fallback. A successful
  native download is not held up by that reserve lookup.
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
| YouTube / Shorts local | yt-dlp with pinned EJS/Deno/PO-token support, SOCKS5 fallback, original audio, and exact quality/clip handling | Enabled when runtime dependencies are healthy | One Short downloaded through both proxy routes; original audio checked separately; 24-link run pending |
| TikTok | TikWM + SSSTik race; eligible Cobalt; one local fallback | Adapters declare support; endpoint/circuit state may disable a route | Pending controlled live run |
| X/Twitter | FxTwitter + eligible Cobalt; yt-dlp fallback | Endpoint/circuit state applies | Pending controlled live run |
| Instagram/Facebook public | Contract-gated SnapSave + eligible Cobalt; gallery-dl/yt-dlp fallback | SnapSave and Cobalt require explicit contract-verification flags | Pending controlled live run |
| Pinterest | Native provider + eligible Cobalt; gallery-dl/yt-dlp fallback | Endpoint/circuit state applies | Pending controlled live run |
| Other supported URLs | yt-dlp and, where declared, gallery-dl | Tool/provider capability applies | Not implied by offline tests |

Personal cookies are not part of the ordinary public-provider race. Configured
authorized Instagram/session or legacy cookie paths remain separate scopes and
must not populate public cache entries.

The default pipeline has no implemented independent YouTube provider. The
`independent-youtube` entry is a routing slot, not a working external fallback.
Configured proxy failover uses the same local yt-dlp backend.

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
| `MEDIA_PROXY_URLS` | JSON array of SOCKS5 URLs in primary/backup order; keep credentials outside Git |
| `MEDIA_PROXY_PLATFORMS` | Platforms whose local extractors may use the pool; YouTube uses proxies first |
| `POT_PROVIDER_URL` | bgutil PO-token server origin; Compose uses the project sidecar |
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
The GitHub `production` environment requires reviewer approval after a release
image has been verified and built. A merged commit or a successful build alone
does not mean that the release is running on the VPS.

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
