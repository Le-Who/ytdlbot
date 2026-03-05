# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- **Facebook Video support**: Download videos from `facebook.com` and `fb.watch` URLs via yt-dlp
- **Rutube Shorts**: Explicitly supported (already worked via Rutube extractor, now documented in UI)
- **`PlatformCookiesManager`**: Per-platform cookie management with global fallback
  - `YTDLP_COOKIES_B64` — global cookies (YouTube, VK, etc.)
  - `TIKTOK_COOKIES_B64` — TikTok-specific override
  - `FACEBOOK_COOKIES_B64` — Facebook-specific override
  - Priority: platform-specific → global fallback
- **TikTok content-type router**: Pre-routes `/photo/` URLs to slideshow pipeline before yt-dlp extraction
- **`TikTokError` enum**: Structured error classification replacing fragile string matching
- **`_is_facebook()` helper**: URL detection for Facebook domains
- **`classify_tiktok_content()`**: URL pattern classifier (slideshow vs video)
- **`classify_tiktok_error()`**: Error message → enum classifier
- **New tests**: `test_cookies_manager.py` (4), `test_tiktok_content_router.py` (15), `_is_facebook` in url helpers
- **Static analysis tooling**: `mypy.ini` for type-checking config, ruff per-file ignores in `pyproject.toml`

### Changed

- **`_base_opts()`**: Universal cookie routing via `get_cookies_path(url)` — all platforms with configured cookies receive them
- **`build_command()`**: Cookies passed per-platform instead of TikTok-only
- **`list_formats()` TikTok error handling**: Refactored from string matching to enum-based routing
- **Facebook format labels**: Infer `height` from `format_id` (`sd`→360p, `hd`→720p), accept formats without `ext`, fallback `📹 Video` instead of `📹 ???p`
- **Dockerfile**: Added Deno JS runtime + `yt-dlp-ejs` for YouTube n-parameter challenge solving
- **`GROUP_VIDEO_FORMAT`**: Relaxed format selector with HLS/height-only fallbacks for platforms without filesize metadata (Rutube Shorts)
- **`build_command()`**: Added `bestvideo[height<=X]+bestaudio` fallback for HLS-only platforms
- **`classify_tiktok_error()`**: Route `"not available"` / `"status code"` errors → `AUTH_REQUIRED` → TikWM fallback instead of slideshow misdetection
- **Test suite**: Expanded from 232 to 249 tests (all passing)

### Fixed

- **Ruff: 27 errors → 0**: Auto-fixed unused imports, removed dead code (`kb_cancel`, `safe_remove`), suppressed intentional E402 in tests
- **Mypy: 170 errors → 0**: Full type safety across 82 source files
  - PTB handlers: `isinstance(q.message, Message)` narrowing for `MaybeInaccessibleMessage` union
  - `assert` narrowing for `callback_query`, `effective_user`, `effective_chat`, `user_data`
  - Fixed implicit `Optional` in `sender.py` (`str = None` → `str | None = None`)
  - Typed `state.py` globals (`active_processes`, `processing_gifs`, `bot_app`)
  - Explicit `TELEGRAM_SECRET_TOKEN: str` annotation in `config.py`
  - `assert proc.stdout/stderr` narrowing in `downloader.py` and `process.py`
  - `assert bot_app.updater` in `main.py`
- **Test mocks: `MagicMock(spec=Message)`**: All callback test mocks now use `spec=Message` so `isinstance` checks pass correctly
- **`test_core_utils_extract.py` module corruption**: Fixed `sys.modules["telegram"] = MagicMock()` → `setdefault()` to prevent poisoning the real telegram package during pytest collection
- **`messages.py` variable naming**: Corrected `msg` → `status_msg` references for editing bot's status messages (vs. user's original message)

### Dependencies

- **`yt-dlp`**: `>=2025.1.15` → `>=2026.03.03`
- **`gallery-dl`**: `>=1.27.0` → `>=1.31.0`

---

## [Previous]

### Added

- **TikTok Slideshow (Image Carousel) support**: Two output modes via `gallery-dl` + `ffmpeg`:
  - **📸 Photo Album** — sends images as a Telegram media group (up to 10)
  - **🎬 Video Slideshow** — combines images + audio into MP4
  - Automatic slideshow detection via `detect_tiktok_slideshow()` heuristic
  - Full support in both private chat (interactive keyboard) and group chat (auto photo album)
