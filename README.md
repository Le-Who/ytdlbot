# YTDL Bot - Telegram Media Downloader

A high-performance Telegram bot for downloading media from popular platforms (YouTube, TikTok, Pinterest, VK, etc.) using `yt-dlp` and `gallery-dl`. Built with **FastAPI**, **python-telegram-bot**, and **asyncio** for maximum concurrency and efficiency.

## 🚀 Key Features

- **Multi-Platform Support**: Downloads videos from YouTube, TikTok (no watermark), Pinterest, VK, Facebook, and RuTube (including Shorts) via `yt-dlp`.
- **TikTok Slideshow Support**: Downloads TikTok image carousel (slideshow) posts via `gallery-dl` with two output modes:
  - **📸 Photo Album** — sends individual images as a Telegram media group (up to 10 photos).
  - **🎬 Video Slideshow** — combines images + audio into an MP4 via `ffmpeg`.
- **Smart Quality Selection**:
  - **Private Chat**: Offers an interactive menu to choose video quality (1080p, 720p, etc.) or Audio only.
  - **Group Mode**: Automatically downloads the best quality video (<45MB) to ensure extensive compatibility and fast sharing without spamming the chat.
- **GIF Conversion**:
  - Easily convert any downloaded video to a GIF with a single click in Group chats.
  - Smart caching reuses the downloaded video file for instant conversion.
- **High Performance**:
  - **Async/Await**: Fully asynchronous architecture to handle multiple downloads simultaneously.
  - **Smart Caching**: In-memory caching of video metadata (`TTLCache`) to reduce duplicate API calls to platforms.
  - **Instant GIF Streaming**: Zero-disk pipelining (`yt-dlp` -> `ffmpeg`) for GIF conversion, enabling an immediate "Time-To-First-Byte" playback.
  - **Robust Memory Management**: Complete protection against `yt-dlp`/`ffmpeg` zombie processes via cross-platform Process Group termination and strict `asyncio.timeout` bounds.
- **Robust Error Handling**: Handles regional restrictions, private content, live streams, and large file limits gracefully with dedicated exception types.
- **Admin Tools**: Multi-layer rate limiting (per user, chat, IP, and token) and configurable download policies.

## 🛠 Tech Stack

