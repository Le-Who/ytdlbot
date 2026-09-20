# Media Provider Racing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship one verified media pipeline that prioritizes complete YouTube/Shorts delivery, races equivalent providers safely, reuses Telegram `file_id`, survives restarts, and deploys one immutable release with rollback.

**Architecture:** `app/services/media/` owns request normalization, provider resolution, candidate validation, bounded racing, materialization, and delivery. Existing handlers become compatibility adapters to one `MediaPipeline`; durable SQLite state records accepted updates, job checkpoints, delivery receipts, and deduplication. Local Telegram Bot API is the required production delivery profile and release scripts replace only the bot image after tests and readiness gates.

**Tech Stack:** Python 3.12, asyncio, FastAPI, python-telegram-bot 22.6, curl_cffi, SQLite WAL, Redis cache, yt-dlp/EJS/Deno/PO-token provider, ffmpeg/ffprobe, Docker Compose, GitHub Actions.

**Spec:** `E:/Projects/ytdlbot-1/docs/superpowers/plans/2026-09-15-media-provider-racing.md`

## Global Constraints

- YouTube Shorts and ordinary YouTube video delivery are the primary acceptance criterion; success on other platforms cannot compensate for broken YouTube.
- Production requires Local Bot API and one `MAX_MEDIA_FILE_MB=2000`, where MB means 1,000,000 bytes. Cloud mode is an explicit degraded profile, never a silent 45/50 MB policy.
- Public links use no personal cookies. Authorized Instagram results stay isolated by auth scope.
- At most two cheap external resolver attempts and at most one heavy local extractor run for one request.
- Provider credentials, Telegram tokens, cookies, and local paths never cross provider origins; logs omit signed query strings and cookies.
- Exact mode never silently changes quality, audio format/language, clip interval, watermark policy, or album completeness.
- Candidate search budgets are connect 3 s, provider resolve 8 s, total resolution 20 s; materialization has a separate deadline and 3 s stall timeout.
- Two full concurrent downloads are allowed only when both declared sizes are at most 20 MB. Larger material is transferred once with stall-based failover.
- Unknown Telegram send outcome is recorded as `uncertain` and never retried automatically.
- No paid API, mandatory trial credit, new proxy service, browser farm, self-hosted Cobalt/Invidious/Piped, or new hosting dependency.
- Every behavior change follows RED → GREEN TDD; task reports must include the failing and passing commands.
- Routine deployment changes only the bot service by immutable digest and preserves the current Compose project, volumes, secrets, Redis, and host services.

---

### Task 1: Canonical Media Contract and YouTube Identity

**Files:**
- Create: `app/services/media/__init__.py`
- Create: `app/services/media/models.py`
- Create: `app/services/media/validation.py`
- Create: `tests/media/test_models.py`
- Create: `tests/media/test_validation.py`

**Interfaces:**
- Produces: `MediaKind`, `DeliveryStatus`, `QualityPolicy`, `ClipInterval`, `MediaRequest`, `MediaItem`, `MediaCandidate`, `ResolvedMedia`, `DeliveryTarget`, `DeliveredItem`, `DeliveryReceipt`, `canonicalize_media_url(url)`, and `request_cache_key(request)`.
- `MediaRequest` contains canonical URL, platform, media ID, kind, quality, audio format/language, clip, album selection/order, watermark policy, caller scope, auth scope, exact/fast mode, and absolute monotonic deadline.

- [ ] **Step 1: Write failing contract tests**

```python
def test_youtube_url_forms_share_identity_but_preserve_clip():
    short = MediaRequest.from_url("https://youtube.com/shorts/abc123?t=15")
    watch = MediaRequest.from_url("https://m.youtube.com/watch?v=abc123&utm_source=x")
    assert short.media_id == watch.media_id == "abc123"
    assert short.canonical_url == watch.canonical_url == "https://www.youtube.com/watch?v=abc123"
    assert short.clip == ClipInterval(start_seconds=15, end_seconds=None)
    assert short.cache_key != watch.cache_key

def test_vertical_1080x1920_satisfies_1080_quality():
    candidate = candidate_fixture(width=1080, height=1920)
    assert validate_candidate(request_fixture(quality=QualityPolicy(max_edge=1080)), candidate).usable
```

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/media/test_models.py tests/media/test_validation.py --no-cov -q`
Expected: collection fails because `app.services.media` does not exist.

- [ ] **Step 3: Implement immutable dataclasses/enums and URL normalization**

```python
@dataclass(frozen=True, slots=True)
class ClipInterval:
    start_seconds: float | None = None
    end_seconds: float | None = None

