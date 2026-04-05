# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- **Native `.gif` File Export** (`giffile|` callback): On-demand two-pass FFmpeg palette pipeline
  (`palettegen` stats_mode=diff → `paletteuse` Bayer dithering, `diff_mode=rectangle`).
  Output is a standards-compliant `GIF89a` binary (magic bytes `47 49 46 38 39 61`) capped
  at 480 px width / 15 fps with Lanczos scaling — compatible with Discord avatars/banners,
  Windows Photo Viewer, Android/iOS galleries, and any tool that validates magic bytes.
  Root cause of the prior limitation: `convert_to_gif_ffmpeg()` produced an MP4 container
  (`ftyp` magic) — a silent video Telegram accepts as animation, but external apps reject
  when saved as `.gif`.
- **`💾 Скачать как .gif файл` inline button**: Attached to every animation message delivered
  by the orchestrator and `on_convert_to_gif`. Token-bound via `file_cache`; UX shows
  spinner `⏳ Готовлю...` → `✅ Отправлен` states.
- **Redis doc_file_id caching** (`gifdoc:{token}`): Subsequent presses of the same button
  bypass conversion entirely and re-send the cached Telegram `file_id` via `sendDocument`.
- **Local API passthrough for GIF documents**: `sendDocument` routed through
  `tg-api-server:8081` when `TELEGRAM_LOCAL_ENDPOINT` is set; supports files up to 2 GB.
  Source MP4 re-fetched from Telegram via `bot.get_file()` when `file_cache` entry has
  expired — zero persistent disk usage.
- **`gif_file_sem = asyncio.Semaphore(2)`** in `state.py`: Dedicated bounded concurrency
  for GIF export jobs, independent from `tasks_sem` and `conversion_sem`.
- **`on_save_as_gif_file()` debounce guard**: `processing_gifs` set prevents double-firing
  from rapid button taps during conversion.

### Fixed

- **Pinterest GIF detection root cause** (`PinterestNativeService`): Video pins that lack
  `og:video` meta tags now have a regex fallback scanning the raw HTML for CDN-hosted
  `.mp4` stream URLs (`cdn.fbcdn.net`, `v1.pinimg.com`), eliminating silent `.jpg`
  thumbnail delivery in place of the actual animated content.
- **Orchestrator GIF routing** (`DownloadOrchestrator`): `GIF_FORMAT_ID` requests for
  Pinterest URLs are now explicitly routed to `PinterestNativeService` instead of falling
  through to `yt-dlp`, which fails with `No video formats found!`.

### Changed

- **`convert_to_gif_ffmpeg()` responsibility narrowed**: Responsible only for stripping
  audio from MP4 for in-Telegram `sendAnimation` delivery. Native `.gif` export is now a
  separate on-demand operation (`convert_to_native_gif()`).

### Quality & Testing

- **5 new unit tests** in `test_converter.py` (`TestConvertToNativeGif`):
  happy path, palettegen failure, empty output guard, missing input, empty/None path.
- **Test suite**: 528 → 533 tests (all passing, exit code 0).
- **Ruff**: 0 errors (auto-fixed 1 unused import in `callbacks.py`).
- **Mypy**: 0 errors across all source files.

---



