> Это исходный аудит HEAD cc6e06edc1ad699c6540aa85ede771c53429bd9c, выполненный до исправлений. Состояние исправлений и финальные измерения указаны в 2026-10-09-test-audit.md. Ссылки на строки относятся к исходной версии.

# Аудит media pipeline и связанного покрытия

Дата: 2026-10-09. Область: все 19 файлов `app/services/media/**`, все 13 файлов `tests/media/**`, `tests/test_pipeline_entrypoints.py` и пять resolver fixtures. Все перечисленные ниже исходники и тесты прочитаны целиком. Production-код и существующие тесты при аудите не изменялись. Live network tests не запускались. Минимальные воспроизведения использовали async/thread Events, временные каталоги, настоящие DiskBudget/MaterializedItem и подмену внешнего I/O.

Окружение воспроизведений: `.worktrees/media-provider-racing/.venv/Scripts/python.exe`, Python 3.12.14. Импорт `tests.conftest` задавал тестовые env defaults. Два inline Python-скрипта завершились exit 0 и напечатали результаты ниже. Отдельно запущено девять выбранных test functions, включая параметризацию: **11 passed in 1.45s**, `--no-cov`. Общий suite/coverage выполняет основной агент.

## Подтверждённые дефекты и конкретные regressions

### M1 — P1: отмена во время записи metadata теряет ownership готового артефакта

`app/services/media/pipeline.py:738` вызывает `await self._store_metadata(selected)` после успешных `materialize` и artifact validation, но до protected publication на строках 739–753. `CancelledError` из metadata cache выходит за пределы cleanup для `completed`; subscriber всё ещё имеет `item=None`. `_release_materialization_user` удаляет pending counter, не находя ready item.

Воспроизведение: передать готовый `MaterializedItem` через transport double и `resolved=...`; cache `put_metadata` устанавливает Event и ждёт другой Event; отменить последнего subscriber после первого Event. Реальный DiskBudget и привязанная lease остаются:

```text
metadata_cancel: file_left=True, lease_left=True, reserved_bytes=5,
ready=0, pending={}
```

Regression: отменять после validation на metadata boundary; после await cancellation должны быть удалены output/lease, `reserved_bytes==0`, оба flight maps и pending/ready maps пусты. Проверить также cache exception, повторную отмену, несколько subscribers, один из которых остаётся. Существующий `test_artifact_validation_failure_releases_materialized_lease` проверяет более раннюю фазу; `test_delivery_cancellation_is_raised_only_after_lease_release` — значительно более позднюю.

### M2 — P1: отмена `adopt_local` отпускает lease до окончания worker-thread move

`app/services/media/transport.py:699` ждёт `asyncio.to_thread(self.file_mover, source, adopted)` без механизма ожидания окончания I/O при cancellation. Thread продолжает работу после отмены coroutine, а `finally:738–743` уже удаляет destination/lease и освобождает reservation.

Воспроизведение: mover устанавливает threading.Event, ждёт разрешения, затем вызывает настоящий `os.replace`; отменить task между Event и replace, дождаться task cancellation, затем разрешить thread. Результат:

```text
before_thread_finishes: file_exists=False, reserved_bytes=0
file_left_after_thread=True, lease_left=False, reserved_bytes=0
```

Это реальная гонка ownership, а не предположение о конкретной скорости файловой системы. Regression должен синхронизировать move Events, дождаться окончания thread и проверить отсутствие orphan output. `test_adopt_local_owns_and_clips_cross_filesystem_artifact` проверяет только EXDEV happy path; cancellation move не покрывает. `_copy_local_file` уже использует `_await_thread_io`, что даёт местный пример корректного владения локальной I/O операцией.

### M3 — P1: отмена small-candidate race теряет уже завершившегося winner

`app/services/media/transport.py:760–807`: после `winner=result` task удалён из `tasks`. Если caller отменяется, пока race ждёт cleanup проигравшего (`gather` на строке 799), winner не возвращается и не освобождается; finally освобождает только результаты остающихся tasks.

Воспроизведение непосредственно на `_race_small` с реальным leased winner: один attempt немедленно возвращает MaterializedItem; второй при отмене входит в `finally`, сигнализирует Event и ждёт. После Event отменить race. Результат:

```text
race_cancel: winner_file_left=True, lease_left=True, reserved_bytes=6
```