@dataclass(frozen=True, slots=True)
class MediaRequest:
    canonical_url: str
    platform: str
    media_id: str
    kind: MediaKind = MediaKind.VIDEO
    quality: QualityPolicy = field(default_factory=QualityPolicy)
    audio_format: str | None = None
    audio_language: str | None = None
    clip: ClipInterval = field(default_factory=ClipInterval)
    album_selection: tuple[int, ...] = ()
    watermark_allowed: bool = True
    caller_scope: str = "public"
    auth_scope: str = "public"
    exact: bool = True
    deadline: float | None = None
```

Normalize `youtube.com/shorts/{id}`, `youtube.com/watch?v={id}`, `youtu.be/{id}`, and mobile hosts to the watch URL; parse `t`, `start`, and `end` separately; remove tracking parameters; build a versioned cache key from every equivalence field.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/media/test_models.py tests/media/test_validation.py --no-cov -q`
Expected: all pass without warnings.

Commit: `feat(media): define canonical media contracts`

### Task 2: Provider Registry, Circuit Breakers, and Candidate Race

**Files:**
- Create: `app/services/media/registry.py`
- Create: `app/services/media/race.py`
- Create: `tests/media/test_registry.py`
- Create: `tests/media/test_race.py`

**Interfaces:**
- Consumes: Task 1 models and candidate validation.
- Produces: `MediaProvider` protocol, `ProviderRegistry`, `ProviderRoute`, `CircuitBreaker`, `RaceConfig`, and `race_candidates(request, routes, validate)`.

- [ ] **Step 1: Write failing behavioral tests**

```python
async def test_second_valid_provider_wins_without_waiting_for_hung_first():
    result = await race_candidates(request, [provider(delay=15), provider(delay=1, valid=True)], validate)
    assert result.winner.provider == "second"
    assert clock.elapsed < 2

async def test_fast_invalid_candidate_never_beats_slower_equivalent_candidate():
    result = await race_candidates(request, [provider(delay=.1, wrong_audio=True), provider(delay=.2, valid=True)], validate)
    assert result.winner.provider == "second"
```

Also cover two-attempt cap, delayed heavy local fallback at 1.5 s, no wait for disabled routes, `Retry-After`, cancellation cleanup, and circuit states: 3 transient failures/60 s → open 5 min → one half-open probe; auth/config failure remains disabled until registry configuration revision changes.

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/media/test_registry.py tests/media/test_race.py --no-cov -q`
Expected: imports fail for registry and race modules.

- [ ] **Step 3: Implement bounded structured concurrency**

```python
class MediaProvider(Protocol):
    name: str
    backend_family: str
    is_heavy: bool
    def supports(self, request: MediaRequest) -> bool:
        raise NotImplementedError
    async def resolve(self, request: MediaRequest) -> list[MediaCandidate]:
        raise NotImplementedError
```

Use `asyncio.TaskGroup`, monotonic deadlines, explicit task cancellation/awaiting, and no semaphore internals. A provider response is only a candidate until Task 1 validation accepts it.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/media/test_registry.py tests/media/test_race.py --no-cov -q`
Expected: all pass without leaked tasks or warnings.

Commit: `feat(media): race bounded provider candidates`

### Task 3: YouTube-First yt-dlp Provider and Builder Corrections

**Files:**
- Create: `app/services/media/providers/__init__.py`
- Create: `app/services/media/providers/ytdlp.py`
- Modify: `app/services/ytdlp/builders.py`
- Modify: `app/services/ytdlp/service.py`
- Modify: `requirements.txt`
- Modify: `Dockerfile`
- Test: `tests/media/providers/test_ytdlp_provider.py`
- Test: `tests/test_ytdlp_service.py`

**Interfaces:**
- Consumes: `MediaProvider`, `MediaRequest`, Task 1 candidate types.
- Produces: `YtDlpProvider.resolve()` with separate metadata/video/audio/mux states and exact requested variants.

- [ ] **Step 1: Write failing YouTube contract tests**

