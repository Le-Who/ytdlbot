# State-machine audit, November 13, 2026

The audit started from clean commit `1a66071` on `vps`. It covers application
lifecycles in `app/`, their existing callers, and offline failure/cancellation
contracts. Changes remain local. Three GPT-6.1 Sol workers ran alongside the
coordinator: durable/runtime and media workers used xhigh reasoning; bot/dialogue
used high. Follow-up reviews were independent same-family reviews.

The recurring root cause was a transition losing its owner or identity across
an await: a shared conversation replaced a request, an old attempt completed in
a newer circuit, a reservation was reopened before its outcome was known, or
cleanup was marked complete before owned work settled. The fixes put identity,
admission, publication, and cleanup at the boundary that owns each transition.
Existing local machines with clear contracts were retained.

## Reproduced defects and corrections

| Boundary | Reproduced path and root cause | Correction and evidence |
| --- | --- | --- |
| Delivery reservation | Two sends both read no receipt; unfinalized uncertainty was reopenable, so both sent. Worker takeover could reopen it too. | Atomic reservation refuses every success/uncertain row; failed rows remain retryable. `tests/core/test_job_store.py`, `tests/test_webhook_durability.py`. |
| Lifespan exit | Cancel while `bot.stop` waits; remaining cleanup and owner retirement were abandoned. | Own the complete shutdown and retirement transition through cancellation. `tests/test_runtime_configuration.py`. |
| HTTP disk reader | Cancel response while its thread reads; file/lease closed before the thread returned. | Join the disk read before closing the source and releasing materialization. `tests/test_download_stream_lifecycle.py`. |
| Process workload grant | Acquire semaphore, block on registration lock, cancel; the slot had no registered owner and never returned. | Roll back the unregistered grant. `tests/core/test_process_supervisor.py`. |
| Process teardown | First finish marks finished before tree closes; second finish returns immediately. Repeated cancellation could abandon outer cleanup. | One shared finish task per handle; finish and cancel join owned work before returning. The lifecycle test also checks slot reuse. |
| Process spawn | Cancel during OS spawn; a child can arrive after the caller exits. | Join spawn, then kill/close the late child and remove declared/derived partial paths. The late-child regression checks process exit, artifacts, and slot reuse. |
| Queue UI | A stalled position call escaped the queue wait deadline; reposition tasks could write after grant, while a stalled timeout notification kept the caller pending. | One owned notifier per waiter, coalesced positions, cancel/join at grant or withdrawal, bounded optional notifications outside the queue lock. `tests/core/test_download_queue_cancellation.py`. |
| Queue lease entry | Two concurrent awaits on one new lease queued two grants but shared one release task. Repeated legacy enqueue could replace a task's existing lease. | Explicit new/acquiring/acquired/released states reject concurrent entry and duplicate legacy ownership. Behavioral tests verify that capacity remains reusable; existing consumers do not nest enqueue. |
| File-cache ownership | Replace/delete skipped cleanup; removing an alias could delete a still-owned source. | Centralize removal notifications after mutation and final-owner checks, including insertion at capacity. `tests/test_cache.py`. |
| Request menus | Parse A, parse B, press A; tokenless picker read B's shared conversation. Back refreshed only part of the metadata. | New controls carry an expiring user/chat-bound request snapshot; refresh replaces title, extraction path, maps, and quality together. Foreign/expired snapshots fail closed. `tests/test_menu_request_binding.py`. |
| Callback overlap | Two callback updates for the same visible button both entered media processing. | Claim the operation before its first await; release in finally for success, error, or cancellation. Later reuse remains allowed. `tests/test_callback_overlap.py`. |
| Preferences | Concurrent format/quality read-modify-write lost a field; a pending write resurrected a later reset. Memory reads exposed mutable stored dictionaries. | Per-user serialization and detached reads. `tests/test_user_prefs_concurrency.py`. |
| Circuit breaker | A resolve admitted while closed completed after a newer half-open probe began, clearing or replacing its state. | Generation-bound permits for every production attempt and outcome. `tests/media/test_circuit_lifecycle.py`. |
| Lease heartbeat | Repeated cancellation interrupted the gather that joined delivery cleanup. | Join renewal and delivery work while preserving cancellation before releasing materialization. `tests/media/test_pipeline_lease_lifecycle.py`. |
| Cobalt origins | A transient origin followed by auth/config failure disabled the entire route because only the final error survived. Item-specific rejection could also be masked. | Aggregate viable alternatives and maximum valid Retry-After; disable only when failures warrant provider-wide disable. `tests/media/providers/test_http_providers.py`. |
| Legacy progress | Stderr spawned UI tasks without an owner, so progress could continue after return/cancellation. | Track, cancel, and join progress observers. `tests/test_service_lifecycle_regressions.py`. |
| Split outputs | Part cleanup was registered after its send; cancel during an early send leaked current/later parts. | Register all returned parts before sending and join cleanup through cancellation. Same service lifecycle suite. |
| Gallery directories | Missing tool, nonzero process, or empty successful extraction returned without transferring or cleaning the directory. | Retain directory ownership until a successful result transfers it; otherwise await cleanup. Eight failure-path cases in the service lifecycle suite. |
| Instagram sessions | A session became dead while the global limiter awaited, then was still selected. | Recheck session liveness after limiter admission. Same service lifecycle suite. |

