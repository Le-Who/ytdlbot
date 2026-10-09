> Это исходный аудит HEAD cc6e06edc1ad699c6540aa85ede771c53429bd9c, выполненный до исправлений. Состояние исправлений и финальные измерения указаны в 2026-10-09-test-audit.md. Ссылки на строки относятся к исходной версии.

# Аудит legacy services и связанных тестов

Дата: 2026-10-09. Область: все 19 Python-исходников app/services, кроме app/services/media. Код и существующие тесты не изменялись. Live network не использовалась. Для выявленных дефектов применён systematic-debugging: чтение реализации, трассировка контракта, независимый offline-вход, наблюдение фактического результата.

## Проверка

Python: E:/Projects/ytdlbot-1/.worktrees/media-provider-racing/.venv/Scripts/python.exe, 3.12.14, закреплённые зависимости. Основной выборочный запуск: 30 файлов тестов services/parsers, --no-cov, exit 0, 100% завершено. Отдельная проверка 68 tests (fast_path_opts, injection_prevention, пять orchestrator adapters, converter workload metrics): 68 passed, 2 dependency deprecation warnings, 2.06 s. Это не утверждение о полном suite и не live-проверка платформ. Полный suite и coverage выполняет основной агент.

Самостоятельный Python требует согласованных MAX_MEDIA_FILE_MB=MAX_DL_MB=MAX_TG_UPLOAD_MB=2000: без этого локальная .env дала RuntimeError conflicting media file limits. Это конфигурационный факт данного окружения, не установленный дефект конфигурационного парсера.

## Полностью прочитанные исходники и карта контрактов