- **Smart Video Compression**: Implemented mathematically precise, automatic two-pass `libx264` video compression for files exceeding Telegram's 50MB limit. The system probes video duration/audio bitrate and dynamically scales video bitrates to fit seamlessly within a 48.5MB container cap (`_TG_MAX_BYTES`).
- **Local Bot API Server Integration (Zero-Copy)**: Introduced native support for local Telegram Bot API servers. Configuring `TELEGRAM_LOCAL_ENDPOINT` transparently increases the maximum upload limit to 2000 MB (2 GB). All `ffmpeg` compression algorithms are bypassed entirely to preserve CPU, and files are streamed via `file://` absolute URI pointers directly into the Telegram daemon without multipart HTTP overhead.
- **Windows Host FFmpeg Compatibility Fix**: Fixed a critical silent crash in the `compress_video_to_size` function caused by hardcoded `/dev/null` paths in the first-pass encode. Replaced with cross-platform `os.devnull` (handling `NUL` on Windows environments).
- **X (Twitter) Ultra-Fast Download via Cobalt**: Twitter/X URLs are now cleanly intercepted and processed instantaneously through the Cobalt API. This completely bypasses legacy `yt-dlp` constraints, resolving all "429 Too Many Requests" errors and blocked extractions.
- **X Multi-Media Posts Support**: Robust extraction pipeline accurately handles Twitter posts containing multiple media items (mixed videos/photos), downloading and delivering the entire collection sequentially. 
- **Instagram Stories & Highlights**: Download Instagram stories and highlights with an interactive rich selection UX. Users send a profile link and get a menu with `[📸 Stories] [📁 Highlights] [📥 Download All]`. Each story shows date, time, and duration. Requires `IG_SESSION_B64` environment variable for authenticated access.
  - New files: `app/services/instagram.py`, `app/bot/ig_callbacks.py`
  - 8 new callback handlers registered in `main.py`
  - 39 new tests in `test_instagram_service.py` and `test_pinterest_service.py`
- **Instagram Resilient Profile Fetch (Hybrid Fallback)**: Profile metadata is now fetched via a two-tier strategy. The bot first attempts the anonymous `web_profile_info` API (zero session risk). If blocked by Instagram's datacenter IP filter (401), it seamlessly falls back to authenticated Mobile API endpoints (`usernameinfo` + `highlights_tray`) via `curl_cffi`. This eliminates "IP Block" errors without risking session cookies on web endpoints.
- **Instagram Reels/Posts Native Download (Mobile API Fallback)**: When Cobalt public instances return 503 errors, the bot now falls back to a native download pipeline using `i.instagram.com/api/v1/media/{pk}/info/`. Shortcodes are converted to numeric media PKs via a deterministic base64 algorithm (`_shortcode_to_media_pk`). Supports video reels, single photo posts, and carousel detection. Cobalt remains the primary attempt to conserve session trust score.
- **Instagram Anti-Ban Rotation Pool**: The brittle `IG_SESSION_B64` variable has been replaced with `IG_SESSIONS_B64`, supporting a comma-separated list of burner cookies. A round-robin allocator (`_get_available_session`) automatically cycles between accounts.
- **Instagram Hard Rate-Limiting**: Every session inside the new pool is individually monitored by the atomic Lua `RedisTokenBucketLimiter` (`ig_fallback`). This proactively enforces a hard hourly ceiling (15 requests/hr per account) to guarantee accounts are never flagged for bot-like activity.
- **Instagram Unified Fingerprints**: Legacy `Android` endpoints used for fallback auth requests now use standard `Chrome 110` desktop payload headers to match the cookie generator TLS fingerprint, eliminating a mismatch that triggered account freezes.
- **Instagram Session Eviction & CSRF**: Automatic dead session eviction on checkpoint/suspension. Added `X-CSRFToken` header to all authenticated requests with automatic retry on HTTP 400.
- **Instagram Dual-Endpoint Fallback**: Added diagnostic logging on HTTP 400 with a new dual-endpoint fallback strategy (web + mobile API) to maximize extraction success.
- **End-to-End Orchestration**: Implemented new bot callback handlers for format selection, download, and Telegram delivery. Uses a new media conversion service.
- **Observability**: Added fully structured JSON logging and a lightweight internal metrics system across the pipeline.

### Performance & Optimization (Redis)

