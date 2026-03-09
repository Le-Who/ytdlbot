# YTDL Bot - Telegram Media Downloader

A high-performance Telegram bot for downloading media from popular platforms (YouTube, TikTok, Pinterest, VK, etc.) using `yt-dlp` and `gallery-dl`. Built with **FastAPI**, **python-telegram-bot**, and **asyncio** for maximum concurrency, offering direct downloads, smart caching, and on-the-fly media conversion.

## What It Does

YTDL Bot solves the problem of friction in downloading and sharing media from social networks. Instead of using third-party websites loaded with ads, users can interact with this Telegram bot to directly download videos, audio, and TikTok image carousels. It handles extraction, conversion, size limitations, and streaming (both direct link and Telegram upload) automatically in the background.

## Current Status

**Production-ish / API-Stabilized**
The project is well-structured and highly tested (>400 tests, CI/CD pipeline). However, since it relies heavily on third-party extraction tools (`yt-dlp`, `gallery-dl`) and platform algorithms, it is inherently subject to platform-side changes (e.g., rate limits, blockages). Some advanced evasion techniques (proxies, cookies) are configured but require manual upkeep by the admin.

## Features

- **Multi-Platform Support**: Extracts video/audio from YouTube, TikTok (watermark-free), Pinterest, VK, Facebook, and RuTube.
- **Smart Group Mode**: Automatically selects and downloads the best quality video (<45MB by default) when a link is sent in a group chat.
- **Interactive Private Mode**: Presents inline keyboard options for users to select specific video qualities or audio-only formats.
- **TikTok Slideshow Support**: Converts TikTok carousels into either a 📸 Photo Album (media group) or a 🎬 Video Slideshow (MP4 with audio) using `ffmpeg`.
- **Strict Format Binding**: Guaranteed zero-mismatch downloads across platforms. Parses formats early to skip FFmpeg muxing (pre-mux priority), conserving resources and preventing Telegram size-limit errors.
- **Zero-Disk Pipeline**: Converts video to GIF natively without saving intermediary files to disk (`yt-dlp` -> `ffmpeg` pipe).
- **Concurrency & Caching**: Employs completely asynchronous logic, `TTLCache` for format metadata, and `--load-info-json` to bypass duplicate extraction API calls.
- **Rate Limiting**: Multi-layered token bucket limiter preventing abuse per User, Chat, IP, and Token.
- **Monitoring**: Built-in Prometheus-compatible metrics endpoint for observability.

## Non-Goals / Limitations

- Exceeding Telegram's hard 50MB bot upload limit is fundamentally restricted (downloads >45MB are either aborted or sent as direct HTTP download links).
- Direct age-restricted or pure-private content downloads might fail unless `*_COOKIES_B64` variables are manually configured and refreshed by the server admin.
- The bot is not a permanent file host. HTTP direct download links expire based on `LINK_TTL_MINUTES`.

## Architecture

- **Web Layer**: FastAPI serves HTTP endpoints (health checks, Prometheus metrics, and chunked video streams) and handles incoming Telegram Webhooks.
- **Telegram Logic**: `python-telegram-bot` processes updates. Callback handlers are uniquely "thin", dispatching tasks immediately to the orchestrator.
- **Orchestration Layer**: `DownloadOrchestrator` centralizes all download lifecycles, safely encapsulating complex rules like concurrency queues (`asyncio.Semaphore`), file-size checks, and fallback mechanisms.
- **Data Fetchers**: `YtDlpService` and `GalleryDlService` act as async wrappers over CLI binaries.
- **Media Processing**: `FFmpeg` is utilized exclusively for post-processing tasks (GIF conversion, slideshow building).
- **State Management**: In-memory `TTLCache` structures govern rate limiting and URL metadata caching securely using a strongly-typed `DownloadContext` dataclass.

```mermaid
flowchart TD
    User([Telegram User]) -- Message/URL --> TG[Telegram API]
    TG -- Webhook --> FA[FastAPI App]
    FA -- Extract Meta --> YD[yt-dlp / gallery-dl]
    FA -- User Picks Format --> TG
    User -- Click Download --> TG
    TG -- Callback --> FA
    FA -- Stream / Download --> YD
    YD -- HTTP Stream --> FA
    FA -- FFmpeg Pipe --> FF[FFmpeg Converter]
    FF -- GIF/MP4 --> TG
    FA -- Direct Link --> User
```