Regression: cancellation после выбора winner и до завершения loser cleanup; требуется release winner ровно один раз, отсутствие файлов/lease/reservations и прекращение обоих attempts. Дополнительно проверить две simultaneous successes, exception при loser release и повторную caller cancellation. `test_two_small_candidates_may_download_concurrently` проверяет `max_active==2`, но не эту ownership-фазу.

### M4 — P1/P2: deadline после transform оставляет финальный файл без lease

`app/services/media/transport.py:854–869` получает `final_paths` после `_finalize_candidate`, но exception cleanup на строках 892–895 удаляет только первоначальные `completed` inputs. Transform уже удалил их и создал другой final path. Если последующая проверка deadline или output size падает, финальный файл остаётся, а reservation/lease освобождаются.

Воспроизведение: реальная materialization с FakeClient + RecordingProcessRunner из существующих тестов; wrapper вокруг настоящего `_finalize_candidate` переводит инъецированный clock за deadline после возврата final paths. Результат:

```text
transformed_deadline: error=TransferTimeout,
files_left=['media_<uuid>.mp4'], reserved_bytes=0
```

Regression: истечение deadline именно после успешного promotion, до возврата MaterializedItem; после failure каталог пуст и reserved bytes ноль. Проверить post-finalization stat failure также. Имеющиеся failure/cancellation tests остановлены внутри transform и эту границу не достигают.

### M5 — P1/P2: album selection теряется на materialization fallback

`app/services/media/pipeline.py:781–786`: `_resolve_once` проецирует `album_selection` только на winner; `result.winner.alternatives` передаются неизменёнными. Transport действительно перебирает candidates; при отказе первого он может материализовать весь альтернативный альбом, а не выбранные элементы. `_resolve_reserve` на строках 822–826, напротив, проецирует и alternatives.

Воспроизведение: один provider возвращает два эквивалентных трёхэлементных album candidates; request selection `(2,0)`. Реальный pipeline resolution даёт:

```text
album_alternative: [['2','0'], ['0','1','2']]
```

Для delivery это нарушение явно выбранного состава/порядка. Regression: provider даёт минимум два album variants; первый materialization отказывает, второй succeeds; в transport/delivery должны попасть только элементы 2,0 в таком порядке, metadata/cache cardinality должна быть 2. Existing `test_album_selection_projects_fresh_items_and_cached_output_indexes` использует единственный candidate и не проверяет fallback.

### M6 — P2: профиль `/mp4` реально выключен canonical public work normalization

`app/services/media/pipeline.py:201–209,1585–1588` заменяет `caller_scope='command'` на `'public'` перед provider.resolve. Сортировочный профиль `app/services/media/providers/ytdlp.py:342–363` включается только для `caller_scope=='command'`. Существующие command-profile tests вызывают provider напрямую.

Воспроизведение: fixtures `metadata()` + добавленный 2160p source; одинаковый command request через provider напрямую и через настоящий pipeline:

```text
command_profile: direct='137+140', pipeline='401+140', pipeline_edge=2160
```

Reachability: `app/bot/commands.py` строит VIDEO request без quality через orchestrator caller_scope command, поэтому trigger соответствует настоящему `/mp4`. Regression должен проходить `MediaPipeline.resolve(build_media_request(...kind=VIDEO,caller_scope='command'))`. Намеренный профиль нужно сделать полем output equivalence (`output_variant` либо другой явный policy), чтобы сохранить coalescing public callers и правильную cache identity. Только сохранение caller_scope у command лишает его текущего public sharing, но тоже требует осознанного решения.

### M7 — P2: animation 60-second cap противоречит final duration validation

Transport намеренно ограничивает `mux_mode='mute-mp4'` до 60 s (`transport.py:1574–1587`), а `_expected_duration` (`pipeline.py:1357–1374`) ожидает полную исходную duration без применения cap. Финальный validator поэтому отвергает правильный 60 s animation из 120 s source.

Воспроизведение: request ANIMATION, candidate duration_seconds=120/mute-mp4; `_ffprobe` I/O double возвращает корректный silent MP4 H264 1280x720, duration60. Настоящий validator:

```text
capped_animation: ArtifactValidationError('final duration does not match')
```

