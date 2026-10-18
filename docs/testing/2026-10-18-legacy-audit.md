# Аудит legacy-кода и зависимостей, 18 октября 2026

Проверка относится к рабочему состоянию поверх
`063930561d05ca775802721c4b18bf6147ffbd2f`, включая существовавшие до начала
задачи незакоммиченные изменения. Исходное состояние сохранено отдельно:
`artifacts/legacy-audit-2026-10-18/baseline-source/`, список SHA-256 —
`baseline-source-hashes.json`. Пользовательские изменения не сбрасывались.

## Область анализа

Просмотрены все 76 исходных Python-модулей приложения, 25 167 строк исходного
состояния: API, bot handlers, core, хранилища, durable jobs, медиапайплайн,
провайдеры, транспорт, доставка, конвертеры и действующие старые адаптеры.
Отдельно проверены оба requirements-файла, pyproject, Dockerfile, оба Compose
файла, `.env.example`, три workflow, девять release/diagnostic/evidence scripts,
`tools/export_cookies.py`, семь корневых Python-утилит и старый deploy helper.

Для кандидатов на удаление прослежены импорты, re-export, атрибутные и ленивые
вызовы, тесты, workflow и действующие операционные документы. Исторические
отчёты не переписываются вслед за удалением описанных в них старых реализаций.
Субагенты аудита и независимого ревью использовали `gpt-6.1-sol`, reasoning
`high`: задачи затрагивали отмену, сериализованный кэш, receipts и release traps.

## Удалённый legacy

| Объект | Основание |
|---|---|
| `instaloader==4.15.3` | Нет потребителя в приложении, тестах и обслуживаемых инструментах; итоговая проверка выполняется в отдельном окружении без пакета |
| `state.tasks_sem` | Нет рабочих acquire/release или context-manager consumers; удаляется и устаревший fixture setup |
| `instagram.is_instagram_url` | Старый локальный URL-helper без рабочих вызовов |
| Orchestrator `_is_native_audio_container`, `_detect_opus_from_webm`, `_maybe_rename_webm_to_ogg` | Невостребованный старый обход конвертации |
| `MediaConverter.remux_webm_opus` | После трассировки обхода остаются только тесты самого неиспользуемого метода |
| Sender `_open_media` и `_TG_URL_DELIVERY_MAX_BYTES` | Невостребованные остатки фасада; настоящая доставка выполняется через общий delivery boundary |
| `DirectDownloadReady` | Исключение больше не выбрасывается и не перехватывается рабочими путями |
| Parser `_is_youtube`, `_is_facebook`, `_is_vk_audio`, `_is_vk_video`, `TikTokError`, `classify_tiktok_error` | Дублирующие старые классификаторы без потребителей |
| `benchmark.py`, `benchmark_tikwm.py`, `test_cobalt.py`, `test_pin.py`, `test_tikwm_api.py`, `test_tikwm_bvc2.py` | Одноразовые live-пробы/замеры, не вызываемые рабочими инструментами или CI |
| `deploy/setup_security.sh` | Старый неподключённый host-wide установщик, не соответствующий текущей ограниченной модели релиза |

Удалены тесты, проверявшие только перечисленные недостижимые helpers.
Действующие проверки форматов, MP3, провайдеров, отправки и callback-контрактов
сохраняются. Внутренний transport helper `_await_thread_io` переносится в общий
типизированный `app/core/asyncio_utils.py::await_owned`; его поведение не удалено.

## Что сохранено после трассировки

- `MediaSender`, `VideoDownloader`, orchestrator и остальные старые фасады:
  у них есть рабочие потребители в handlers, API и fallback-путях.
- Прежние callback shapes/action aliases, `DownloadContext`, dict/list-shaped
  сохранённые payloads и Redis JSON fallback: они нужны для уже выданных
  кнопок и данных, переживающих смену образа.
- `DownloadQueue.enqueue/release`, file cache, parsing state и semaphore:
  они продолжают обслуживать действующие запросы.
