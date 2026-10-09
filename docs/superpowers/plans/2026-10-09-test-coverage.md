# Test Coverage and Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Исправить воспроизведённые дефекты, сделать проверки объективными и построить полную карту необходимых тестов.

**Architecture:** Сохраняем существующие интерфейсы и разделение модулей. Каждое исправление защищаем наблюдаемым регрессионным тестом; итоговые метрики включают строки, ветви и входную точку приложения.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, pytest-cov, Hypothesis, закреплённые зависимости проекта.

**Spec:** docs/superpowers/specs/2026-10-09-test-audit-design.md

## Global Constraints

- Python 3.12 и закреплённые requirements.txt / requirements-ci.txt.
- Детерминированные проверки без обращений к сторонним провайдерам и production.
- Не перестраивать архитектуру; сохранять публичные интерфейсы.
- RED до исправления; GREEN после. Не ослаблять assertions ради зелёного результата.
- Не коммитить, не публиковать, не деплоить; сохранять чужие изменения.
- Сабагенты: high или выше, xhigh для concurrency и итогового ревью.

## Review Focus

- Отмена в момент освобождения: чужие ресурсы и ёмкость очереди остаются корректны.
- Отмена после появления готового файла: нет файлов без lease и потерянных резервов.
- Перезапуск после failed/uncertain delivery: нет дубликатов и пустых восстановленных заданий.
- Реальный fallback: порядок и выбранные элементы альбома, аудиоформат и профиль качества сохраняются.
- Ошибка зависимости и плохие метаданные: не выдавать несуществующий или слишком большой результат как успешный.

---

### Task 1: Core cancellation, ownership and retention

**Files:** app/core/{download_queue,resource_budget,drain,job_store,cache}.py; app/core/storage/redis_storage.py; tests/core/test_{download_queue_cancellation,resource_budget,drain,job_store}.py; tests/test_{cache,storage_redis}.py.

**Interfaces:** Существующие DownloadLease.release(), DiskReservation.release()/ensure(), DurableUpdateWorker.start()/stop(), DrainController.drain(), JobStore.renew_claim()/purge_update_payloads(), FileTTLCache и RedisStorage.clear().

- [x] Read artifacts/test-audit/core-report.md and reproduce each accepted finding.
- [x] Add failing tests for cancelled queue release, simultaneous disk release, corrupt sidecar shape, worker cancellation with an active handler, rollback after worker startup failure, expired claim renewal and retained retry payload.
- [x] Define drain deadline behavior against actual deployment callers; cover slow checkpoint without claiming impossible forced termination of arbitrary cancellation-resistant Python code.
- [x] Add exact callback tests for capacity eviction and fake-clock TTL expiry; prefixed Redis clear must preserve other namespaces. Keep unprefixed clear's existing explicit database scope.
- [x] Run new tests, record expected failures; implement minimal fixes and run core/storage/cache suites.
- [x] Independently review ownership, cancellation propagation and tests; fix review findings.

### Task 2: Media artifact lifecycle and request equivalence

**Files:** app/services/media/{pipeline,transport,validation}.py; tests/media/test_{pipeline,transport,models,validation}.py; tests/media/providers/test_ytdlp_provider.py as needed.

**Interfaces:** Existing MediaPipeline public resolve/materialize/deliver methods, MediaTransport.adopt_local(), candidate projection, request_from_download_context(), build_media_request().

- [x] Read artifacts/test-audit/media-report.md and its reproducible scripts.
- [x] Add RED tests for cancellation during metadata publication, noncancelable thread move and cleanup of a download race winner; timeout after transform must delete owned output.
- [x] Test actual selected album fallback with order (2, 0), command quality profile through public pipeline, audio request with cached video height, and 120-second input transformed to the documented 60-second animation.
- [x] Invalid YouTube clip values must raise a typed error rather than silently change platform/request. Determine whether callback encoder misuse is reachable before changing its interface.
- [x] Replace the baseline sleep/count heartbeat assertion with event-driven evidence of renewal while delivery is suspended, including no renewal after release.
- [x] Implement minimal fixes, run media suites and independently review ownership and request/cache compatibility.

### Task 3: Legacy service result correctness

**Files:** app/services/{cobalt,converter,orchestrator}.py; app/services/ytdlp/{service,parsers}.py; related existing service/parser tests.

**Interfaces:** Existing download_slideshow(), extract_video_meta(), split_video_stream_copy(), compress_video_to_size(), rewrite URL and TikTok error classification APIs.