Regression: actual transform→validator chain для animation исходной длины >60, а также clip duration>60 и start-only clip. Проверять ожидаемый duration=min(remaining/requested duration,60) согласно текущему transform контракту. Existing `test_animation_transform_mutes_and_caps_mp4_duration` проверяет аргументы ffmpeg и подставленные bytes без validator; validator animation tests используют duration20 и проверяют наличие audio, но не cap.

## Воспроизведённые contract inconsistencies с ограниченной reachability

M8 — P2/P3: `request_from_download_context` сохраняет `height=1080` у MP3 request (`pipeline.py:1096`), хотя ytdlp AUDIO candidates имеют width/height=None и `validate_candidate` отклоняет quality (`validation.py:72`). Inline real resolution напечатал `MediaResolutionError('ytdlp: quality_too_low; ytdlp: quality_too_low')`. Existing adapter test `test_legacy_download_context_maps_mp3_clip_and_quality_without_fallback` утверждает именно эту shape; командные/обычные special picker пути задают height=None (`commands.py:117`, `messages.py:653`). Поэтому это подтверждённая несовместимость поддерживаемого API/старого payload, но массовая production reachability не доказана. Решить, является ли visual quality осмысленным ограничением AUDIO; покрыть adapter→real resolver, не только поля adapter.

M9 — P3: `encode_callback_data('send')` возвращает `m2|send`, который собственный decoder отвергает. `_validate_callback_parts` проверяет len>=2, считая version частью payload; запрет отсутствующего payload в docstring и decode shape уже есть. В текущих callsites пустой payload не найден; это boundary/API defect, не подтверждённая текущая user failure. Добавить encoder negative tests и roundtrip всех допустимых cardinalities; вызов без parts должен иметь чёткий контракт.

M10 — P3: `build_media_request` ловит все `UnsupportedMediaUrlError`, включая invalid YouTube clip time, и трактует URL как generic other (`pipeline.py:1055–1073`). Воспроизведено для `youtu.be/abc123?t=nan`, `t=1m2s3`, `youtube.com/watch?v=abc123&t=-1`: запрос создаётся с platform other и пустым clip, query t удалён. `MediaRequest.from_url` явно rejects nonfinite/negative time, проверено existing models tests. У generic builder fallback намеренный; ожидаемый пользовательский контракт invalid clip требует решения (reject или documented generic handling). Не считать blocker до уточнения, но не позволять model-only negative tests создавать впечатление, что entrypoint invalid URL тоже отвергается.

## Карта исходников, реальных проверок и пробелов