| Исходник | Контракт / риск | Реальные имеющиеся проверки | Недостающее полезное покрытие |
|---|---|---|---|
| app/services/__init__.py | Пустой package marker | Импорт пакета во многих tests | Отдельный тест не нужен |
| app/services/ytdlp/__init__.py | Пустой package marker | Импорты service/parsers | Отдельный тест не нужен |
| app/services/ytdlp/models.py | FormatItem/FormatMetadata, восстановление ExtractionResult из dict | service/list_formats/parser consumers | from_dict round-trip через asdict/JSON, вложенный special_format, отсутствие mutation входа |
| app/services/ytdlp/builders.py | CLI args, одинаковая политика качества и звука во всех fallback, cookies/proxy/clip до source | test_ytdlp_service, fixes_verification, professional_features, injection_prevention, fast_path_opts | Комбинация create_format_item с builder и реальным yt-dlp selector; invalid audio language; extractor budget; mixed source flags |
| app/services/ytdlp/cookies.py | platform override, global fallback, B64 temp files, atexit cleanup | test_cookies_manager; реальные cookies+CLI integration в media provider tests | invalid B64/encoding; содержимое декодированного файла; VK; URL host versus query false positive; cleanup повторный |
| app/services/ytdlp/exceptions.py | Типизированная классификация stderr, DirectDownloadReady | test_ytdlp_exceptions; consumers messages_errors | LiveStreamError из extract не должен превращаться в generic при list_formats; порядок неоднозначных сообщений; VK badbrowser; truncation boundary |
| app/services/ytdlp/parsers.py | Детекторы платформ, duration/height/filesize, format normalization, scoring/dedup, TikTok classification | ytdlp_parsers, parsers_full, parsers_filesize, URL helpers, property_based, TikTok/router/slideshow | Пограничные HTTP status code; независимый oracle единиц bitrate; реальные codec/quality conflicts для dedup; malformed nullable metadata |
| app/services/ytdlp/service.py | extract subprocess JSON/error handling; bypass TikTok/Pinterest; list/cache; Python CLI executable | ytdlp_service/list_formats, professional_features, fixes_verification; media provider cookie tests | JSON empty/malformed, nonzero typed errors, timeout/cancellation; m.vk.com rewrite; cache file content/readability/cleanup; cache-write failure |
| app/services/gallery_dl/__init__.py | Пустой package marker | Импорты service | Отдельный тест не нужен |
| app/services/gallery_dl/service.py | supervised process; сбор images/audio/metadata; video fallback | gallery_dl_service, injection_prevention | Реальные nested directories, failed-download cleanup, пустые медиа, ordering across directories; spawn failure lifecycle |
| app/services/downloader.py | download_video tuple, token file cache, cancellation, size policy, MediaSender facade | downloader_service; process-supervisor owned partials; mocked callers integration | Нельзя считать ghost test реальной загрузкой; disk happy/error с async cancellation cache; pipe byte cap до EOF, timeout после stdout EOF; cache semantics между format/clip |
| app/services/sender.py | Совместимый API, structured DeliveryReceipt, file/URL/buffer и album delegation | sender_service, fast_path_opts; media delivery suite у другого агента | Проверять весь album и все receipt.items; reply/caption/thumbnail/operation-key propagation; реальный receipt вместо bool у callers |
| app/services/slideshow.py | gallery-dl delegation и cleanup directory | slideshow_service, slideshow_callbacks, group/converter extended | Nested slideshow root+audio cleanup; явное владение output directory вместо substring; gallery delegation args assert |
| app/services/orchestrator.py | Queue acquire/release; media pipeline adapter; legacy resolution/conversion/delivery; metadata codec checks | pipeline_entrypoints adapters; video_meta; fast_path_opts; bot integration и callback mocks | legacy clip propagation; cached-info versus actual selected media metadata; queue release во всех failures/cancellation; successful transcode cache consistency; split oversize/partial receipt |
| app/services/converter.py | ffprobe, stream-copy split, two-pass compression, thumbnail; MP3/Ogg/GIF/slideshow transforms | converter/service; fast_path_opts; group/converter_extended; metrics workload test | Проверка bytes/size output вместо mocked exists; oversize segment/compression отказ и cleanup; failed transform partial cleanup; MP4 atom v0/v1; adaptive duration/concat escaping; semaphore bounds |
| app/services/cobalt.py | per-origin process/retry and picker; byte-bounded disk streams; slideshow assets | cobalt_service; cobalt_streaming_safety | optional audio failure path; image failure concurrent sibling lifecycle; cancellation removes partial; declared length and cap at public API |
| app/services/tikwm.py | unshorten, lock pacing, cache expiry/canonical keys; retry CDN; disk stream cap/truncation; slideshow partial success | tikwm_service, tikwm_optimized, tikwm_streaming_safety | deterministic cache hit/TTL eviction/aliases; CDN retry exactly once and eviction; rate-limit timing with fake clock; malformed API body; cancellation and cap at public boundary |
| app/services/pinterest.py | Native resolution typed errors, PinterestProvider, OG/JSON scraping, legacy streaming/carousel fallback | pinterest_service; three PinterestProvider tests in http_providers | escaped HTML query params, JSON slash escapes; native 429 Retry-After; cap/truncation/cancellation in legacy streaming; carousel type/order/partial policy |
| app/services/instagram.py | URL parser, shortcode PK, session pool/limiter/eviction, stories/highlights, mobile fallback and CDN downloads | instagram_service; callers pipeline-entrypoints отдельно основным агентом | Session pool and donor rotation almost untested; death signals/retry/limiter; profile successful stories/highlights; highlight endpoint; post Cobalt/native; malformed reel item; truncated/empty/cancelled stream |

## Подтверждённые дефекты и точные регрессии

Оценка приоритета учитывает, что основной инициализированный MediaPipeline возвращает до legacy branches в orchestrator.py:380. Legacy-дефект не объявляется автоматически дефектом основного pipeline. Некоторые services (extract/PinterestProvider/Instagram) при этом вызываются и из активных маршрутов.

1. **P2: Cobalt возвращает путь к несостоявшемуся audio.** app/services/cobalt.py:268 присваивает audio_path до status/stream проверки. except на 273 не удаляет partial и не обнуляет путь; return на 276 сохраняет его. Offline fake session: image HTTP200, audio HTTP500 → images=1, audio_non_none=True, audio_exists=False. Для stream exception после первого chunk путь также укажет на неполный файл. Точная регрессия: параметризовать audio HTTP500 и chunk-then-raise; assert images валидны, audio is None, audio.mp3 отсутствует. В существующем cobalt_service есть только successful optional audio.