- **Redis Storage Compression**: Switched `RedisStorage` serialization from `msgspec.json` to `msgspec.msgpack` and added threshold-based `zlib` compression (level 1) for payloads >1KB. This reduces memory footprint by 3-5x, essential for strict 256MB free-tier limits. Included transparent backward-compatible fallback for existing JSON data.
- **Upstash Redis Free-Tier Tuning**: Optimized the `redis-py` connection pool specifically for serverless Upstash constraints (`max_connections=5`, short socket timeouts, disabled `health_check_interval` to conserve 500K cmd/month quota).
- **Rate Limiter Lua Optimization**: Rewrote `RedisTokenBucketLimiter` atomic Lua script from a 3-command HASH approach (`HGETALL`+`HMSET`+`EXPIRE`) to a 2-command STRING approach (`GET`+`SET EX`). Saves ~33% of the monthly command budget per rate-limit check.
- **Redis Key Namespacing**: Introduced strict key prefixing (`lnk:`, `inf:`, `can:`) to isolate distinct cache types in a single logical database.
- **Redis App Lifecycle Hooks**: Bound connection `ping()` to FastAPI startup and cleanly close connection pools (`aclose()`) inside the application `lifespan` event.

### Changed

- **Pinterest Streaming Rewrite**: Replaced full-buffer `resp.content` with chunked async streaming (`aiter_content()`) in `pinterest.py`, eliminating RAM exhaustion on large files. Added `og:image` extraction for static image pins and proper Cobalt `picker` response handling for carousels.

### Dependencies

- **`instaloader`**: Added `>=4.13.0` for Instagram stories/highlights extraction

### Quality & Testing

- **Test suite expanded**: 257 → 528 tests (60% coverage, threshold 60%)
- **Property-based testing**: 14 `hypothesis` properties (~1400 random examples) for parsers, URL detection, sanitization
- **Integration tests**: 4 tests with real `yt-dlp` + `ffmpeg` binaries (`@pytest.mark.integration`)
- **Mutation testing**: `mutmut` configured targeting `parsers.py` (runs in CI)
- **Test consolidation**: Removed 7 redundant/overlapping test files, merged into stronger suites

### Dev Experience & CI

- **GitHub Actions**: Added `integration.yml` workflow (integration + mutation tests on push to main)
- **Docker multi-stage build**: 2-stage Dockerfile (builder → runtime), removes gcc from final image
- **Docker HEALTHCHECK**: Checks `/health` endpoint every 30s
- **Pre-commit hooks**: `.pre-commit-config.yaml` with ruff (lint + format), mypy, file hygiene
- **File hygiene**: Comprehensive `.gitignore` and `.dockerignore` updates

### Core Feature Updates

- **TikTok BVC2 Anomaly Detection**: `TikWMService` now implements a mathematical file-size heuristic to deterministically intercept proprietary ByteDance (`bvc2`/`hevc`) codecs before download. Erroneous "HD" streams are instantly swapped to native H.264 streams, strictly eliminating ffmpeg transcode overhead and Telegram inline playback corruption.
- **TikTok Instant Bypass**: TikTok links are now fully intercepted before `yt-dlp` extraction to eliminate blocking delays. The response is routed directly to `TikWMService` or `gallery_dl`.
- **Dynamic TikTok Caching**: `TikWMService` now parses the internal `expire=` Unix timestamp from delivered TikTok CDN URLs, caching them in-memory for 80% of their actual lifespan to solve strict 1.1s API rate limits on viral videos.
- **Pinterest Native Extraction**: Implemented a blazing-fast (200ms) native open-graph regex parser (`PinterestNativeService`) via `curl_cffi` to extract `og:video` tags, fully bypassing `yt-dlp` Cloudflare 403 Forbidden scenarios. Falls back to Cobalt for photo carousels.
- **TikTok API Extraction (TikWM & GalleryDL)**: Replaced fragile fallback chains (and geo-blocked Cobalt instances) with the robust TikWM API as the primary TikTok fetcher, offering watermark-free video and native image slideshow extraction.
- **Cobalt Deprecation (Optional)**: Cobalt API is now deactivated by default to bypass strict TikTok IP blocking constraints, but can be reactivated locally via `ENABLE_COBALT_TIKTOK`.
- **Yt-Dlp Extraction Bypass**: TikTok links now entirely bypass `yt-dlp` parsing and initialization when third-party APIs fail, instead generating a synthetic metadata object that directs the routing straight to `gallery-dl` fallback pipelines, preventing immediate IP-bans on the host server.
- **YouTube Pipe Mode (Opt-in)**: Optional `YOUTUBE_PIPE_MODE=true` buffers `yt-dlp` output into `BytesIO` memory for direct Telegram upload on videos <50MB, completely omitting temporary disk writes.
- **Dynamic Extractor Metadata (`--load-info-json`)**: Bypassed duplicate network parsing calls by passing previously identified JSON metadata downstream into execution contexts, saving ~2-5s off standard YouTube latency.