| Прочитанный файл | Контракт и главный риск | Реальное существующее покрытие | Необходимые дополнения |
|---|---|---|---|
| `app/services/media/__init__.py` | Public reexports | Импортируется большинством tests | Только lightweight import/public API smoke при изменениях exports |
| `app/services/media/models.py` | Immutable request/candidate/receipt, canonical YouTube identity и cache key | Aliases/tracking/Shorts/audio-policy cache migration, nonfinite URL times, frozen request | One-field-at-a-time key invariants для quality/audio/clip/selection/watermark/scopes/exact/variant; deadline exclusion; malformed URL credentials/IPv6; receipt aggregates |
| `app/services/media/validation.py` | Candidate equivalence до победы race | AUTO/explicit kind/audio, album completeness/selection, watermark=False, media ID | Missing dimensions, watermark=None, auth mismatch прямо в validator, negative/empty selections; quality AUDIO contract M8 |
| `app/services/media/registry.py` | Single-loop breaker provider×platform, half-open single probe | 3/60/300 boundary, cooldown, cancellation probe, config revision, disabled/unsupported filters | INTERNAL neutrality, permanent/auth failure во время half-open, configuration revision while probe inflight; имя provider дублируется |
| `app/services/media/race.py` | 2 cheap workers, one sequential delayed heavy; joined cancellation; Retry-After | ManualClock budgets8/20, heavy delay1.5, winner validation, half-open admission, retries/trip, cleanup | Simultaneous successes/alternative ordering; repeated cancellation and cancellation-resistant adapter; late old attempt updates breaker state; validator throws; exact budget edges |
| `app/services/media/singleflight.py` | Shared factory per key with subscriber cancellation isolation and process owner | Strong Event tests, actual supervised Python process, last cancellation, exceptions/recovery, recursive reentry across task/key/group, inherited marker lifetime | Subscriber attaches during last unsubscribe/factory cleanup; factory completes concurrently with last cancel; large subscriber counts; per-subscriber deadline policy |
| `app/services/media/proxies.py` | Process-owned SOCKS creds, route affinity/cooldown/deadlines | Backup after AUTH/timeout, no fallback on permanent, cancellation stops backups, platform scope, URL secret redaction, real local SOCKS peer | All routes cooled recovery, same route concurrent success/failure ordering, no-proxy public AccessDenied classification; exact deadline; invalid timeout values |
| `app/services/media/transport.py` | SSRF/DNS pinning, bounded streams/ranges/transform/resources | Mixed private DNS, redirect auth/body checks, unknown signature/HTML, limits/timeouts, cancellation cleanup, EXDEV adopt, ffmpeg argv, process cleanup, janitor promotion race, refresh identity/language | M2/M3/M4 first; repeated cancellation at response close/write/reserve/release, cancellation after stream promotion, actual ffmpeg output semantics, response body→Content-Length integrity, second DNS answer/IPv6/port0, range missing/weak validator and changed encoding |
| `app/services/media/delivery.py` | Typed Telegram receipts, durable outcomes, safe retry, cache IDs, owned file handles | All send types, NetworkError uncertain/no retry, invalid ID known-not-sent fallback, cache failure tolerance, 11-item chunk9+2, animation singly, partial retry, local path confinement and cloud decimal limit | Actual durable current_job context with send/cache/store cancellation windows, unexpected TelegramError vs uncertainty, incomplete/extra album receipts, group invalid-ID fallback and missing item, mixed audio/document/visual units, caption/reply/spoiler/thumbnail mapping, stream size enforcement |
| `app/services/media/pipeline.py` | Resolve→materialize→validate→cache/deliver and leases, public sharing vs authorized scope | Common domains/cache-before-providers, fallback, TikTok prefetch, ordered album, metadata cardinality, scopes, heartbeat errors, artifact codec/container/language/orientation/duration | M1/M5/M6/M7 first; full provider→transport→validator→delivery composition; overlapping deadlines in coalescing; simultaneous late ready registration/release; slow metadata boundaries; alternatives metadata reflects actual selected MaterializedItem |
| `app/services/media/providers/__init__.py` | Package marker | Implicit imports | No dedicated test justified |
| `app/services/media/providers/http.py` | Resolver HTTP, credential redirects, origin pacing, classified errors, bounded production probe | Cross-origin sensitive headers/body-preserving refusal, retry429, timeout/challenge, production safe-probe delegation, independent-origin pacing | Actual CurlProviderTransport HTTP boundary, bounded resolver body, safe redirect schemes/destinations, 401/403 auth with credentials after redirect, same-origin/default-port equality, Retry-After date/malformed/nonfinite, cancellation queue/pacer |
| `app/services/media/providers/cobalt.py` | Configured origins, sequential fallback, direct/picker normalization | Failing first origin→second, auth scoping/stripping, mixed picker fixture, pacing, shared HTTP errors | Audio/photo direct response contract, malformed/empty picker, all origins fail with different FailureKinds, identity/duration not blindly claimed, unsupported statuses, mixed per-item expiry |
| `app/services/media/providers/tikwm.py` | HD/SD video or ordered image album+optional soundtrack | HD despite smaller bytes, SD limitation, order/music, expired music/video, pacing/errors | Missing/bad data/numeric fields, expired HD with live SD, ignored optional music failure policy, quality metadata absent, identity mismatch, watermark policy |
| `app/services/media/providers/ssstik.py` | Live HTML form+session then ready link parsing | One synthetic form/result fixture, origin exfil refusal, watermark classification, pacing/errors | Multiple forms, session cookies, dynamic token shape, missing/partial fields, alternate link attributes/relative URL, several variants with one bad probe, audio-only links, actual verified web contract |
| `app/services/media/providers/snapsave.py` | Disabled unless verified; ready link vs remote render | Default disable, ready fixture720, pending job ID, pacing/errors | Verified origin HTML variations, ready+job mixture, several candidates with one failed probe, exact candidate validation; fixture provenance |
| `app/services/media/providers/fxtwitter.py` | API v2, attachment order, quoted media, ranked formats | Mixed fixture order, max variant, quality limitation, zero GIF duration, quote fallback, pacing/errors | Missing all with separate photos+videos incomplete flag; malformed all entries cannot silently imply complete; NaN/inf dimensions/duration, unsupported variant protocols/container, single animation semantics |
| `app/services/media/providers/gallerydl.py` | Public local process dump, ordered media/safe headers/refresh | Two direct tests + proxy/selection refresh tests | Actual `_dump_gallery` subprocess command/config-ignore/stdout/JSON/exit/cancel contract, unavailable package, malformed/unsupported items and completeness, headers secrets/CRLF, numeric overflow/nonfinite, disappeared variant, refresh auth/deadline |