- Legacy media-limit aliases, Cobalt/session/cookie flags, `YOUTUBE_PIPE_MODE`,
  TikTok/VK proxy settings и WireGuard example: найдены реальные consumers.
- aria2: используется старым downloader/builder-путём внутри поддерживаемой
  конфигурации. Его возраст не является доказательством неиспользования.
- `ig_wizard.py`, cookie export, YouTube diagnostics, evidence collector,
  Docker evidence adapter, legacy evidence harness, bootstrap и rollback:
  это обслуживаемые инструменты и миграционные границы.
- Ключи пользовательских preferences и схемы SQLite receipts: смена написания
  или удаление миграционного кода потеряли бы сохранённые данные.

Версии используемых библиотек не обновляются только из-за наличия более новых
релизов. Изменение зависимостей ограничено доказанно ненужным прямым pin.

## Выявленные ошибки и проверяемое поведение

| Граница | Ошибка исходного пути | Поведение, которое закрепляют регрессии |
|---|---|---|
| Прямой album candidate | Selection `(2, 0)` проверяется после сокращения трёх элементов до двух и ошибочно отклоняется | Проверка исходных индексов до projection, точный порядок и повторное использование file IDs |
| GIF и mute animation | URL-only cache смешивает клип/качество; token-wide локальный файл может относиться к другому сообщению | Ключ по фактическому Telegram source identity; локальное повторное использование только после подтверждённой привязки к успешной отправке |
| GIF/cloud recovery | Безусловный `getFile` вместо сохранённого локального источника ломает допустимые cloud uploads размером 20–50 MB | Действительный локальный источник точного отправленного файла используется без повторной загрузки |
| Цепочка animation → native GIF | Успешно отправленная анимация получает кнопку экспорта, но её локальный файл сразу удаляется | Подтверждённый generated output переходит в существующий bounded file cache; следующий callback использует точную анимацию, expiry/eviction удаляют файл |
| GIF lifecycle | Download/debounce и ранние выходы находятся вне полного cleanup; ошибка или отмена отправки оставляет результат | Уникальные owned paths, блокировка до await и очистка на failure, cancellation и раннем выходе |
| Durable GIF | Новая идентичность может обойти прежний receipt после истечения link cache | Успешный или uncertain receipt того же single-media job запрещает replay; failed остаётся повторяемым |
| Slideshow callback | Неизвестный mode молча превращается в video | Неизвестные значения отклоняются до queue/provider/send; прежние допустимые формы сохраняются |
| Direct highlights | Producer пишет только список и теряет requested clip | Сериализованный envelope сохраняет `items` и `section` для single/all selection |
| Disk worker | Отмена asyncio-wrapper не останавливает уже начатую запись; close/remove может опередить поток | Caller сохраняет handles/paths до завершения owned operation, затем восстанавливается отмена |
| Потоки скачивания | Legacy пути допускают пустые, чрезмерные или неполные файлы; cancellation оставляет partial | Общий byte cap, проверка completeness и очистка до возврата; для compressed HTTP bodies учитывается декодированный объём |
| Carousel/slideshow | Ошибка одного элемента оставляет соседние загрузки или уже загруженные файлы | Соседняя работа отменяется и ожидается, неопубликованный результат удаляется целиком |
| Converter | Concat writer и audio-duration reader могут пережить владельца источника | Завершение owned worker предшествует удалению concat/source ownership |
| GIF converter outputs | Повторная конвертация перезаписывает прежний результат; failed/empty conversion оставляет partial | Уникальные пути результатов, очистка palette/output на всех неуспешных выходах, изоляция владельцев при повторном преобразовании |
| POSIX processes | Завершение лидера ошибочно принимается за завершение всей группы | Оставшиеся потомки получают kill до освобождения файлов; normal finish не оставляет descendants |
| Drain configuration | Неправильный float разбирается в shutdown `finally` и может пропустить cleanup | Finite nonnegative deadline проверяется до resources, shutdown использует захваченное значение; zero допустим |
| CI | Workflow передаёт другой webhook secret; Compose gate не создаёт требуемый `.env`; `bash -n file1 file2 ...` проверяет только первый скрипт | Реальный webhook test проходит под env каждого workflow; временный `.env` удаляется и при ошибке gate; оба workflow проверяют каждый из пяти release scripts без их выполнения |
| Release selection | Preflight очищает Compose overrides только в собственном процессе, activation наследует чужой выбор | Все standalone entrypoints используют проверенную конфигурацию проекта |
| Windows test harness | Startup попадает в behavior deadline; timeout может оставить MSYS descendants | Наблюдаемая readiness отдельно от прежних 2/5-second checks; owned test tree закрывается на watchdog |