Core, durable, media, and bot regressions were observed failing before their
corresponding fixes. Additional controls cover unaffected error ordering,
compatibility, ownership transfer, cancellation, and permitted reuse. Follow-up
review identified the late-spawn artifact and queue notifier gaps; these were
added to the fixes and regression coverage.

## Lifecycle inventory

The inventory groups related machines by their invariant. A retained boundary
means source and existing behavioral evidence were examined without requiring a
new abstraction. This is a code/offline audit, not a formal exhaustiveness proof.

| Machine | States and transitions | Invariant and evidence |
| --- | --- | --- |
| Job schema | Uninitialized -> migrate/initialize -> ready or closed failure | Versioned schema; failed connection closes. Job-store transaction suites. |
| Webhook admission | Authenticate/limit -> persist -> accepted/deduplicated -> ACK, or retryable error | Never ACK before commit. Webhook durability/security suites. |
| Job execution | Accepted/checkpointed/expired running -> live-owned running -> completed/failed/checkpointed | Owned worker transitions require live worker/job leases; compatibility transitions retain their optional-owner contract. Job-store/drain suites. |
| Worker lease | Absent/expired -> acquire -> renew -> release/takeover | Stale owner cannot mutate successor state; pending SQLite ownership work settles. Job-store/drain suites. |
| Item receipts | Absent/failed -> reserved uncertain -> final success/failed/uncertain | Success and all uncertainty prevent replay, including albums. Delivery/webhook suites and new reservations. |
| Polling retry reservation | Absent/final failed -> pending uncertain -> final receipt | One winner; restart does not replay uncertain sends. Job-store tests. |
| Retry predecessor | Failed/checkpointed -> successor reservation -> superseded completed | Reject cycles, stale ownership, and unsafe prior delivery. Job-store/retry tests. |
| Drain | Accepting -> grace -> cancellation -> bounded cleanup -> settled | Join before checkpoint; resistant live handler stays tracked. Drain tests. |
| Worker lifecycle | Fresh -> startup -> active -> failure/release/backoff/reacquire -> stopping -> stopped/deferred | Shared startup/stop and continued ownership of deferred handlers. Drain tests. |
| Lifespan | Unowned -> startup -> active -> draining -> ordinary/deferred shutdown -> unowned | No overlapping owner; cleanup settles before cancellation returns. Runtime/readiness suites. |
| Readiness | Concurrent dependency probes -> success/error/timeout/cancel -> settled siblings | Caller cancellation does not cancel shared Local Bot API probes. Readiness lifecycle suite. |
| Payload retention | Retained terminal job -> minimal update-ID tombstone | Keep active/checkpointed jobs and retryable failed payloads. Job-store tests. |
| Delivery units | Prepare/cache lookup -> reserve -> single/album send -> structured receipt -> durable finalization | Known invalid file ID permits fallback; uncertainty does not. Delivery suite. |
| HTTP relay | Admission -> materialization/renewal or supervised fallback -> stream -> cleanup | Response owns media even before first body chunk; disk reads settle before close. Stream lifecycle suite. |
| Request identity | Raw URL/context -> canonical request or rejection | Kind, quality, clip, auth, language, and item order survive every consumer. Models/validation/pipeline suites. |
| Provider eligibility | Configuration + capability + circuit state -> eligible routes | Inspection does not reserve a half-open probe. Registry/race suites. |
| Provider circuit | Closed -> open -> one half-open probe -> healthy/reopen; auth/config -> disabled until revision | Only the admitting generation can complete its transition. New circuit lifecycle suite. |
| Resolve race | Fresh cheap routes -> bounded retries; delayed sequential heavy route -> winner/deadline | First equivalent winner; losers cancelled and joined. Race suite. |
| Proxy route health | Healthy -> cooldown -> eligible recovery | Shared route health and explicit source affinity remain separate. Proxy/YouTube route suites. |
| HTTP resolver/pacer | Per-origin admission -> request/redirect -> classified result | Locks release on cancellation; origin and redirect header contracts persist. HTTP provider suites. |
| Cobalt origin aggregation | Eligible origins -> candidate or combined failure | Preserve retryability and avoid false global disable. HTTP provider regressions. |
| Provider adapters | Resolve -> link/album -> bounded probe -> candidate/rejection/failure | Shared validation enforces request equivalence. HTTP-provider/validation suites. |
| Local extractors | Eligible -> proxy/direct extraction -> candidates/refresh descriptor | Auth opt-in and original-language/output/order affinity remain. yt-dlp/gallery-dl provider suites. |
| Pipeline shared work | Resolve flight -> materialize flight -> publish -> refcounted use -> last-owner cleanup | Caller scope and publication ownership remain distinct. Pipeline/singleflight suites. |
| Pipeline fallback | Resolve/materialize/validate -> remaining candidate/reserve or publication | Unused prefetched work is cancelled and joined. Pipeline suite. |
| Full YouTube route race | Parallel complete attempts -> verified winner -> loser cleanup | One materialization measurement/delivery; cancellation joins losers. YouTube route race suite. |
| Transport | Safe URL/DNS -> probe -> redirect/range body -> validate -> promote or remove | Byte/deadline/range/source-validator contracts persist. Transport/range/curl suites. |
| Transport refresh | Transfer failure -> one shared refresh -> equivalence/affinity validation -> retry | Bounded deadline and no changed output semantics. Transport suite. |
| Disk reservation | Reserve -> bind/renew/rebind/promote -> release | Owner-token markers; stale owners cannot mutate leases; exact-once accounting. Resource-budget suite. |
| Media transform | Reserved inputs/output -> supervised transform -> validate/promote -> transfer/cleanup | Inputs remain owned until workers/processes settle. Transport/converter cleanup suites. |
| Media lease heartbeat | Operation + renewal -> result, renewal failure, or cancellation -> joined cleanup | Never release the materialization while delivery cleanup is active. New lease lifecycle suite. |
| Cache records/leases | Miss -> typed record -> expiry/eviction; lease acquire -> renew -> owner-checked release | Stable metadata, signed URLs, Telegram IDs, and tokens have separate identities. Media-cache suite. |
| Singleflight | No flight -> active subscribers -> completion or last-subscriber cancellation -> joined removal | Subscriber cancellation isolated; recursive same-key work rejected. Singleflight suite. |
| Process ownership | Captured owner epoch -> bounded workload/process grant -> launch -> active -> cancel/finish -> released | No unregistered slot, orphan spawn, duplicate finish, or premature resource release. Process-supervisor suite. |
| Queue lease/waiter | New -> immediate grant or waiting -> grant/withdraw -> released | FIFO capacity, idempotent release, one settled notifier per waiter. Queue cancellation suite. |
| File cache | Insert/refresh/alias -> retained -> replace/expire/remove -> final-owner cleanup | Same source remains available while any cache entry owns it. Cache suite. |
| Ephemeral storage/limiter | Store -> get/delete/TTL expiry; token refill -> admit/reject | Existing memory/Redis storage and rate-limit contracts retained. Storage/limiter suites. |
| Legacy downloader | Cached file or supervised stream/process -> output/cache publication or cleanup | Progress observers finish before caller ownership returns. Downloader/new lifecycle suites. |
| Legacy gallery | Owned directory -> subprocess -> collected result -> transfer or cleanup | Every non-transferred outcome removes its directory. Gallery/new lifecycle suites. |
| Legacy orchestration | Queue -> source -> transform/split -> sequential sends -> cleanup/release | Split ownership precedes sends; all extras settle on cancellation. Orchestrator/new lifecycle suites. |
| Instagram session pool | Initialize -> round robin -> limiter -> selected/dead/exhausted | Session still live after limiter await; finite rotation. Instagram/new lifecycle suites. |
| Native media streams | Open -> bounded reads/writes -> result or close/remove; batch siblings -> joined cleanup | Actual byte caps, truncation detection, batch cancellation. Legacy streaming suites. |
| Converter/sender/cookies | Derived output -> process -> transfer/cleanup; receipt adapter; cookie initialize -> process-exit retirement | Existing boundary contracts retained. Converter/sender/cookies suites. |
| Private parsing | Parse owner/waiters -> cache/menu/direct send/error/cancel -> wake/cleanup | One request-local snapshot; waiters released in finally. Message/bot boundary suites. |
| Picker/back | Request snapshot -> chosen link -> refreshed picker or expiry | New controls never borrow a later request; legacy decoder retained. New menu-binding suite. |
| Callback operation | Idle -> claimed -> admission/queue/media -> success/error/cancel -> released | One overlapping operation per request within one process; later reuse allowed. New overlap suite. |
| Slideshows | API/legacy/pipeline choice -> queue -> download/transform/send -> cleanup | Snapshot preserves request, mode, clip, auth, and overlap ownership. Slideshow/group suites. |
| Bot retry | Saved -> owner check -> active -> predecessor/UI/polling reservation -> delivered/retryable/finished | Unsafe predecessor outcomes and duplicate active snapshots cannot replay. Retry/media retry suites. |
| Instagram navigation | Profile/story/highlight -> ordered cached items -> authorized selection -> receipt | Scope, clip, and order retained; malformed caches expire. Instagram callback suites. |
| GIF/source binding | Exact Telegram/local identity -> claim -> recovery/conversion/send -> cache transfer or cleanup | Source version and receipt identity survive cache expiry and local recovery. GIF/source-binding suites. |
| User preferences | Empty/format/quality -> merged snapshot/reset | Per-user serialization and no mutable read alias. New preference concurrency suite. |
| Janitor | Normal/warning/critical -> healthy recovery; retained lease -> expired cleanup | Deliberate disk hysteresis, bounded alerts, lease-aware cleanup. Janitor suites. |