### Architecture & Stability (Targeted Refactor)

- **yt-dlp Integration Audit**: Achieved production-grade reliability for the extraction pipeline:
  - **CLI Builder Pattern**: Unified command generation via `YtDlpCLIBuilder`, eliminating scattered argument assembly.
  - **Structured Error Mapping**: Implemented `map_ytdlp_error` with regex heuristics to predictably convert brittle string-based `stderr` outputs into structured exception classes (`AccessDeniedError`, `LiveStreamError`, etc.).
  - **Decoupled Orchestration**: Lifted platform-specific fallback routing out of atomic download services (`VideoDownloader`) into the higher-level `DownloadOrchestrator`.
  - **Robust Subprocess Cancellation**: Hardened `run_subprocess` to support clean graceful SIGTERM signals falling back to SIGKILL, handling zombie processes safely across POSIX and Windows endpoints.
- **Telegram Event Loop**: Bound missing `apislide` and `apigrpslide` regex routing rules to the `main.py` application builder, resolving bugs where TikTok gallery format buttons infinitely hung after clicking in private and group chats alike.
- **Redis Cache Deserialization**: Patched the `apislide` callbacks to correctly instantiate `DownloadContext` from `msgspec` JSON dictionaries, fixing a bug where clicking the gallery format buttons returned "ссылка устарела" (Link Expired) due to failed generic datatyping checks.
- **TikTok API Format Recognition**: Fixed a bug where both the 'Video' and 'Album' buttons would strictly return a video file. This was caused by comparing dynamic API payloads (`photo`) to legacy internal constants (`__slideshow_photos__`).
- **Webhook Concurrency (Critical)**: Replaced blocking `await process_update()` in the `/webhook` endpoint with `asyncio.create_task()` fire-and-forget dispatch. Previously, the endpoint blocked the FastAPI response until the entire download finished, causing Telegram to pause delivering updates to all other users. Combined with `.concurrent_updates(True)` on the Application builder to dispatch handlers in parallel.
- **Semaphore Double-Release (Critical)**: Fixed a bug in `DownloadOrchestrator` where the `tasks_sem` semaphore was released manually on the size-check early-return path AND again in the `finally` block, causing semaphore count drift that gradually allowed unbounded concurrent downloads.
- **Group Slideshow Concurrency Guard**: `on_group_slideshow` handler now acquires `tasks_sem` before starting downloads. Previously, group slideshows bypassed the global concurrency limiter entirely, allowing unbounded parallel slideshow downloads.
- **Persistent State Backend Protocol**: Replaced brittle `TTLCache` in-memory single-point-of-failures with an asynchronous `StateStorage` protocol.
- **Redis Integration**: Implemented `RedisStorage` using `msgspec` for blazing-fast JSON serialization to persist app caches across restarts. This enables zero-downtime deployments. Memory-based fallback implemented solely via `MemoryStorage`.
- **Redis Rate Limiter**: Introduced `RedisTokenBucketLimiter` implementing atomic Lua scripts for accurate, distributed rate limiting, replacing local in-memory token buckets.
- **Async External Binary Isolation**: Completely removed `ThreadPoolExecutor` from `YtDlpService` and re-engineered `list_formats` and `extract` endpoints to spawn decoupled `yt-dlp` instances via `asyncio.create_subprocess_exec()`. This ensures absolute unblocking of the main Telegram event loop.
- **Resilient Process Management**: Implemented `run_subprocess` using OS-level process groups (`os.setsid`/`os.killpg` on POSIX, `CREATE_NEW_PROCESS_GROUP` on Windows) to guarantee orphan `ffmpeg` or `yt-dlp` processes are deterministically killed on timeout or client cancellation.