```python
def test_1080_fallback_keeps_height_and_audio_constraints():
    cmd = builder.build_download_cmd(URL, "137", "out.mp4", height=1080, section="*10-20")
    fmt = cmd[cmd.index("--format") + 1]
    assert "height<=1080" in fmt
    assert "+bestaudio" in fmt
    assert cmd.index("--download-sections") < cmd.index("--")

async def test_youtube_access_denied_promotes_independent_route_without_ios_android_retry():
    local = recording_provider(error=AccessDeniedError("bot check"))
    external = recording_provider(candidate=youtube_candidate(height=1080, has_audio=True))
    resolved = await race_candidates(youtube_request(height=1080), [local, external], validate_candidate)
    assert resolved.winner.provider == external.name
    assert all("player_client=ios,android" not in " ".join(cmd) for cmd in local.commands)
```

Cover 720p/1080p with sound, vertical Shorts, strict MP3, clip forwarding, expired signed URL re-resolution, missing audio rejection, and stream-copy mux when codecs are compatible.

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/media/providers/test_ytdlp_provider.py tests/test_ytdlp_service.py --no-cov -q`
Expected: missing provider and current fallback/argument-order assertions fail.

- [ ] **Step 3: Implement provider and current yt-dlp defaults**

Remove `YOUTUBE_OAUTH2` and the universal `ios,android` retry. Keep PO-provider as configured optional support, Deno >=2.3.0, and matching pinned `yt-dlp[default]`/EJS versions. Use one heavy-extractor slot and the shared process supervisor.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/media/providers/test_ytdlp_provider.py tests/test_ytdlp_service.py tests/test_ytdlp_service_list_formats.py --no-cov -q`
Expected: all pass.

Commit: `feat(media): add youtube-first yt-dlp provider`

### Task 4: HTTP Provider Adapters and Provider-Specific Isolation

**Files:**
- Create: `app/services/media/providers/cobalt.py`
- Create: `app/services/media/providers/tikwm.py`
- Create: `app/services/media/providers/fxtwitter.py`
- Create: `app/services/media/providers/ssstik.py`
- Create: `app/services/media/providers/snapsave.py`
- Modify: `app/services/cobalt.py`
- Modify: `app/services/tikwm.py`
- Test: `tests/media/providers/test_http_providers.py`
- Test: `tests/test_cobalt_service.py`
- Test: `tests/test_tikwm_service.py`

**Interfaces:**
- Consumes: Task 1 contract and Task 2 registry.
- Produces: provider adapters that only resolve candidates; they never send Telegram messages or select user preferences.

- [ ] **Step 1: Write fixture-driven failing tests**

```python
async def test_cobalt_error_on_one_origin_continues_to_next_origin():
    result = await service.process(URL)
    assert result.status == "redirect"
    assert called_origins == ["https://one.example", "https://two.example"]

async def test_credentials_are_not_forwarded_to_different_origin():
    transport = recording_transport(redirect="https://cdn.other.example/video.mp4")
    await CobaltProvider(endpoint_with_api_key="https://api.one.example").resolve_with(transport, request)
    assert transport.requests[0].headers["Authorization"] == "Api-Key secret"
    assert "Authorization" not in transport.requests[1].headers
```

For every adapter cover success, 429/`Retry-After`, timeout, HTML instead of JSON, challenge page, expired CDN URL, video/photo/animation/ordered album, and lower-quality metadata. FxTwitter uses `/2/status/{id}`. SSSTik extracts the live session token from a fixture rather than a fixed regex. SnapSave stays disabled unless the upstream-ready-link fixture passes; upstream-render jobs are represented distinctly. Remove TikWM's `hd_size < size` codec claim.

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/media/providers/test_http_providers.py tests/test_cobalt_service.py tests/test_tikwm_service.py --no-cov -q`
Expected: new adapters missing and Cobalt/TikWM regressions fail.

- [ ] **Step 3: Implement adapters with per-origin headers, rate limits, and capability flags**

```python
@dataclass(frozen=True, slots=True)
class ProviderEndpoint:
    origin: str
    api_key: str | None
    capabilities: frozenset[str]
    enabled: bool
```

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/media/providers/test_http_providers.py tests/test_cobalt_service.py tests/test_tikwm_service.py --no-cov -q`
Expected: all pass.

Commit: `feat(media): add isolated HTTP provider adapters`