## Verification and limits

Final checks on the corrected working tree:

| Check | Observed result |
| --- | --- |
| Full offline suite with coverage gate | 1,918 passed, 3 skipped, 5 integration cases deselected; 46 subtests passed; exit 0 |
| Combined statement/branch coverage | 79.81%, above the required 60% |
| Separate offline media acceptance | 36 passed, 1 live case deselected; exit 0 |
| Ruff check over app/tests | Passed |
| Ruff format over changed Python files | Passed after formatting |
| mypy, Linux and Windows targets | Both passed, 81 source files each |
| Five release shell scripts | All parsed with Git Bash `bash -n` |
| Whitespace/error check | `git diff --check` passed |
| Independent resource regression review | 86 passed |

The full run completed in 422.90 seconds on this Windows workstation. Its three
warnings concern existing Starlette/httpx deprecation and curl's Windows
selector-thread adapter. This test duration is not a media-performance result.
Docker is not installed, so `docker compose config --quiet` was not run.

Use the project `.venv` (Python 3.12 with pinned dependencies). PATH Python on this
workstation is 3.14 with different library versions and is not the release
environment. Set `PYTHON_DOTENV_DISABLED=1`, `BOT_TOKEN=test_token`,
`WEBHOOK_URL=https://example.com`, and `TELEGRAM_SECRET_TOKEN=test-secret` for
offline verification so the production `.env` is not loaded.

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest tests/acceptance/test_media_release.py --no-cov -q
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m mypy app --no-incremental --platform linux
.\.venv\Scripts\python.exe -m mypy app --no-incremental --platform win32
git diff --check
```

Offline fakes, local HTTP/SOCKS peers, native Windows subprocesses, and fixture
databases exercise these transitions. No live provider requests, Telegram sends,
production database operations, deployment, or performance measurement belong
to this audit. Linux type checking does not execute POSIX process cleanup.
Existing hard bounds for cancellation-resistant injected transport cleanup are
retained. No TLC model was executed and no formally proved invariant is claimed.

Callback claims and preference locks are process-local, matching the supported
single-consumer deployment. Existing unbound menus remain compatible for their
TTL and retain their historical context-based behavior; newly emitted menus
carry request identity. Deliberate later button reuse is permitted, while
durable success/uncertainty protection remains scoped to its logical job/item.