## Объективность и ограничения тестов

1. Большинство tests проверяют полезные observable behaviors: отказ SSRF до socket/body, конкретный order bytes/items, callbacks реально вызывают адаптер, cancellation ждёт cleanup, unsafe retry не происходит. Нельзя назвать suite преимущественно тавтологичным. Есть сильные local integration проверки process ownership, janitor interleaving, curl close и SOCKS DNS affinity.
2. `test_every_entrypoint_builds_the_same_canonical_media_request` (`tests/test_pipeline_entrypoints.py:53`) семь раз вызывает один `build_media_request`, меняя только caller_scope; он **не вызывает семь entrypoints**. Остальные реальные entrypoint tests частично компенсируют, но имя и число parametrized cases здесь переоценивают end-to-end доказательство.
3. `test_cache_key_isolates_scopes_and_all_delivery_equivalence_fields` (`tests/media/test_models.py:114`) создаёт base с множеством nondefaults, а сравнивает с requests, отличающимися сразу множеством полей. Тест не доказывает включение каждого поля: удаление quality/clip/watermark/caller_scope из key может остаться незамеченным. Нужна поочерёдная perturbation одного поля через dataclasses.replace.
4. Fast-command tests напрямую вызывают YtDlpProvider и успешно проходят при сломанном реальном pipeline (M6). MP3 adapter тест доказывает shape, которая несовместима с real validator (M8). Это конкретные ошибки выбора test boundary, а не просто нехватка числа tests.
5. Pipeline `_Transport` почти всегда возвращает заранее выбранный MaterializedItem независимо от request/candidates. `_Delivery.retry_failed` делегирует `deliver` всего materialized result и не фильтрует assets. `test_partial_delivery_retries_only_known_failed_items` поэтому доказывает merge предзаданного receipt, но **сам не доказывает**, что successful/uncertain items не отправлены повторно; настоящий TelegramDelivery покрыт другим тестом. Нужен один composition test с настоящим delivery и bot I/O double.
6. Transform tests используют RecordingProcessRunner, записывающий `ftyp...` bytes, и проверяют argv. Validator tests подменяют `_ffprobe`. Они полезны для планов и контроля ресурсов, но их комбинация не доказывает реальную playable MP3/clip/animation или duration semantics; M7 попал между этими слоями. Нужен tiny generated media fixture + настоящий ffmpeg/ffprobe offline integration для copy mux, MP3, rotation/clip/cap.
7. HTTP `ok_probe()` fixture имеет status206 и Content-Type, но пустое body; RecordingTransport ветка production safe signature check не исполняет. Это adapter fixture tests, не доказательство безопасности production curl boundary. Fixtures синтетические `*.example`, без записанной даты/source/schema provenance; они проверяют parsers для своего fixture, не актуальность SSSTik/SnapSave/FxTwitter API. Live contract validation требуется отдельно от обязательного offline suite.
8. `test_large_candidates_transfer_only_once_at_a_time` (`test_transport.py:1320`) даёт first successful candidate; второй не пытается. Даже ошибочная parallelization поздних large fallbacks не обязательно обнаружится. Сделать первый fail после controlled open, второй succeed и доказать max active1.
9. Timing-sensitive tests используют sleep0.035 с heartbeat0.01 и ожидание >=3, жёсткие elapsed <0.08/<0.1 или много `sleep(0)`; они могут быть flaky под Windows/CI load. Event-based фазовые границы и injected clocks предпочтительны; elapsed bounds нужны с реалистичным margin именно для timeout contract. Некоторые subscriber-count polling loops без timeout способны повесить suite на regressions.
10. `test_gallery_dl_is_public_and_platform_scoped` предполагает установленный optional gallery_dl: используется `extract=None`, затем ожидается supports=True. Для deterministic unit test inject extractor/availability; наличие/отсутствие package проверять отдельно. Parametrized HTTP provider objects создаются на collection-time с mutable RecordingTransport.responses, что ухудшает возможность повторного запуска того же case в одном процессе.