### Task 5: Safe URL Validation and Streaming Materialization

**Files:**
- Create: `app/services/media/transport.py`
- Create: `app/core/resource_budget.py`
- Modify: `app/services/cobalt.py`
- Modify: `app/services/tikwm.py`
- Test: `tests/media/test_transport.py`
- Test: `tests/core/test_resource_budget.py`

**Interfaces:**
- Consumes: validated `MediaCandidate` items.
- Produces: `URLPolicy`, `DiskReservation`, `MaterializedItem`, and `MediaTransport.materialize()`.

- [ ] **Step 1: Write failing transport tests**

```python
async def test_redirect_to_private_ip_is_rejected_before_body_read():
    with pytest.raises(UnsafeMediaURL):
        await transport.probe("https://public.example/redirect-private")

async def test_stream_aborts_at_decimal_2000_mb_limit_without_buffering_body():
    source = chunk_source(chunk_size=1_000_000, chunk_count=2001)
    with pytest.raises(MediaSizeExceeded):
        await transport.stream_to_file(source, max_bytes=2_000_000_000)
    assert source.largest_read == 1_000_000
    assert not output_path.exists()
```

Cover original/redirect/nested URLs, DNS rebinding checks, cross-origin credential stripping, Range signature probe, declared and actual byte caps, disk reservation, active lease protection, 1.5 s first-byte and 3 s stall failover, 20 MB dual-download rule, partial cleanup, and redacted URLs in logs.

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/media/test_transport.py tests/core/test_resource_budget.py --no-cov -q`
Expected: new modules missing.

- [ ] **Step 3: Implement chunked streaming to `/srv/ytdlbot/media`**

Never use whole-response `.content` for media. Reserve declared bytes when known and enforce the byte counter while streaming when unknown.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/media/test_transport.py tests/core/test_resource_budget.py --no-cov -q`
Expected: all pass with bounded buffers.

Commit: `feat(media): stream and validate remote media safely`

### Task 6: Telegram file_id Cache and Cancellation-Safe Singleflight

**Files:**
- Create: `app/core/media_cache.py`
- Create: `app/services/media/singleflight.py`
- Test: `tests/core/test_media_cache.py`
- Test: `tests/media/test_singleflight.py`

**Interfaces:**
- Consumes: Task 1 cache key and receipts.
- Produces: `MediaCache`, `CachedDelivery`, `SingleFlightGroup`, Redis owner-token leases, and memory fallback.

- [ ] **Step 1: Write failing cache/singleflight tests**

```python
async def test_equivalent_public_request_hits_file_id_before_resolver():
    receipt = await pipeline.deliver(equivalent_request, target)
    assert receipt.items[0].file_id == "cached-file-id"
    resolver.resolve.assert_not_awaited()

async def test_cancelling_one_subscriber_keeps_shared_work_for_other_subscriber():
    first = asyncio.create_task(group.do("same", materialize))
    second = asyncio.create_task(group.do("same", materialize))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second == "media-path"
    assert materialize.await_count == 1
```

Cover key isolation for clip/quality/kind/audio/auth/album order/transform version, 30-day sliding TTL, invalid file_id eviction, resolve/materialize singleflight, safe Redis lease release by owner token, and no slot leaks.

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/core/test_media_cache.py tests/media/test_singleflight.py --no-cov -q`
Expected: modules missing.

- [ ] **Step 3: Implement cache layers and shared-task subscription**

```python
async def release(self, key: str, owner: str) -> bool:
    return bool(await redis.eval(COMPARE_AND_DELETE_LUA, 1, key, owner))
```

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/core/test_media_cache.py tests/media/test_singleflight.py --no-cov -q`
Expected: all pass.

Commit: `feat(media): cache file ids and deduplicate work`

### Task 7: Delivery Receipts and Required Local Bot API Profile

**Files:**
- Create: `app/services/media/delivery.py`
- Modify: `app/services/sender.py`
- Modify: `app/core/config.py`
- Modify: `app/core/policy.py`
- Modify: `app/main.py`
- Test: `tests/media/test_delivery.py`
- Test: `tests/test_sender_service.py`
- Test: `tests/test_main_utils.py`

**Interfaces:**
- Consumes: `DeliveryTarget`, materialized media, cached file IDs.
- Produces: `TelegramDelivery.deliver() -> DeliveryReceipt`; compatibility facade returns receipts while legacy callers can use `receipt.success`.