### Code Quality

- **Mypy strict: 0 errors** in 47 source files (up from 39 files)
  - Type annotations added to `metrics.py`, `routes.py`, `main.py`, `service.py`, `sender.py`, `keyboards.py`, `slideshow.py`, `process.py`, `state.py`
  - `tests/*` scopes excluded from strict Mypy bounds to allow uninhibited mock patching.
  - `pyproject.toml` consolidates all mypy config (deleted standalone `mypy.ini`)
- **Ruff: 0 errors** — auto-fixed 40+ issues (unused imports, formatting)
- **Structured logging**: Converted 40 f-string loggers → `extra={}` format across 9 files for machine-parseable JSON logs
- **Dead code removal**: `vulture` scan clean, deleted 6 redundant files (`mypy.ini`, `reproduce_format_error.py`, `verify_*.py`)

### Documentation

- **README updated**: Test count 257 → 430+, coverage badge, pre-commit setup, integration test docs, Docker multi-stage section

### Added

- **Video metadata in `send_video`**: Passes `duration`, `width`, `height` to Telegram API for faster delivery and proper video preview. Extracts from yt-dlp info JSON (zero-cost) with `ffprobe` fallback
- **`_extract_video_meta()` helper**: Dual-strategy metadata extraction in `callbacks.py`
- **Video thumbnail in format selection**: Shows video preview image (thumbnail) in the format selection message via `send_photo()`. Uses `edit_message_media()` with `InputMediaPhoto` for back-button navigation. Falls back to text-only message when thumbnail is unavailable (e.g. TikTok slideshows). Zero extra requests — thumbnail URL is already in yt-dlp metadata
- **New tests**: `test_video_meta.py` (8 tests), `test_tikwm.py` (7 tests, skipped without `curl_cffi`)

### Changed

- **Typed Use-Case Orchestration**: Extracted overarching download logistics out of `callbacks.py` into a new `DownloadOrchestrator` service to dramatically thin out the Telegram UI presentation layer.
- **Typed State Caching**: Replaced arbitrary, string-indexed dictionaries in `state.link_cache` with a heavily annotated `DownloadContext` dataclass ensuring firm data contracts across web scopes and bot groups.
- **`list_formats()` return type**: Completely refactored from a 7-element Tuple into a strongly-typed `ExtractionResult` Dataclass. This removes leaky abstractions, massive code duplication for TikTok testing, and fragile tuple indexing throughout the UI layer (`messages.py` & `callbacks.py`).
- **Group chat TikTok handling**: Removed duplicated `extract()` execution from `group_logic.py`, eliminating a double-extraction bug, accelerating group chat TikTok processing.
- **Strict Format Binding**: Replaced legacy `height` overriding with deterministic 1:1 `format_id` binding. UI format choices are now exact guarantees, removing implicit `ffmpeg` mismatches.
- **Pre-muxed Format Priority**: `parsers.py` strongly favors single-file muxed video+audio (`vcodec != none`, `acodec != none`). Allows TikTok/Facebook to bypass `ffmpeg` merges completely, increasing download speed.
- **Audio Selectors**: Fallback to `+bestaudio` now occurs directly in parser logic, composing composite `format_id`s cleanly instead of relying on CLI append.
- **TikWM service → async `curl_cffi`**: Rewrote `tikwm.py` from sync `urllib.request` to async `curl_cffi.requests.AsyncSession` with TLS fingerprint impersonation (`impersonate="chrome"`) and 3-retry logic
- **Dockerfile graceful shutdown**: Added `exec` prefix to CMD and `STOPSIGNAL SIGINT` — uvicorn is now PID 1 and receives signals directly (prevents 10s SIGKILL timeout on `docker stop`)
- **Thread-safety invariants**: Documented that all `TTLCache` reads/writes must happen from the event loop thread (cachetools is NOT thread-safe)
- **`sender.py`**: `send_file()` now accepts `duration`, `width`, `height` params → forwarded to `send_video`/`send_animation`/`send_audio`
- **Test suite**: Expanded from 249 to 434 tests (434 passed)

