# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

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

### Changed

- **`list_formats()` return type**: Extended from 4-tuple to 5-tuple with `is_slideshow` flag
- **Dockerfile**: Updated base image from `python:3.11-slim` to `python:3.12-slim`
- **Dockerfile**: Removed unused `curl` package from system dependencies
- **Tech stack**: Python version requirement updated from 3.10+ to 3.12+
- **Test suite**: Expanded from 140 to 217 tests (all passing)
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

- Webhook HMAC authentication using `hmac.compare_digest` (timing-safe)
- Security headers middleware with CSP (strict for API, relaxed for docs)
- Rate limiting per user, chat, IP, and token via `LimiterRegistry`