- [ ] **Step 1: Write failing delivery-profile tests**

```python
async def test_local_path_delivery_returns_video_file_id_and_message_id():
    receipt = await delivery.send(item, target)
    assert receipt.status is DeliveryStatus.SUCCESS
    assert receipt.items[0].telegram_type is MediaKind.VIDEO
    assert receipt.items[0].file_id == "video-file-id"

async def test_network_disconnect_after_request_is_uncertain_and_not_retried():
    bot.send_video.side_effect = NetworkError("connection closed after upload")
    receipt = await delivery.send(item, target)
    assert receipt.status is DeliveryStatus.UNCERTAIN
    assert bot.send_video.await_count == 1
```

Cover video/audio/photo/animation/document types, single photo vs groups of 2–10, partial album receipts and retry-only-known-failed items, Local API `file://` under shared path, bounded-memory multipart fallback, configurable media write/read timeouts, invalid file_id eviction, cloud degraded rejection for oversized items, and exact 2,000,000,000-byte boundary logic.

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/media/test_delivery.py tests/test_sender_service.py tests/test_main_utils.py --no-cov -q`
Expected: receipt/profile assertions fail.

- [ ] **Step 3: Implement receipt-returning sender and PTB Local API configuration**

Set `.base_url(endpoint + "/bot")`, `.base_file_url(endpoint + "/file/bot")`, `.local_mode(True)`, and media timeouts. Validate legacy `MAX_DL_MB`/`MAX_TG_UPLOAD_MB` only as an explicit migration to `MAX_MEDIA_FILE_MB`; conflicting values fail startup.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/media/test_delivery.py tests/test_sender_service.py tests/test_main_utils.py --no-cov -q`
Expected: all pass.

Commit: `feat(media): deliver receipts through local bot api`

### Task 8: Unified MediaPipeline and Entrypoint Migration

**Files:**
- Create: `app/services/media/pipeline.py`
- Modify: `app/services/orchestrator.py`
- Modify: `app/bot/messages.py`
- Modify: `app/bot/group_logic.py`
- Modify: `app/bot/commands.py`
- Modify: `app/bot/callbacks.py`
- Modify: `app/bot/ig_callbacks.py`
- Modify: `app/api/routes.py`
- Test: `tests/media/test_pipeline.py`
- Test: `tests/test_pipeline_entrypoints.py`

**Interfaces:**
- Consumes: Tasks 1–7 components.
- Produces: one `MediaPipeline.resolve()` and `MediaPipeline.deliver()` used by messages, groups, commands, callbacks, Instagram callbacks, and HTTP download endpoints.

- [ ] **Step 1: Write failing end-to-end unit tests**

```python
async def test_cache_miss_races_resolvers_then_delivers_one_valid_winner():
    receipt = await pipeline.deliver(request, target)
    assert receipt.success
    assert provider_wins == {"independent": 1}
    assert loser.cancelled

@pytest.mark.parametrize("entrypoint", ["private", "group", "mp3", "mp4", "callback", "api"])
async def test_every_entrypoint_builds_the_same_media_request(entrypoint):
    captured = await invoke_entrypoint(entrypoint, url=YOUTUBE_URL, quality="1080", clip="10-20")
    assert captured.canonical_url == "https://www.youtube.com/watch?v=abc123"
    assert captured.quality.max_edge == 1080
    assert captured.clip == ClipInterval(10, 20)
```

Cover fast/exact modes, MP3/clips across routes, no hidden fallback, mixed albums preserving order, no default slideshow conversion, one editable status message, and the pre-existing TikTok cached-assignment error path.

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/media/test_pipeline.py tests/test_pipeline_entrypoints.py --no-cov -q`
Expected: pipeline absent and entrypoints diverge.

- [ ] **Step 3: Implement pipeline and thin compatibility adapters**

```python
class MediaPipeline:
    async def resolve(self, request: MediaRequest) -> ResolvedMedia:
        raise NotImplementedError
    async def deliver(self, request: MediaRequest, target: DeliveryTarget) -> DeliveryReceipt:
        raise NotImplementedError
```

Do not create a parallel pipeline under `app/core`.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/media/test_pipeline.py tests/test_pipeline_entrypoints.py tests/test_bot_messages.py tests/test_group_logic.py --no-cov -q`
Expected: all pass.