### Fixed

- **Pinterest GIF Extraction**: Fixed a bug where downloading animated GIFs via Pinterest URLs returned corrupted static thumbnails (`.jpg`) instead of the true `.gif` loops. The algorithm now checks `og:image` path hashes and seamlessly upgrades them via deterministic search against the JSON payload to stream the `.gif` format.
- **Prometheus Metric Leak**: Fixed an issue in `downloader.py` where timeout cancellations incorrectly orphaned active downloads incrementally, resulting in unbounded `active_downloads` gauge drifts.
- **Group Command Throttling**: Closed a web-hook concurrency gap in `group_logic.py` where downloads could trigger simultaneously missing standard `state.tasks_sem` isolation bounds.
- **GIF Conversion Deadlock**: Lifted nested asynchronous tracking semaphore locks globally preventing race conditions in `callbacks.py` rendering `FFmpeg` freezes.
- **Redis Deserialisation Crash**: Safe-guarded payload reconstruction in `ExtractionResult` unpacking via explicit typecasting, resolving mapping anomalies on restarts.
- **YouTube Extraction Deadlock (`RuntimeError`)**: Fixed a severe coroutine conflict in `YtDlpService` where `communicate()` was called concurrently with background `stderr` readers, causing all YouTube downloads to fail.
- **Redis Cache Strict Decoding Failure**: Fixed an issue where `RedisStorage` would crash with `msgspec.json.DecodeError` when `type_hint=None` due to expecting a literal `"null"` instead of parsing standard JSON objects. This resolves the downstream UI bug where standard TikTok links wrongly triggered the Slideshow fallback.
- **Cache unpacking bug** (CRITICAL): UI handlers previously raised exceptions during format unpacking after `list_formats` was extended. Safely isolated this behind `ExtractionResult` properties.
- **TikWM Rate Limit Exhaustion**: Fixed an issue where `TikWMService.download_video` redundantly called the TikWM API to resolve direct video links when the UI had already queried them. A 1.1s generic `asyncio.Lock()` queue was also added to enforce their 1 request/second API limitation properly, handling concurrent link requests gracefully.
- **Cross-Platform Test Pathing**: Fixed `gallery_dl` mock assertions arbitrarily failing on Windows environments due to hardcoded `/` posix path separators instead of native `os.path.join()`.

### Performance & Reliability

- **Garbage Collection Optimization**: Janitor tasks now proactively sweep leftover `tikwm_*`, `gdl_video_*`, `slideshow_*`, and trailing `info_*` artifacts securely avoiding TEMP path exhaustion.
- **Deduplication Key Strictness**: Object hashing mapping `id(fmt)` has been refactored heavily using hard `fmt.format_id` binding during `deduplicate_formats()`, improving filter consistency.
- **`--load-info-json` for all platforms**: Removed YouTube-only guard — extraction metadata is now cached as JSON and reused during download for TikTok, VK, Pinterest, Rutube, Facebook. Eliminates double extraction (saves 3–15s per download)
- **Group mode info JSON reuse**: TikTok slideshow detection extraction in group mode now caches info JSON and passes it to the download phase
- **Per-phase timing metrics**: Added `_Histogram` class to `metrics.py` with `time()` context manager. New Prometheus-compatible timers: `extraction_duration_seconds`, `download_duration_seconds`, `conversion_duration_seconds`, `upload_duration_seconds`
- **`-preset veryfast` for slideshow encoding**: `images_to_video()` in `converter.py` now uses `veryfast` preset instead of default `medium` — 2–4× faster encoding
- **Configurable `YTDLP_CONCURRENT_FRAGMENTS`**: New env var (default `8`, up from hardcoded `5`) for DASH/HLS fragment downloads. ~20–40% faster segment-based downloads
- **Info JSON cleanup**: `on_send()` now deletes cached info JSON files after download, preventing `/tmp` disk fill

---

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
- **YouTube download speed**: `--load-info-json` reuses extraction data during download, eliminating double n-challenge solving (~15-20s savings)
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