- **`gallery_dl/service.py`**: New `GalleryDlService` subprocess wrapper for downloading TikTok slideshow images and audio
- **Slideshow callback handler**: `on_slideshow()` in `callbacks.py` with rate limiting, queue management, and cleanup
- **Slideshow keyboard**: `build_slideshow_keyboard()` with Photo / Video / Audio options
- **i18n support**: All ~50 user-facing Russian strings extracted to `app/core/texts.py` (`Texts` class) for easy localization
- **CI/CD pipeline**: `.github/workflows/test.yml` — automated testing with coverage on push/PR
- **Integration tests**: `tests/test_integration.py` — 18 end-to-end flow tests (URL→formats→pick→send, cancel, rate limiting)
- **Shared test fixtures**: `tests/conftest.py` with `mock_state`, `mock_update`, `mock_context` for DRY test setup
- **pytest-cov config**: `pyproject.toml` with coverage settings (source=app, show_missing)
- **`__all__` exports**: Added to 6 modules (`utils.py`, `limiter.py`, `policy.py`, `downloader.py`, `service.py`, `callbacks.py`)
- **IP-based webhook rate limiting** (M8): Prevents replay attacks via `state.limiter.allow_ip()`
- **`parse_mode="HTML"` for group captions** (M4): Enables HTML mentions in group chat file sends
- **TikTok format deduplication** (M5): Deduplicates by filesize instead of height
- **`.dockerignore`**: Excludes `.git`, tests, IDE files, docs from Docker build context
- **Injection prevention tests**: `tests/test_injection_prevention.py` — 15 tests for URL separator, prefix validation, gallery-dl cookies ordering, and stream byte limits

### Changed

- **`list_formats()` return type**: Extended from 4-tuple to 5-tuple with `is_slideshow` flag
- **Dockerfile**: Updated base image from `python:3.11-slim` to `python:3.12-slim`
- **Dockerfile**: Removed unused `curl` package from system dependencies
- **Tech stack**: Python version requirement updated from 3.10+ to 3.12+
- **Test suite**: Expanded from 140 to 232 tests (all passing)
- **Dockerfile**: Added non-root `botuser` for defense-in-depth
- **`conversion_lock`→`Semaphore(3)`**: Allows 3 concurrent CPU-intensive conversions instead of serializing
- **Janitor**: Now also cleans `slideshow_*` directories and `concat_*` temp files
- **PTB error handler**: Registered global `add_error_handler` to log and notify users of unhandled exceptions
- **`MAX_CONCURRENT_TASKS`**: Default increased from 2 to 5
- **`inflight_parsing`**: Replaced plain `dict` with `TTLCache(100, 600)` for auto-expiry of stale entries
- **Photo streaming**: `send_slideshow_photos` now passes file handles instead of buffering entire images in memory
- **8 test files rewritten**: Removed FastAPI/TestClient dependencies by testing logic directly

### Dependencies

- **`python-telegram-bot`**: 20.8 → 22.6 (migrated deprecated API: `quote` → `do_quote`, `disable_web_page_preview` → `LinkPreviewOptions`)
- **`fastapi`**: 0.115.6 → 0.135.1
- **`uvicorn[standard]`**: 0.32.1 → 0.41.0
- **`python-dotenv`**: 1.0.1 → 1.2.2
- **`cachetools`**: 5.5.0 → 7.0.1
- **`yt-dlp`**: Pinned to `>=2025.1.15` (was unpinned)
- **`gallery-dl`**: Added `>=1.27.0` for TikTok slideshow image downloads
- **Removed**: `aiohttp` (unused dependency)

### Fixed

- **8 test collection errors**: Tests importing `app.main` or `fastapi.testclient` now use correct import paths
- **C5 size_allowed**: Documented behavior when filesize is None, added `_MAX_UNKNOWN_SIZE_LIMITS` for future enforcement
- **`send_file` signature**: Added `parse_mode` parameter propagation through all Telegram send methods
- **PTB 22.x compatibility**: Migrated all deprecated API calls (`quote=True` → `do_quote=True`, `disable_web_page_preview` → `LinkPreviewOptions`)

### Security

- **CLI injection prevention** (SEC-1/3): `--` end-of-options separator added to all `yt-dlp` and `gallery-dl` subprocess commands
- **URL prefix validation**: URLs starting with `-` are rejected before being passed to any subprocess
- **Stream byte limit** (SEC-2): HTTP `/dl/{token}` endpoint terminates after exceeding `MAX_DL_MB` to prevent resource exhaustion
- **Non-root Docker user**: Container runs as `botuser` instead of root
- Webhook HMAC authentication using `hmac.compare_digest` (timing-safe)
- Security headers middleware with CSP (strict for API, relaxed for docs)
- Rate limiting per user, chat, IP, and token via `LimiterRegistry`