Commit: `refactor(media): route all entrypoints through pipeline`

### Task 9: Queue and Process Cancellation Correctness

**Files:**
- Modify: `app/core/download_queue.py`
- Modify: `app/core/process.py`
- Modify: `app/services/gallery_dl/service.py`
- Modify: `app/services/converter.py`
- Modify: `app/services/orchestrator.py`
- Test: `tests/core/test_download_queue_cancellation.py`
- Test: `tests/core/test_process_supervisor.py`
- Test: `tests/test_gallery_dl_service.py`
- Test: `tests/test_converter_service.py`

**Interfaces:**
- Produces: cancellation-safe `DownloadQueue.acquire()` lease/context manager and one process supervisor for yt-dlp, gallery-dl, ffmpeg, and ffprobe.

- [ ] **Step 1: Write failing cancellation tests**

```python
async def test_cancelled_waiter_removed_before_grant_and_slot_is_reusable():
    waiter.cancel()
    active.release()
    async with queue.acquire(noop_ui):
        pass
    assert queue.queue_depth == 0

async def test_request_cancel_leaves_no_process_socket_or_partial_after_two_seconds():
    task = asyncio.create_task(supervisor.run(long_running_command, owner="req-1"))
    await supervisor.cancel_owner("req-1")
    await asyncio.wait_for(task, timeout=2)
    assert supervisor.processes_for("req-1") == ()
    assert list(media_dir.glob("*.part")) == []
```

Also reproduce timeout/grant/cancel races repeatedly without reading `Semaphore._value`; verify gallery-dl uses async supervised subprocess; change WebM+Opus rename to ffmpeg `-c:a copy`; strict MP3 creates MP3.

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/core/test_download_queue_cancellation.py tests/core/test_process_supervisor.py tests/test_gallery_dl_service.py tests/test_converter_service.py --no-cov -q`
Expected: cancelled waiter or process cleanup assertions fail.

- [ ] **Step 3: Implement atomic lease ownership and supervised subprocess groups**

All acquire paths return a lease whose `release()` is idempotent. A grant to an already-cancelled waiter is returned immediately to the next waiter or semaphore.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/core/test_download_queue_cancellation.py tests/core/test_process_supervisor.py tests/test_gallery_dl_service.py tests/test_converter_service.py --no-cov -q`
Expected: all pass without orphan warnings.

Commit: `fix(media): make queues and processes cancellation safe`

### Task 10: Durable Inbox, Job Store, Delivery Dedup, and Drain

**Files:**
- Create: `app/core/job_store.py`
- Create: `app/core/drain.py`
- Modify: `app/api/routes.py`
- Modify: `app/main.py`
- Test: `tests/core/test_job_store.py`
- Test: `tests/core/test_drain.py`
- Test: `tests/test_webhook_durability.py`

**Interfaces:**
- Produces: SQLite WAL `JobStore`, inbox states `accepted/running/checkpointed/completed/failed`, delivery outcomes `success/failed/uncertain`, `DrainController`, and one worker claim loop.

- [ ] **Step 1: Write failing durability tests**

```python
async def test_webhook_returns_success_only_after_transactional_insert():
    response = await client.post("/webhook", json=UPDATE, headers=AUTH)
    assert response.status_code == 200
    assert await store.get_update(UPDATE["update_id"])

async def test_sigkill_recovery_does_not_repeat_completed_or_uncertain_delivery():
    await store.record_delivery("job-complete", "success")
    await store.record_delivery("job-uncertain", "uncertain")
    recovered = await reopened_store.claim_recoverable_jobs()
    assert {job.id for job in recovered}.isdisjoint({"job-complete", "job-uncertain"})
```

Cover update_id dedup, retryable response on write failure, minimum payload retention, single owner, recovery, drain acceptance without starting heavy jobs, deadline checkpoint/cancel, no webhook deletion during normal deploy shutdown, and backwards-compatible schema migrations.

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/core/test_job_store.py tests/core/test_drain.py tests/test_webhook_durability.py --no-cov -q`
Expected: job store absent and current webhook acknowledges before persistence.

- [ ] **Step 3: Implement transactional store and lifecycle**

Use `BEGIN IMMEDIATE`, WAL, busy timeout, explicit schema version, and a project-scoped `/srv/ytdlbot/state/jobs.sqlite3`. Redis remains optional cache only.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/core/test_job_store.py tests/core/test_drain.py tests/test_webhook_durability.py --no-cov -q`
Expected: all pass.