## Repository Structure

| Path            | Purpose                                                                    |
| --------------- | -------------------------------------------------------------------------- |
| `app/api/`      | FastAPI routes for Webhooks, `/health`, `/metrics`, and `/dl/{token}`.     |
| `app/bot/`      | Telegram bot command/message handlers and inline keyboards.                |
| `app/core/`     | Global config, rate limiter logic, caching, and state structures.          |
| `app/services/` | Wrappers for `yt-dlp`, `gallery-dl`, `ffmpeg` conversion, and downloading. |
| `app/tasks/`    | Background periodic tasks (e.g., `janitor.py` for temp cleanup).           |
| `tests/`        | 434+ Pytest tests covering unit, integration, and security.                |
| `Dockerfile`    | Multi-stage build definition for containerized deployment.                 |
| `scripts`       | Python standalone script for local debugging of `yt-dlp` extraction.       |

## Tech Stack

| Layer           | Technology          | Purpose                                              |
| --------------- | ------------------- | ---------------------------------------------------- |
| Web Framework   | FastAPI / Uvicorn   | Webhooks, streaming downloads, metrics               |
| Bot Framework   | python-telegram-bot | Telegram API interface and callback routing          |
| Extraction Core | yt-dlp / gallery-dl | Resolving platform links to raw media URLs           |
| Media Engine    | FFmpeg              | Media manipulation, GIF conversion, merging          |
| Fingerprinting  | curl_cffi           | TLS impersonation (used for TikWM fallback handling) |

## Setup

1. **Clone Repo**:
   ```bash
   git clone https://github.com/yourusername/ytdlbot.git
   cd ytdlbot
   ```
2. **Setup Virtual Environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```
3. **Install Requirements**: Ensure system `ffmpeg` is installed first.
   ```bash
   pip install -r requirements.txt
   ```
4. **Environment Configuration**:
   ```bash
   cp .env.example .env
   # Add your BOT_TOKEN to .env
   ```

## Configuration

Selected key variables from `.env.example`:

| Variable                | Required | Default                 | Description                                     | Used In          |
| ----------------------- | -------- | ----------------------- | ----------------------------------------------- | ---------------- |
| `BOT_TOKEN`             | **Yes**  | —                       | Telegram Bot Token from @BotFather              | Core Bot Setup   |
| `BASE_URL`              | No       | `http://localhost:8000` | External endpoint base for generated DL links   | HTTP API         |
| `WEBHOOK_URL`           | No       | —                       | If set, FastAPI acts as webhook. Else, polling  | Webhook setup    |
| `MAX_TG_UPLOAD_MB`      | No       | `45`                    | Maximum size for direct Telegram upload         | Download Limiter |
| `LIMITER_USER_CAPACITY` | No       | `10`                    | Rate limit tokens per user                      | Core Limiter     |
| `YTDLP_COOKIES_B64`     | No       | —                       | Base64-encoded Netscape cookies for Auth bypass | `yt-dlp` Service |

## Run

Run the bot natively using Uvicorn (uses Polling if `WEBHOOK_URL` is empty):

```bash
uvicorn app.main:api --reload
```

## Scripts

| Command          | Purpose                                                                                            |
| ---------------- | -------------------------------------------------------------------------------------------------- |
| `python scripts` | Standalone script for testing `yt-dlp` raw format extraction locally without running the full bot. |

## Testing

The project uses `pytest` heavily, along with `mutmut` for mutation testing and `hypothesis` for property-based tests. Test thresholds are strictly enforced via the CI pipeline (`.github/workflows/test.yml`).