## План необходимого покрытия

Приоритет P1: зафиксировать M1–M5 failing regressions с точной ownership фазой и проверкой четырёх инвариантов: task termination, files/lease cleanup, DiskBudget.reserved_bytes, singleflight pending/ready/subscribers. Затем исправить production ownership и выполнить red→green+профильные tests.

P2: M6 и M7 через настоящий pipeline composition; album alternatives/metadata actual winner; authorized and public requests never share bytes/cache across auth_scope; full pipeline/cache stale file-ID/partial receipt retries with genuine TelegramDelivery and bot double. Определить contract AUDIO quality (M8), generic invalid YouTube URL (M10), deadlines of coalesced subscribers, clipped albums, successful output from fallback whose cardinality отличается от initial resolver winner.

P3: расширить matrix of independent request-cache fields, malformed provider payloads, 401/403 classification, Retry-After finite/date values, parser variants and fixture provenance. Оставить local curl+SOCKS tests offline и без credentials. Отдельный opt-in monitored/live contract suite не должен быть обязательным CI и не может заменять offline assertions.

## Подтверждённая нестабильность baseline slideshow test

Основной агент сообщил полный baseline: 1362 cases, 1 failure, 1 skip; combined branch coverage73.74%, `app/main.py` исключён из coverage. Прочитан полный relevant traceback `artifacts/test-audit/pytest-baseline.log`. Failure — `test_slideshow_video_uses_soundtrack_validation_delivery_and_lease_heartbeat` на `tests/media/test_pipeline.py:357`, источники должны иметь renewed>=3 после delivery sleep0.035 и heartbeat interval0.01.

Изолированный запуск того же pytest case воспроизвёл failure (1 failed in0.75s). Затем тот же test body запущен восемь раз с trace wrapper только вокруг `_Reservation.renew`, без изменения scheduler или production logic. Первый run failed, остальные семь passed:

```text
failed: renewal counts [2,2,1]; renew call times ms [2.62,2.72,27.22,27.22,27.23]
passed example: counts [3,3,2]; times ms [1.11,1.19,15.36,15.36,15.36,31.33,31.34,31.35]
```

У images/audio есть initial renew; у derived нет initial renew и счётчик отражает только heartbeat. В failed run delivery уже completed к следующему tick, поэтому heartbeat корректно остановлен после одного tick. `asyncio.sleep(0.01)` не гарантирует tick каждые10ms на Windows; наблюдённый первый heartbeat задержан примерно24.5ms. Это **не доказательство production failure renewal**: default pipeline interval300s, реальные lease TTL3600s, а test выбрал10ms с жёсткой cardinality по35ms. Исправлять следует test synchronization: удерживать delivery Event до двух наблюдённых heartbeat cycles/renew evidence, затем завершать и проверять shutdown-before-release guard. Сохранить assertion, что heartbeat не renew после начала derived release; просто уменьшать счётчик/увеличивать arbitrary sleep скрывает причину нестабильности.

## Полный список прочитанных тестов

- `tests/media/test_models.py`
- `tests/media/test_validation.py`
- `tests/media/test_registry.py`
- `tests/media/test_race.py`
- `tests/media/test_singleflight.py`
- `tests/media/test_proxy_routes.py`
- `tests/media/test_transport.py`
- `tests/media/test_range_downloads.py`
- `tests/media/test_delivery.py`
- `tests/media/test_pipeline.py`
- `tests/media/providers/test_ytdlp_provider.py`
- `tests/media/providers/test_http_providers.py`
- `tests/media/providers/test_gallerydl_provider.py`
- `tests/test_pipeline_entrypoints.py`

Дополнительно прочитаны целиком `tests/conftest.py`, пять `tests/fixtures/providers/{cobalt-picker.json,fxtwitter-status.json,ssstik-index.html,ssstik-result.html,snapsave-ready.html}`; релевантные фрагменты `app/core/resource_budget.py`, `app/bot/commands.py`, `app/bot/callbacks.py`, `pyproject.toml`; просмотрен git log media. Остальные tests вне выделенной области не оцениваются в этом отчёте.