Commit: `feat(jobs): persist webhook work across releases`

### Task 11: Health, Metrics, Compose Storage, and Versioned Runtime

**Files:**
- Modify: `app/api/routes.py`
- Modify: `app/core/metrics.py`
- Modify: `app/core/config.py`
- Modify: `docker-compose.yml`
- Modify: `Dockerfile`
- Modify: `requirements.txt`
- Modify: `.env.example`
- Test: `tests/test_health_readiness.py`
- Test: `tests/test_metrics.py`
- Test: `tests/test_compose_contract.py`

**Interfaces:**
- Produces: `/health/live`, `/health/ready`, `APP_RELEASE`, active delivery profile/limit, durable-store state, Local API probe state, pipeline latency/success/cache/wasted-byte/retry/provider/transcode/queue/orphan metrics.

- [ ] **Step 1: Write failing health and Compose contract tests**

```python
def test_ready_reports_release_limit_store_and_required_local_api(client):
    body = client.get("/health/ready").json()
    assert body["release"] == "sha-test"
    assert body["max_media_file_mb"] == 2000
    assert body["local_bot_api"]["required"] is True

def test_compose_mounts_same_absolute_media_path_rw_and_ro():
    compose = load_compose("docker-compose.yml")
    assert "media:/srv/ytdlbot/media" in compose["services"]["bot"]["volumes"]
    assert "media:/srv/ytdlbot/media:ro" in compose["services"]["tg-api"]["volumes"]
```

Cover bot/tg-api `/srv/ytdlbot/media` rw/ro, state volume, persistent Telegram session and Redis volumes, pinned images/dependencies, no runtime yt-dlp update, and readiness remaining healthy when an optional provider is down.

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/test_health_readiness.py tests/test_metrics.py tests/test_compose_contract.py --no-cov -q`
Expected: endpoints/fields/mounts absent.

- [ ] **Step 3: Implement health probes, metrics, and deployment mounts**

The Local API startup probe performs an authenticated functional call without logging the token. Readiness is false when the durable store or required Local API is unavailable.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/test_health_readiness.py tests/test_metrics.py tests/test_compose_contract.py --no-cov -q`
Expected: all pass.

Commit: `feat(ops): expose release readiness and media health`

### Task 12: Immutable Scoped Deploy and Rollback

**Files:**
- Create: `scripts/deploy-release.sh`
- Create: `scripts/preflight-production.sh`
- Create: `scripts/rollback-release.sh`
- Create: `tests/deploy/test_release_scripts.py`
- Modify: `.github/workflows/test.yml`
- Modify: `.github/workflows/deploy.yml`

**Interfaces:**
- Produces: CI gate for the exact SHA, digest release manifest, project-scoped `flock`, stale-SHA skip, bot-only activation, readiness/version/webhook checks, and previous bot image/config rollback.

- [ ] **Step 1: Write failing script behavior tests using fake docker/curl/flock commands**

```python
def test_routine_deploy_only_replaces_bot(fake_host):
    result = fake_host.run_deploy()
    assert result.commands == [
        "docker pull ghcr.io/example/ytdlbot@sha256:digest",
        "docker compose -p ytdlbot up -d --no-deps --no-build bot",
    ]

def test_readiness_failure_restores_previous_manifest_and_exits_nonzero(fake_host):
    fake_host.readiness_status = 503
    result = fake_host.run_deploy()
    assert result.returncode != 0
    assert fake_host.active_manifest == fake_host.previous_manifest
    assert "docker compose -p ytdlbot up -d --no-deps --no-build bot" in result.commands
```

Cover stale SHA, two concurrent deploys, interrupted activation, disk shortage, preservation of secrets/volumes/project name, allowlisted release files, SSH known-host validation, and absence of `down`, `--remove-orphans`, host-service restart, Caddy/Portainer mutation, or global prune.

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/deploy/test_release_scripts.py --no-cov -q`
Expected: scripts absent/current workflow violates scoped behavior.

- [ ] **Step 3: Implement shell scripts and workflow gates**

The workflow runs Python 3.12 tests, regression/contract tests, `docker compose config`, `bash -n`, builds once, pushes SHA tag, resolves digest, rechecks branch HEAD, then invokes the same release script documented for manual use. `concurrency.cancel-in-progress` is false.

- [ ] **Step 4: Verify GREEN and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/deploy/test_release_scripts.py --no-cov -q`
Run: `bash -n scripts/deploy-release.sh scripts/preflight-production.sh scripts/rollback-release.sh`
Expected: all pass.