Cloud download limit подтверждён отдельно от upload limit в официальных
[getFile](https://core.telegram.org/bots/api#getfile),
[sendVideo](https://core.telegram.org/bots/api#sendvideo) и
[sendAnimation](https://core.telegram.org/bots/api#sendanimation). Metadata mapping
локальных источников не владеет файлами, не продлевает leases и не создаёт копии:
срок жизни определяется прежним владельцем, stale path проверяется перед reuse.
Отдельно для новой успешно отправленной анимации callback явно передаёт свой
generated output в существующий `FileTTLCache` под уникальным ключом владельца.
Исходный token mapping сохраняется; failure, uncertainty и cancellation
оставляют файл у callback для обязательной очистки.

## Исходное измерение

Полный исходный offline-прогон в Python 3.12.14 завершился с
**1621 passed, 8 failed, 1 skipped, 5 deselected, 42 subtests passed**,
643 секунды, exit 1. Сохранены `baseline.log` и `baseline.xml`.

Пять падений относятся к подготовке/общему времени process fixtures, два — к
общим watchdogs shell fixtures; восьмое — aggregate, содержащий упавшие дочерние
проверки. Исходный Ruff и mypy были зелёными. Новые поведенческие RED-прогоны
хранятся отдельно и воспроизводят функциональные ошибки, которых прежние тесты
не покрывали. Исходный набор не называется зелёным.

В Windows-проверках сохраняются внутренние двухсекундные проверки отмены и
пятисекундная изолированная проверка singleflight. Startup/общий mock-script
watchdog — подготовка стенда, а не production deadline. Реальная задержка
MSYS-запуска учитывается отдельно; ожидание файла не заменяет проверку
освобождённого сокета, завершённого потомка или удалённого partial.

## Независимое ревью и сохранность исходной работы

Отдельный `gpt-6.1-sol` проверил diff относительно сохранённого рабочего
состояния, а не только Git HEAD. Найденная P2-регрессия cloud GIF подтверждена
отдельным воспроизведением, исправлена исходным implementer и повторно проверена
ревьюером. Дополнительная сквозная проверка обнаружила отдельную P2-регрессию
срока жизни generated animation между двумя callback. Она исправлена передачей
в существующий bounded owner, с уникальными путями обоих GIF converters и
очисткой partial outputs на неуспешных выходах. Оба замечания закрыты; финальное
ревью зафиксированной версии не содержит открытых actionable findings. Отчёт
сохранён в `artifacts/legacy-audit-2026-10-18/independent-review.md`.

Исправление привязки источника отдельно прошло 83 проверки в чистом окружении,
включая 21 новый сценарий. Запись metadata происходит только после
подтверждённого локального upload. Проверяются canonical path и
device/inode/size/mtime; успешная отправка по URL или сохранённому file ID не
создаёт выдуманную привязку к другому локальному файлу.

Финальная группа исправлений цепочки GIF и converter ownership прошла 137
профильных проверок. Семь новых chain cases проверяют оба реальных delivery
boundary и следующий callback, cloud/Local API recovery failure, expiry/eviction,
независимость повторных преобразований и failed/uncertain sends. Ещё десять
converter cases проверяют уникальность результатов и удаление файлов при
nonzero return, empty output, exception и cancellation. После последнего guard
для borrowed source повторно прошли 37 проверок ownership/callback.

Из 16 исходных изменённых/untracked файлов 14 сохраняются byte-for-byte.
В `docs/acceptance.md` добавлены новые контракты с сохранением прежнего блока.
В `tests/core/test_process_supervisor.py` уточнены отдельные setup fixtures;
существовавшая пользовательская правка child-ready/leader-exited handshake
сохранена и проверена сравнением AST соответствующей функции.

## Итоговая проверка

Итоговая среда — отдельный Python 3.12.14 virtualenv, установленный из текущих
`requirements.txt` и `requirements-ci.txt`. В нём нет `instaloader`;
`uv pip check` проверил 82 установленных пакета без конфликтов.

| Проверка | Результат |
|---|---|
| Полный offline pytest с branch coverage | **1711 passed, 3 skipped, 5 deselected, 42 subtests passed**, 478,17 секунды, exit 0 |
| Покрытие `app`, `scripts`, `tools` | **78,09%** суммарно с учётом ветвей; строки **81,25%**, ветви **68,09%**; порог 60% пройден |
| Последующая проверка CI-gate и workflow contracts | **20 passed**, 23,81 секунды, exit 0 |
| Ruff check | Passed для всего checkout |
| Ruff format check | **196 files already formatted** |
| mypy `app` | **77 source files**, no issues |
| Bash syntax | Все пять release scripts проверены по отдельности |
| Git attributes и реальные line endings | Для всех пяти release scripts LF; `*.sh text eol=lf` |
| `git diff --check` | Exit 0 |

Основной прогон выполнялся с изолированными test env values и отдельным
`COVERAGE_FILE` в ignored artifacts. Его команда:

```sh
python -m pytest tests -o addopts= -q -m 'not integration' --timeout=660 \
  --cov=app --cov=scripts --cov=tools --cov-branch --cov-fail-under=60 \
  --cov-report=term-missing:skip-covered \
  --cov-report=json:artifacts/legacy-audit-2026-10-18/coverage-final.json \
  --junitxml=artifacts/legacy-audit-2026-10-18/pytest-final.xml
```

После основного прогона исправлен только дополнительный CI-gate: два workflow,
два тестовых модуля, без изменений Python-кода приложения. Его 12 новых
сценариев дали **8 expected failures / 4 controls passed** до исправления, затем
все 20 затронутых CI/workflow checks прошли. Проверяется синтаксическая ошибка
по очереди в каждом release script и положительный контроль в обоих workflow;
`exit 37` в синтаксически корректных fixtures доказывает, что gate не выполняет
сами release scripts. Буквальная проверка прежней строки команды заменена этим
поведенческим покрытием. Независимое ревью этой группы также закрыто без
actionable findings.

`aggregate-source-hashes.json` фиксирует 191 Python-файл основного прогона;
`final-source-hashes.json` фиксирует итоговое состояние после CI-проверок.
Сравнение подтверждает неизменность всех 77 модулей приложения. Итоговый
`audit-changes.patch` сравнивает изменения задачи с сохранённым исходным рабочим
состоянием, поэтому не включает чужие первоначальные правки.

Финальный collection содержит 1726 выбранных тестов и пять исключённых
integration cases. Сверка каждого node ID с объединёнными JUnit основного и
CI-прогона не обнаружила непроверенных выбранных тестов. Три пропуска:
два реальных POSIX descendant/socket сценария и проверка POSIX executable bits,
недоступных на Windows. Сводное доказательство — `final-verification.json`.

В полном прогоне остаются три предупреждения библиотек: deprecation test-client
bridge Starlette/httpx и два Windows selector предупреждения `curl_cffi`.
`httpx` используется действующими HTTP-клиентами приложения; pins сохранены.
Автоматический selector bridge обслуживает Windows Proactor loop, необходимый
и для async subprocesses. Предупреждения не подавлялись.

Логи, JUnit, coverage JSON, RED/GREEN-проверки, SHA-256 manifests, отдельный
review и patch находятся в `artifacts/legacy-audit-2026-10-18/`.

## Дополнительная проверка при публикации

Перед публикацией полный offline-прогон в чистом Windows-окружении прошёл:
**1723 passed, 3 skipped, 5 deselected, 42 subtests passed**, 521,60 секунды;
branch/line coverage — **78,08%**. Доказательства сохранены в
`pytest-pre-push.log`, `pytest-pre-push.xml` и `coverage-pre-push.json`.

Первая проверка на Linux выявила отсутствовавший executable bit у трёх Python
CLI со shebang. Исправлены только Git modes у
`scripts/collect-media-release-evidence.py`,
`scripts/media-acceptance-docker-adapter.py` и `tools/export_cookies.py`.
Следующая проверка выявила восемь mypy-ошибок Windows ctypes API при Linux
target. Native Job Object helpers теперь отклоняют неподдерживаемую платформу
до загрузки DLL; шесть новых регрессий проверяют этот порядок без native calls.
Mypy проверяет оба target, Linux и Windows, в CI и pre-commit: все 77 модулей
приложения прошли обе проверки. Offline Media Acceptance на GitHub также прошёл
для `c694c6c` (run `39118796979`).

Полный Linux run `39118797011` пришлось отменить после потери прогресса на
pytest; runner не сохранил журналы, поэтому точная причина его остановки
не установлена. Независимый безопасный replay выявил опасный дефект старых
`tests/test_core_process.py`: оба теста проходили, передавая `AsyncMock.pid`
в незамоканный `os.killpg`. Преобразование PID давало группу `1`; replay
зарегистрировал TERM/KILL/KILL, не отправляя настоящие сигналы.

Исправленные fixtures задают целочисленный PID и изолируют POSIX signals,
Windows taskkill и создание Job Object. Оба сценария проверяют обе платформы
на любом host, включая реальные аргументы сигналов и Windows fallback.
Совместная проверка supervisor, platform boundaries и fixtures прошла:
**28 passed, 2 skipped, 4 subtests passed**, 9,70 секунды. Все восемь
pre-commit hooks прошли; независимое ревью не обнаружило открытых замечаний.
Доказательства — `mock-process-linux-unsafe-replay.log`,
`mock-process-pre-commit.log` и `mock-process-pre-commit-hooks.log`.

Полный повторный Linux-прогон
[run 39120732201](https://github.com/Le-Who/ytdlbot/actions/runs/39120732201)
прошёл на Python 3.12.15: **1728 passed, 4 skipped, 5 deselected,
46 subtests passed**, 101,81 секунды; суммарное branch/line coverage —
**77,57%**, строки — **80,75%**, ветви — **67,50%**. Итоговая коллекция после
platform regressions содержит 1732 выбранных теста и пять исключённых
integration cases. Четыре Linux-пропуска — настоящие Windows Job Object
сценарии, прошедшие локальную проверку на Windows. Обе настоящие POSIX
descendant/socket регрессии прошли на Linux.

В том же GitHub run прошли Ruff check, Ruff format для 198 файлов, оба mypy
target для 77 модулей, offline acceptance, настоящий
`docker compose config --quiet` и отдельный `bash -n` для всех пяти release
scripts. Live diagnostics остались выключены. Прогон выполнялся в отдельной
технической ветке: относительно `a62d669` изменён только workflow запуска
диагностики, все application/test/dependency bytes совпадают. Этот временный
workflow не входит в публикуемую ветку `vps`. Журналы и JUnit/coverage XML
сохранены в `github-linux-final.log` и `linux-final-evidence/`.

## Граница локальных доказательств

Проверка остаётся offline. В локальном Windows-окружении отсутствуют Docker,
Redis server, ffmpeg/ffprobe и WSL; соответствующие Linux-проверки отдельно
описаны выше. Stub Docker не считается запуском образа или проверкой mount/UID.
Настоящие POSIX descendant/socket tests на Windows пропускаются, при этом их
platform-boundary regressions исполняются. Real provider/Telegram requests,
production-IP acceptance и измерение production performance не входят в
проведённые проверки. Они не следуют из прохождения unit/contract suite.