2. **P2: extract_video_meta доверяет extraction JSON как метаданным выбранного файла.** app/services/orchestrator.py:72 early return при одной duration; pix_fmt/codec_tag вообще не берутся из JSON. Offline JSON duration=10,h264,1920x1080,pix_fmt=yuv420p10le + fake probe actual HEVC → meta содержит h264 и не содержит pix_fmt, ffprobe_calls=0. При одной duration actual compatible file получит vcodec=None и пойдёт на лишнюю перекодировку; при cached h264 фактический HEVC или 10-bit может пройти safe checks на 146. Точная регрессия: JSON duration-only должен добрать codec/pix_fmt из actual file; stale cached codec/dimensions должны уступить probe фактического output; 10-bit cached metadata не должны обходить compatibility. Следует уточнить provenance fast path, чтобы не просто сохранять неправильные expectations test_video_meta.

3. **P2: split_video_stream_copy не обеспечивает заявленный upper size bound.** app/services/converter.py:184 проверяет лишь >0. Реальный ffmpeg segment_time не гарантирует размер из-за VBR/keyframes; также floor=10 секунд может превысить цель на коротком видео. Offline fake transform создаёт два реальных файла по 300 bytes при segment_bytes=200 → возвращаются [300,300]. Точная регрессия: output part >segment_bytes должен вызвать отказ/предусмотренный fallback и cleanup всех parts; правильные parts должны возвращаться sorted. Имеющийся fast_path_opts:test_returns_sorted_parts_on_success сам задаёт getsize=100MiB всем двум segment при ceiling47MiB и считает это success — он закрепляет дефект; дополнительно не проверяет порядок, только len=2.

4. **P2: compressor объявляет oversize output успехом.** app/services/converter.py:419-426 логирует размер и возвращает файл без сравнения с target_bytes. Offline fake two-pass creates actual900000-byte output при target800000 → result900000. Точная регрессия: oversize output возвращает None/явный отказ, удаляет output, сохраняет source; under-limit success path отдельно. Не требуется воспроизводить реальное двупроходное overshoot, ошибка public postcondition доказывается фактическим output после fake process.

5. **P2: m.vk.com переписывается в m.m.vk.com.** app/services/ytdlp/service.py:83,284 глобальный str.replace. Offline build_command('https://m.vk.com/video-1_2') → source https://m.m.vk.com/video-1_2. Точная регрессия: parametrized desktop/mobile/uppercase VK URLs; hostname replacement idempotent, path/query не изменяются. Нужны extract и build_command branch; subprocess fake проверяет args, network не нужна.

6. **P3: HTTP status code 404 классифицируется как AUTH_REQUIRED.** app/services/ytdlp/parsers.py:90 общий status code имеет приоритет над 403/404. Offline classify_tiktok_error('HTTP status code 404') → AUTH_REQUIRED. Точная регрессия: HTTP status code404→NOT_FOUND,403→FORBIDDEN, а TikTok business status10231→AUTH_REQUIRED. Existing tests проверяют HTTP Error403/404, минуя ошибочное условие. Utility сейчас менее важен, чем active typed provider boundary.

7. **P2: legacy orchestrator теряет clip section.** app/services/orchestrator.py:537-545 передаёт info_json_path/fallback/progress, но не payload.section; TikWM fallback calls тоже его опускают. Offline state.media_pipeline=None, real process_download with DownloadContext(section='*10-20'), fake download/send → success=True, download kwargs ['info_json_path','fallback_clients','progress_callback'], section None. Точная регрессия: legacy download must receive section='*10-20' (или typed rejection если native API не поддерживает clip). Инициализированный pipeline adapter уже проверяет request.clip и проходит; дефект относится fallback path.

## Подтверждённые слабости тестов, не равные багам продукта