| Test Type       | Tooling               | Command                                                                                          | Scope                                                                    |
| --------------- | --------------------- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------ |
| **Unit**        | `pytest` + `coverage` | `python -m pytest tests/ --cov=app --cov-report=term-missing -v --tb=short -m "not integration"` | Tests core logic, mock external APIs, parsers. Fails under 75% coverage. |
| **Integration** | `pytest`              | `python -m pytest tests/test_integration_real.py -m integration --no-cov -v`                     | Requires real `yt-dlp` and `ffmpeg` binaries. Verifies real downloads.   |
| **Mutation**    | `mutmut`              | `mutmut run`                                                                                     | Mutates format parsing logic to check test robustness.                   |

_Prerequisites: System must have `ffmpeg` and local `python -m pytest` available._

## API / Events / Contracts

### HTTP Routes (FastAPI)

- `GET /health` : Liveness probe.
- `GET /metrics` : Prometheus-compatible metrics output.
- `GET /dl/{token}` : Streams media chunks directly to clients given a valid token payload.
- `POST /webhook` : Telegram Webhook destination. Validates `X-Telegram-Bot-Api-Secret-Token`.

### Telegram Commands (PTB)

- `/start` : Initializes bot, registers user intent, prints welcome menu.
- `/help` : Displays supported platforms and current size limit constants.

### Action Callbacks

- `^pick\|` : Quality format selected by user.
- `^cancel\|` : Task cancellation request.
- `^send\|` | `^gif\|` : Post-download interactive transforms.
- `^slideshow\|` / `^grpslide\|` : Specific selectors for TikTok carousel behavior.

## Main User Flows

### Flow 1: Private Quality Selection

- **Preconditions**: User sends a valid supported link to the bot privately.
- **Steps**:
  1. Bot replies with "Processing..." and extracts formats.
  2. Bot presents inline keyboard with specific resolutions (e.g., 1080p, 720p) and sizes.
  3. User clicks an inline button.
- **Expected Outcome**: Bot downloads the requested track, uploads it back to Telegram, and removes its temporary files.

### Flow 2: Smart Group Mode

- **Preconditions**: Bot is added to a group with message read permissions, and someone posts a link.
- **Steps**:
  1. Bot intercepts the link and parses formats.
  2. Logic bypasses interactive keys, auto-selecting the best format below `MAX_TG_UPLOAD_MB`.
  3. Bot posts the video directly as a reply to the original message.
- **Expected Outcome**: Immediate, seamless media playback in group without menu spam.

### Flow 3: Fast GIF Conversion

- **Preconditions**: Bot previously posted a video into the chat.
- **Steps**:
  1. User presses the inline "Send GIF" button attached to the bot's video message.
  2. Webhook triggers GIF callback handler.
- **Expected Outcome**: Bot relies on the cached stream (zero-disk streaming via `pipe:0` to `ffmpeg`), converts it instantly, and returns an animated GIF version.

## Troubleshooting

- **Large file fails to upload**: Verify `MAX_TG_UPLOAD_MB` is not set above Telegram's 50MB hard limit. The bot cleanly aborts uploads larger than this context.
- **429 Too Many Requests**: You hit the token bucket. Inspect `LIMITER_*` variables in `.env` if developing locally.
- **TikTok extraction fails**: Usually caused by aggressive datacentre IP blocks. Feed `TIKTOK_COOKIES_B64` or a residential `TIKTOK_PROXY` into the environment.

## Known Documentation Gaps

- **Script naming discrepancy**: Older documentation specifies a `scripts` folder containing CLI scripts, but the repo possesses a single `scripts` flat file containing python code used for debug purposes.
- **Metrics documentation missing**: Although the bot exposes a standard operational `/metrics` route (Prometheus histograms), this was notably under-documented in prior iterations despite being active in `app.api.routes`.
- **Property testing details**: The GitHub Actions integration testing workflow actually defaults to `mutmut run` succeeding loosely (`|| true`), indicating mutation metrics are likely informative, not strictly enforcing build failure at this time in the test suite.

## Contributing

1. Create feature branches (`feature/Name`).
2. Pass `pre-commit run --all-files` locally (which triggers `ruff`, `mypy`).
3. Ensure the unit test suite passes with `python -m pytest tests`.
4. Submit PRs against `main`. All CI checks must pass.

## License

Distributed under the MIT License.