- **Language**: Python 3.12+
- **Framework**: [FastAPI](https://fastapi.tiangolo.com/) 0.135.x (Web Server & Webhook handling)
- **Bot Framework**: [python-telegram-bot](https://python-telegram-bot.org/) 22.x
- **Core Engine**: [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Video/audio extraction)
- **Image Downloader**: [gallery-dl](https://github.com/mikf/gallery-dl) (TikTok slideshow image extraction)
- **Processing**: [FFmpeg](https://ffmpeg.org/) (Video/Audio processing, GIF conversion & slideshow-to-video)
- **Containerization**: Docker (Python 3.12-slim)
- **CI/CD**: GitHub Actions (automated testing with coverage)

## 📂 Project Structure

```
.
├── app
│   ├── api                # FastAPI routes (webhooks, health checks, streaming)
│   │   └── routes.py      # Webhook endpoint, /dl/{token}, /health
│   ├── bot                # Telegram Bot logic
│   │   ├── callbacks.py   # Button interactions (Download, Cancel, GIF)
│   │   ├── commands.py    # /start, /help handlers
│   │   ├── group_logic.py # Group chat specific logic
│   │   ├── keyboards.py   # Inline keyboard builders
│   │   └── messages.py    # Private chat message handlers
│   ├── core               # Core configurations & utilities
│   │   ├── cache.py       # Centralized TTLCache factories
│   │   ├── config.py      # Environment variables settings
│   │   ├── limiter.py     # Token bucket rate limiter + LimiterRegistry
│   │   ├── logging.py     # Structured JSON logging + correlation IDs
│   │   ├── policy.py      # Size policy checks
│   │   ├── process.py     # Cross-platform process group management
│   │   ├── state.py       # Global state (locks, caches, semaphores)
│   │   ├── texts.py       # All user-facing UI strings (i18n-ready)
│   │   └── utils.py       # Helper functions
│   ├── constants.py       # Shared constants (format IDs, limits)
│   ├── services
│   │   ├── downloader.py  # MediaSender service (Download/Send/Convert/Slideshow)
│   │   ├── gallery_dl     # gallery-dl wrapper for TikTok slideshows
│   │   │   └── service.py # GalleryDlService (image + audio download)
│   │   └── ytdlp          # yt-dlp wrapper service
│   │       ├── service.py # YtDlpService facade
│   │       ├── parsers.py # Format parsing, deduplication & slideshow detection
│   │       ├── models.py  # FormatItem, FormatMetadata dataclasses
│   │       ├── builders.py# Command-line builders (ffmpeg flags)
│   │       ├── cookies.py # Cookie file management
│   │       └── exceptions.py # Domain-specific errors
│   ├── tasks
│   │   └── janitor.py     # Periodic temp file cleanup
│   └── main.py            # Application entry point
├── tests/                 # 241 tests (unit + integration)
├── .github/workflows/     # CI/CD pipeline
├── Dockerfile             # Docker build (Python 3.12-slim)
├── .dockerignore          # Excludes .git, tests, IDE files from build context
├── pyproject.toml         # pytest + coverage config
├── requirements.txt       # Python dependencies (pinned)
└── README.md
```

## ⚙️ Installation & Setup

### Prerequisites

- Python 3.12+
- FFmpeg (installed and in system PATH)
- gallery-dl (optional, required for TikTok slideshow downloads)

### Local Development

1.  **Clone the repository**:

    ```bash
    git clone https://github.com/yourusername/ytdlbot.git
    cd ytdlbot
    ```

2.  **Create a virtual environment**:

    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3.  **Install dependencies**:

    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure Environment**:
    Copy `.env.example` to `.env` and fill in the required values:

    ```bash
    cp .env.example .env
    ```

    At minimum, set `BOT_TOKEN`. See [Configuration](#-configuration) for all options.

5.  **Run the bot**:
    ```bash
    uvicorn app.main:api --reload
    ```
    The bot will start in Polling mode if `WEBHOOK_URL` is not set.

## 🐳 Docker Deployment

1.  **Build the image**:

    ```bash
    docker build -t ytdlbot .
    ```

2.  **Run the container**:

    ```bash
    docker run -d --name ytdlbot \
      -e BOT_TOKEN=your_token \
      -e BASE_URL=https://your-domain.com \
      -e WEBHOOK_URL=https://your-domain.com/webhook \
      -p 8000:8000 \
      ytdlbot
    ```

    The `PORT` environment variable (default `8000`) controls uvicorn's listening port and is usually overridden by your hosting platform (Northflank, Railway, etc.).

## 🧪 Testing

Run the full test suite:

```bash
BOT_TOKEN=test pytest tests/ -v
```

With coverage report:

```bash
BOT_TOKEN=test pytest tests/ --cov=app --cov-report=term-missing
```

The test suite includes:

- **241 unit + integration tests**
- HMAC webhook authentication tests
- Rate limiter behavior tests
- Format parsing and deduplication tests
- TikTok slideshow detection and callback tests
- Gallery-dl service command and error handling tests
- Full end-to-end flow tests (URL → formats → pick → download → send)

## 🎮 Usage

### Private Chat

1.  Send a link (e.g., TikTok, YouTube).
2.  Wait for the bot to fetch formats.
3.  Choose your desired quality or format (Audio/Video).
4.  For TikTok slideshows: choose **📸 Фото** (album) or **🎬 Видео** (slideshow→video).
5.  Get a direct download link **or** receive the file directly in Telegram.

### Group Chat

1.  Add the bot to a group.
2.  Send a link.
3.  The bot automatically downloads the best suitable video and sends it.
4.  For TikTok slideshows: choose **📸 Альбом** or **🎬 Видео**.
5.  Click **"Send GIF"** on a video reply to instantly get a GIF version.

## 🔧 Configuration

Use `.env.example` as baseline. All variables are read from environment or `.env` file via `python-dotenv`.

### Core

| Variable                | Description                                           | Default                 |
| ----------------------- | ----------------------------------------------------- | ----------------------- |
| `BOT_TOKEN`             | Telegram bot token (from @BotFather)                  | **required**            |
| `BASE_URL`              | Public URL of the service (for download links)        | `http://localhost:8000` |
| `WEBHOOK_URL`           | Webhook URL (omit for polling mode)                   | —                       |
| `TELEGRAM_SECRET_TOKEN` | HMAC secret for webhook auth (auto-generated if omit) | random                  |
| `TMPDIR`                | Temporary files directory                             | system temp             |
| `PORT`                  | Uvicorn listening port                                | `8000`                  |

### Limits & Policies

| Variable                 | Description                              | Default |
| ------------------------ | ---------------------------------------- | ------- |
| `MAX_TG_UPLOAD_MB`       | Max file size for Telegram upload        | `45`    |
| `MAX_DL_MB`              | Max file size for HTTP download          | `1000`  |
| `MAX_CONCURRENT_TASKS`   | Max simultaneous downloads               | `2`     |
| `ENABLE_TELEGRAM_UPLOAD` | Show "Send to Telegram" button (`1`/`0`) | `1`     |
| `LINK_TTL_MINUTES`       | Download link expiry time                | `30`    |
| `DL_TIMEOUT_TELEGRAM`    | Timeout for Telegram send (seconds)      | `600`   |
| `DL_TIMEOUT_HTTP`        | Timeout for HTTP downloads (seconds)     | `900`   |

### Rate Limiting

| Variable                       | Description                | Default |
| ------------------------------ | -------------------------- | ------- |
| `LIMITER_USER_CAPACITY`        | Per-user token bucket size | `10`    |
| `LIMITER_USER_REFILL_PER_SEC`  | Per-user refill rate       | `0.5`   |
| `LIMITER_CHAT_CAPACITY`        | Per-chat token bucket size | `20`    |
| `LIMITER_CHAT_REFILL_PER_SEC`  | Per-chat refill rate       | `1`     |
| `LIMITER_IP_CAPACITY`          | Per-IP token bucket size   | `15`    |
| `LIMITER_IP_REFILL_PER_SEC`    | Per-IP refill rate         | `1`     |
| `LIMITER_TOKEN_CAPACITY`       | Per-token bucket size      | `3`     |
| `LIMITER_TOKEN_REFILL_PER_SEC` | Per-token refill rate      | `0.25`  |

### Maintenance

| Variable                   | Description                 | Default |
| -------------------------- | --------------------------- | ------- |
| `MAX_TEMP_AGE_SECONDS`     | Temp file cleanup threshold | `3600`  |
| `JANITOR_INTERVAL_SECONDS` | Cleanup task interval       | `300`   |

| Variable               | Description                                                                | Default |
| ---------------------- | -------------------------------------------------------------------------- | ------- |
| `YTDLP_COOKIES_B64`    | Base64-encoded Netscape cookies file — global fallback (YouTube, VK, etc.) | —       |
| `TIKTOK_COOKIES_B64`   | Base64-encoded cookies for TikTok (overrides global)                       | —       |
| `FACEBOOK_COOKIES_B64` | Base64-encoded cookies for Facebook (overrides global)                     | —       |
| `TIKTOK_PROXY`         | SOCKS5/HTTP proxy for TikTok (datacenter IP bypass)                        | —       |

Cookies allow downloading age-restricted, private, or sign-in-required content. Priority: **platform-specific → global fallback**.

To set up cookies:

1. Export your cookies from a browser using an extension like **"Get cookies.txt LOCALLY"**.
2. Encode the file: `base64 -w0 cookies.txt` (Linux) or `[Convert]::ToBase64String([IO.File]::ReadAllBytes('cookies.txt'))` (PowerShell).
3. Set the appropriate env var (`YTDLP_COOKIES_B64` for YouTube/general, `TIKTOK_COOKIES_B64` for TikTok, `FACEBOOK_COOKIES_B64` for Facebook).

> **Note**: Cookies expire periodically (1–4 weeks depending on platform) and will need to be re-exported.

### Structured Logging

JSON logs include: `correlation_id`, `op`, `duration_ms`, `error_type`, and optional context fields (`token`, `chat_id`, `user_id`, `url_host`).

## 🌐 i18n

All user-facing strings are centralized in [`app/core/texts.py`](app/core/texts.py). To localize the bot, replace the `Texts` class constants or implement a locale-based lookup.

## 🤝 Contributing

Contributions are welcome! Please follow these steps:

1.  Fork the repository.
2.  Create a feature branch (`git checkout -b feature/AmazingFeature`).
3.  Commit your changes (`git commit -m 'Add some AmazingFeature'`).
4.  Push to the branch (`git push origin feature/AmazingFeature`).
5.  Open a Pull Request.

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