- test_downloader_service:52-69 подставляет state.cancel_cache={} вместо async storage, real method делает await state.cancel_cache.get(token), получает TypeError, затем internal error. assertNotEqual(path,ghost) проходит и при этом unrelated crash. Заменить cache на MemoryStorage; fake child пишет output, assert конкретная успешная загрузка и build args. Existing real cancellation test с local subprocess/socket очень полезен и действительно проверяет освобождение порта/partials за2сек.
- test_injection_prevention:110 все assertions внутри if mock_run.called; полностью удалённый process call будет принят. try/except Exception тоже маскирует независимый crash. Требовать assert_awaited_once и всегда inspect argv. yt-dlp separator tests значительно сильнее: строго immediate-before-source.
- test_sender_service:147 название caps/truncated устарело: реальная delivery дробит album. Проверяется только последний media_group len≤10. Следует assert последовательность длин [10,5], что all15 items доставлены и receipt согласован, captions корректны. Иначе потеря первых/последних5 photos может проходить.
- test_cobalt_service conditional assertions (if res.error_message, if images) могут пропустить пустой error message / empty images. Требовать обязательные nonempty значения до detail assert. Mock headers MagicMock не проверяют declared length; streaming_safety делает реальные disk bytes и лучше моделирует этот контракт.
- test_ytdlp_service_list_formats TikTok auth/error tests устанавливают extract.side_effect, но TikTok pre-routing never calls extract. Это проверки bypass, а не обработки исключений. Объединить и назвать как bypass; assert_not_awaited. Auth mapping проверять в extract или exceptions.
- test_professional_features:19 assertRaises(Exception) допускает любой crash с подходящей строкой. Уточнить LiveStreamError. test_fixes_verification audio_selector_robustness реально делает video137 и проверяет bestvideo, не проверяя audio robustness. test_concurrent_fragments ищет str(config) где угодно в argv, не value immediately after --concurrent-fragments.
- Filesize tests часто вычисляют expected через тот же BITRATE_COEFFICIENT, включая parsers_filesize:31; это не независимый oracle единиц. Legacy helper использует128, тогда как conventional decimal1000kbps×10sec/8=1250000bytes; часть tests закрепляет1280000. Это математическое discrepancy; влияние estimated filesize2.4% и нужный compatibility contract следует решить до изменения.
- Большинство converter mocks одновременно подменяют exists=True/getsize=1024/process rc0: хорошо проверяют command routing, но ничего не доказывают про реальный output codec/container/duration/size. Добавить несколько offline tiny real ffmpeg integration tests при наличии binary, а не десятки похожих mocked success cases.
- Property-based tests преимущественно утверждают string/enum/not-crash или len(output)≤len(input). Это полезные totality smoke checks, но слабо проверяет correctness. Дополнить dedup: один winner на nonzero quality, вход не мутируется, победитель по codec/premux rule, идемпотентность; duration round-trip.
- Sender currently returns DeliveryReceipt, не bool. Callback/integration fake True совместим с truthiness, но не ловит код, использующий .success/.items. Поэтому при service boundary подставлять реальную receipt factory.
- Shared globals: mock_state и многие unittest asyncSetUp вручную заменяют state.* без восстановления. Риск order dependence не объявляется установленным failure, но улучшать isolation через monkeypatch/yield restoration и отдельные suites in both orders полезно.

## Риски для следующего исследования (пока не заявлены reproduced product failures)

- downloader.py:194 pipe BytesIO полностью заполняется до проверки size на240; byte ceiling не действует во время чтения. После EOF на206 стоит proc.wait без текущего polling budget. Нужны fake child: превышение tiny cap и закрытие stdout с продолжающимся sleep; проверить abort и cleanup в пределах timeout.
- Cobalt/TikWM/Pinterest/Instagram exception cleanup ловит Exception, не asyncio.CancelledError; частичные файлы на task cancellation могут остаться. Есть сильное доказательство структуры кода, но отдельный event-gated repro нужен перед исправлением. У Cobalt gather image-failure default не отменяет sibling tasks; нужен explicit sibling-lifecycle test.
- Instagram/Pinterest legacy streams не проверяют Content-Length и global cap, Instagram пишет sync file I/O в event loop. Нужно public boundary test declared length mismatch, empty body, tiny artificial cap и event-gated cancellation; не live/CDN test.
- cleanup_slideshow удаляет parent первой картинки по substring 'slideshow_' во всём path; gallery-dl может создавать nested directories, audio остаётся в root. Нужно реальное дерево /slideshow_x/sub/photos + root audio + sibling images, assert очищается принадлежащий output root, unrelated parent сохраняется.
- IG pool initialization/pickle is operator-configured. Не делать вывод remote code execution без untrusted-input boundary: IG_SESSIONS_B64 приходит из конфигурации. URL regex host false positives и missing endpoint timeout нужно оценивать по callers validation, а не объявлять generic SSRF.
- GIF stream-copy accepts HEVC as MP4-safe, хотя Telegram compatibility set narrower; необходимо определить animation delivery contract и tiny actual codec test. Ошибка контейнера и ошибка совместимости разные.

## Минимальная схема полезного нового покрытия