- [x] Read artifacts/test-audit/services-report.md and confirmed reproductions.
- [x] RED: failed slideshow audio cannot return a nonexistent/partial path; cached duration must not suppress codec/pixel-format inspection of real output.
- [x] RED: split and compression results exceeding explicit target cannot be returned as conforming output; failure cleans owned outputs.
- [x] RED: m.vk.com rewriting is idempotent and does not replace a URL's unrelated text; HTTP 404 classifies as missing instead of authentication.
- [x] RED: legacy DownloadOrchestrator passes payload.section through the actual downloader boundary; clips cannot silently become complete videos.
- [x] Preserve intentional cache metadata/transform behavior using discriminating inputs. Implement minimal fixes and run legacy service/parser suites.
- [x] Independent task review, then resolve valid findings.

### Task 4: Objective tests, full coverage map and final verification

**Files:** tests/test_{security,security_main,security_html_injection,injection_prevention,logic_stability,new_features,bot_callbacks}.py; tests/conftest.py if isolation failure is demonstrated; pyproject.toml; docs/testing/2026-10-09-test-audit.md; docs/testing/2026-10-09-coverage-matrix.csv.

**Interfaces:** Real API routes, handlers, downloader queue routing; no copies of implementation inside tests.

- [x] Replace stdlib-only HTML/auth/sanitization checks and literal-set routing tests with calls through actual application boundaries.
- [x] Require gallery-dl command invocation before checking separator/cookie positions; remove broad exception swallowing and conditional assertions.
- [x] Deduplication test invokes the real handler with controlled events and proves one extraction and both callers' outcome.
- [x] Fix the progress-error test to inject the error named by the test. Add bounded command/settings and Instagram callback tests for meaningful uncovered behavior.
- [x] Measure main.py and branches, retain the existing floor without artificially inflating it; produce every-source-file mapping and distinguish behavioral tests from import coverage.
- [x] Run complete offline suite, lint/type checks appropriate to touched code, targeted reverse-order tests and independent final review.
- [x] Record remaining real Redis, POSIX/process and provider/Telegram acceptance work explicitly; never describe unexecuted live checks as passing.

## Execution record

Ledger and task reports live under .superpowers/sdd/2026-10-09-test-coverage/; raw audit artifacts under artifacts/test-audit/. Update this plan after verified task completion.

### Task 5: Acceptance evidence and cookie-export tooling contracts

**Files:** scripts/media-acceptance-docker-adapter.py; scripts/collect-media-release-evidence.py; scripts/debug_youtube.py; tools/export_cookies.py; tests/acceptance/test_{docker_evidence_adapter,live_collector}.py; new tests/test_export_cookies.py and tests/test_debug_youtube.py.

**Interfaces:** Actual MetricsCollector exposition consumed by embedded metrics_snapshot; adapter observations consumed by collector; strict smoke/resume/finalize evidence; browser export CLI with synthetic data only.

- [x] Read artifacts/test-audit/infra-report.md and infra_repro.py; reproduce four confirmed problems.
- [x] RED: real first-byte and wasted-byte label shapes retain measurements without bypassing quiescent/request attribution fences.
- [x] RED: a success string with unfinalized/unconfirmed/missing delivery ID must never count as confirmed full_delivery through adapter and collector.
- [x] RED: unsafe unknown fields in smoke/resume data are rejected before publishing output or executing more work; valid existing sanitized reports remain accepted.
- [x] RED: export with no Google/YouTube cookie match cannot return unrelated sessions; exact-domain filtering, path spaces, cancellation and cleanup must honor safe synthetic CLI inputs.
- [x] RED: debug_youtube awaits ExtractionResult and reads its fields instead of unpacking a coroutine.
- [x] Minimal fixes, focused acceptance/tooling suite, independent task review; no real browser cookies, subprocess provider network, Docker service changes or production actions.


## Итог выполнения — 9 октября 2026

Все пять задач выполнены и независимо проверены. Результаты: [итоговый аудит](../../testing/2026-10-09-test-audit.md), [106-файловая карта](../../testing/2026-10-09-coverage-matrix.csv), [независимое заключение](../../testing/2026-10-09-independent-review.md).

Окончательный офлайн-прогон после всех исправлений: 1571 passed, 42 subtests passed, 1 Windows/POSIX-mode skip, 5 integration cases deselected; exit 0. Источник и raw artifacts зафиксированы в итоговом аудите. 303 reverse-order tests passed; новых Ruff/mypy diagnostics нет, исходный debt сохранён явно.

Ревью найденных ошибок закрыто повторной проверкой: callback Back compatibility, unsafe resume до identity/preflight, поздние реальные SQLite операции и общий Redis/deferred runtime. Поддерживается один активный bot lifespan; повторный запуск разрешён после фактического завершения cleanup.

Оставшиеся runtime/live layers и дополнительные непроверенные сценарии перечислены по приоритетам в карте и аудите. Они не обозначены успешными. Коммиты, публикация и deployment не выполнялись; пользовательские исходные untracked файлы сохранены.
