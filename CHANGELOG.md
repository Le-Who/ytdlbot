# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- **i18n support**: All ~50 user-facing Russian strings extracted to `app/core/texts.py` (`Texts` class) for easy localization
- **CI/CD pipeline**: `.github/workflows/test.yml` — automated testing with coverage on push/PR
- **Integration tests**: `tests/test_integration.py` — 18 end-to-end flow tests (URL→formats→pick→send, cancel, rate limiting)
- **Shared test fixtures**: `tests/conftest.py` with `mock_state`, `mock_update`, `mock_context` for DRY test setup
- **pytest-cov config**: `pyproject.toml` with coverage settings (source=app, show_missing)
- **`__all__` exports**: Added to 6 modules (`utils.py`, `limiter.py`, `policy.py`, `downloader.py`, `service.py`, `callbacks.py`)
- **IP-based webhook rate limiting** (M8): Prevents replay attacks via `state.limiter.allow_ip()`
- **`parse_mode="HTML"` for group captions** (M4): Enables HTML mentions in group chat file sends
- **TikTok format deduplication** (M5): Deduplicates by filesize instead of height

### Changed

- **Dockerfile**: Updated base image from `python:3.11-slim` to `python:3.12-slim`
- **Tech stack**: Python version requirement updated from 3.10+ to 3.12+
- **Test suite**: Expanded from 140 to 193 tests (all passing)
- **8 test files rewritten**: Removed FastAPI/TestClient dependencies by testing logic directly

### Fixed

- **8 test collection errors**: Tests importing `app.main` or `fastapi.testclient` now use correct import paths
- **C5 size_allowed**: Documented behavior when filesize is None, added `_MAX_UNKNOWN_SIZE_LIMITS` for future enforcement
- **`send_file` signature**: Added `parse_mode` parameter propagation through all Telegram send methods

### Security

- Webhook HMAC authentication using `hmac.compare_digest` (timing-safe)
- Security headers middleware with CSP (strict for API, relaxed for docs)
- Rate limiting per user, chat, IP, and token via `LimiterRegistry`