Приоритет1: regression каждого confirmed defect выше, real tmp_path bytes и typed receipt, fake process/session только на I/O boundary; доказать RED до изменения. Для VK и HTTP classification parameterization даст2-3 inputs на test. Для Cobalt audio включить HTTP и mid-stream exception.

Приоритет2: public download lifecycle (Cobalt/TikWM/Pinterest/Instagram) одним reusable fake streaming session с explicit active flag, Event after first chunk, declared length и tiny cap. Проверять cancellation propagates, session closed, partial отсутствуют, sibling tasks закончены, результат не success. Не дублировать internal helper tests без public boundary.

Приоритет3: VideoDownloader disk/pipe success+size/EOF timeout with real local Python child; extraction failure types+malformed JSON/cancellation; cache expiry/short alias eviction с fake clock; IG pool rotate/evict/limiter и successful profile/highlight/post branch.

Приоритет4: 3-4 real tiny FFmpeg fixtures: VP9→mute H264 MP4, WebM Opus→real Ogg header, non-MP3→MP3 probe, mixed-size images→playable slideshow with correct duration/audio. Проверять process/probe bytes. Tests marked integration если зависят от ffmpeg binary, default suite сохраняет детерминированные unit equivalents.

Не добавлять tests для пустых __init__, trivial dataclass assignment и той же константы, что implementation. Размер suite должен расти за счёт новых failure/provenance/resource contracts, а не filename/function coverage ради процента.

## Перечень прочитанных тестов

Полностью прочитаны: tests/test_ytdlp_service.py; tests/test_ytdlp_service_list_formats.py; tests/test_ytdlp_exceptions.py; tests/test_ytdlp_parsers.py; tests/test_ytdlp_url_helpers.py; tests/test_parsers_full.py; tests/test_parsers_filesize.py; tests/test_cookies_manager.py; tests/test_converter.py; tests/test_converter_service.py; tests/test_downloader_service.py; tests/test_gallery_dl_service.py; tests/test_sender_service.py; tests/test_slideshow_service.py; tests/test_tikwm_service.py; tests/test_tikwm_optimized.py; tests/test_tikwm_streaming_safety.py; tests/test_cobalt_service.py; tests/test_cobalt_streaming_safety.py; tests/test_pinterest_service.py; tests/test_instagram_service.py; tests/test_fast_path_opts.py; tests/test_video_meta.py; tests/test_tiktok_content_classification.py; tests/test_tiktok_content_router.py; tests/test_slideshow_detection.py; tests/test_professional_features.py; tests/test_fixes_verification.py; tests/test_property_based.py; tests/test_injection_prevention.py; tests/test_group_and_converter_extended.py; tests/test_slideshow_callbacks.py; tests/test_integration.py; tests/test_messages_errors.py; tests/conftest.py.

Прочитаны релевантные cross-area fragments, не заявляю полный аудит этих файлов: tests/test_pipeline_entrypoints.py:520-610 (command/callback adapter and no legacy fallback); tests/test_metrics.py:406-437 (converter workload); tests/media/providers/test_http_providers.py:747-813 (PinterestProvider); tests/media/providers/test_ytdlp_provider.py:1-200 и518-end (selector samples and real cookie-manager/CLI tests); tests/core/test_process_supervisor.py:317-342 (owned partial cleanup). Индексированы по импортам/names cross-area tests/test_bot_callbacks.py, test_bot_messages.py, test_bot_keyboards.py, test_group_logic.py, test_webhook_durability.py, media/test_proxy_routes.py; они принадлежат основному/другому агенту и здесь не отмечены как полностью прочитанные.

## Сохраняемый воспроизводитель

Ниже код offline7-observations; сохранить как .py при implementation task. Он не обращается в сеть, не запускает yt-dlp/ffmpeg и пишет только в TemporaryDirectory. Из root запускать указанным Python. Он печатает нарушенные postconditions; заменить prints на желаемые asserts при RED regression.