Commit: `feat(deploy): activate immutable scoped releases`

### Task 13: Documentation and Offline Acceptance Harness

**Files:**
- Create: `docs/deployment.md`
- Create: `docs/acceptance.md`
- Create: `tests/acceptance/test_media_release.py`
- Modify: `README.md`
- Modify: `.github/workflows/integration.yml`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: reproducible offline regression suite, manual production/VPS checklist, rollback procedure, 12 Shorts + 12 ordinary-video manifest format, and capability documentation that distinguishes verified from configured providers.

- [ ] **Step 1: Add executable acceptance tests**

```python
def test_youtube_acceptance_manifest_requires_12_shorts_and_12_videos():
    manifest = load_manifest("tests/fixtures/youtube-acceptance.json")
    assert len(manifest["shorts"]) == 12
    assert len(manifest["videos"]) == 12
```

Offline tests cover contract fixtures, provider race, cache bypass, cancellation, Local API boundaries, durable recovery, and deploy rollback. Live tests remain opt-in and record p50/p95, full-delivery rate, 403/429 causes, bytes, CPU, independent-route success, cold/warm cache, and three time windows from the production IP.

- [ ] **Step 2: Verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/acceptance/test_media_release.py --no-cov -q`
Expected: acceptance artifacts absent.

- [ ] **Step 3: Document actual behavior and first-release procedure**

Remove zero-CPU/zero-disk universal claims, unverified quotas, and obsolete diagrams. Document bootstrap-only volume migration, Local API degradation behavior, durable job recovery, manual release/rollback, and the fact that zero downtime is not promised for one bot instance.

- [ ] **Step 4: Run complete local verification**

Run: `.venv/Scripts/python.exe -m pytest tests -q`
Run: `.venv/Scripts/python.exe -m ruff check app tests`
Run: `.venv/Scripts/python.exe -m mypy app`
Run: `bash -n scripts/deploy-release.sh scripts/preflight-production.sh scripts/rollback-release.sh`
Run when Docker is available: `docker compose config`
Expected: all available local gates pass with no unexpected warnings.

- [ ] **Step 5: Commit**

Commit: `docs: document media release and acceptance`

### Task 14: Controlled Production Evidence and One Release

**Files:**
- Create after authorized run: `artifacts/media-release/<sha>/baseline.json`
- Create after authorized run: `artifacts/media-release/<sha>/candidate.json`
- Create after authorized run: `artifacts/media-release/<sha>/release-report.md`

**Interfaces:**
- Consumes: access to the current production VPS, the approved public-link manifest, GitHub environment protection, and release digest.
- Produces: evidence for every live acceptance criterion and one release/rollback record.

- [ ] **Step 1: Collect baseline before activation**

Run the 24-link YouTube set and supported-platform photo/video/album samples in three time windows with cold/warm cache. Record p50/p95 resolve/first-byte/materialize/deliver, success, RSS, CPU, bytes, 403/429, and provider results without secrets.

- [ ] **Step 2: Run isolated Compose deployment and migration scenarios**

Verify successful replacement, readiness rollback, stale SHA, concurrent run, interrupted SSH, no disk, secret/config/volume preservation, SIGTERM/SIGKILL inbox recovery, uncertain-send behavior, bootstrap media/state mount migration, and no neighboring-service restart.

- [ ] **Step 3: Activate one approved digest**

Invoke `scripts/deploy-release.sh` through the protected workflow. Verify local and public readiness, matching `APP_RELEASE`, Local Bot API profile, `getWebhookInfo`, job recovery, and current/previous manifests.

- [ ] **Step 4: Compare and record acceptance**

Require cold p95 on platforms with two working providers to improve at least 25%, success not to regress, CPU/RAM to stay within current limits, and no race duplicates. A failed criterion triggers the documented image/config rollback; SQLite/Redis data is never rolled back.

- [ ] **Step 5: Commit only redacted evidence**

Commit: `docs: record media release verification`