```python
import os
for name in ("MAX_MEDIA_FILE_MB", "MAX_DL_MB", "MAX_TG_UPLOAD_MB"):
    os.environ[name] = "2000"
import asyncio, json, pathlib, tempfile
from unittest.mock import AsyncMock, patch
from app.core import state
from app.core.models import DownloadContext
from app.core.process import ProcessResult
from app.services.cobalt import CobaltService, CobaltResult, CobaltPickerItem
from app.services.converter import split_video_stream_copy, compress_video_to_size
from app.services.orchestrator import extract_video_meta, DownloadOrchestrator
from app.services.ytdlp.service import YtDlpService
from app.services.ytdlp.parsers import classify_tiktok_error

class Response:
    def __init__(self, status):
        self.status_code, self.headers = status, {}
    async def aiter_content(self, chunk_size):
        yield b"image"
class Session:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def get(self, url, **kwargs):
        return Response(500 if "audio" in url else 200)
class Queue:
    async def enqueue(self, *args, **kwargs): return True
    def release(self): pass

async def main():
    with tempfile.TemporaryDirectory() as directory:
        folder = pathlib.Path(directory)
        with patch("app.services.cobalt.TEMP_DIR", directory), patch("app.services.cobalt.AsyncSession", Session):
            images, audio = await CobaltService.download_slideshow(CobaltResult(
                status="picker", picker=[CobaltPickerItem("photo", "https://example/photo")], audio="https://example/audio"))
            print("bad_audio", len(images), audio is not None, pathlib.Path(audio).exists() if audio else None)
        info = folder / "info.json"
        info.write_text(json.dumps(dict(duration=10, vcodec="h264", width=1920, height=1080, pix_fmt="yuv420p10le")))
        probe = AsyncMock(return_value=ProcessResult(0, json.dumps({"streams": [{"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p10le"}]}).encode(), b""))
        with patch("app.services.orchestrator.process_supervisor.run", probe):
            print("cached_meta", await extract_video_meta("actual.mp4", str(info)), "probes", probe.await_count)
        source = folder / "source.mp4"
        source.write_bytes(b"x" * 1000)
        async def split_process(command, **kwargs):
            for index in ("000", "001"):
                pathlib.Path(command[-1].replace("%03d", index)).write_bytes(b"x" * 300)
            return ProcessResult(0, b"", b"")
        with patch("app.services.converter.TEMP_DIR", directory), patch("app.services.converter._probe_full_meta", AsyncMock(return_value={"duration_s": 20, "audio_kbps": 128})), patch("app.services.converter._run_transform_process", split_process):
            parts = await split_video_stream_copy(str(source), segment_bytes=200)
            print("split_sizes", [pathlib.Path(part).stat().st_size for part in parts])
        state.conversion_sem = asyncio.Semaphore(1)
        source.write_bytes(b"x" * 1000000)
        async def compress_process(command, **kwargs):
            if command[-1].lower() != "nul": pathlib.Path(command[-1]).write_bytes(b"x" * 900000)
            return ProcessResult(0, b"", b"")
        with patch("app.services.converter.TEMP_DIR", directory), patch("app.services.converter._probe_full_meta", AsyncMock(return_value={"duration_s": 10, "audio_kbps": 128})), patch("app.services.converter._run_transform_process", compress_process):
            output = await compress_video_to_size(str(source), target_bytes=800000)
            print("compressed_size", pathlib.Path(output).stat().st_size)
        state.media_pipeline = None
        state.download_queue = Queue()
        state.api_queue = Queue()
        download = AsyncMock(return_value=(str(source), None))
        with patch("app.services.orchestrator.MediaSender.download_video", download), patch("app.services.orchestrator.ensure_telegram_compatible", AsyncMock(side_effect=lambda path, **kwargs: path)), patch("app.services.orchestrator.extract_video_meta", AsyncMock(return_value={})), patch("app.services.orchestrator.MediaSender.send_file", AsyncMock(return_value=True)):
            await DownloadOrchestrator.process_download("t", 1, AsyncMock(), DownloadContext(page_url="https://youtu.be/x", format_id="22", height=720, section="*10-20"), None, AsyncMock(), None)
        print("legacy_clip", download.await_args.kwargs.get("section"))
    print("vk_source", YtDlpService().build_command("https://m.vk.com/video-1_2", "best", 720, "out")[-1])
    print("status404", classify_tiktok_error("HTTP status code 404"))
asyncio.run(main())
```

Наблюдавшийся результат: bad_audio 1 True False; cached_meta h264 без pix_fmt, probes0; split_sizes[300,300]; compressed_size900000; legacy_clipNone; vk_source https://m.m.vk.com/video-1_2; status404 AUTH_REQUIRED.
