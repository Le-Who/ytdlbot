# Приоритетное расширение покрытия — 18 октября 2026

Продолжение [аудита покрытия](2026-10-09-test-audit.md) и
[последующей проверки Ruff/mypy](2026-10-09-lint-cleanup.md).
Выбраны оставшиеся P2-сценарии с риском утечки ресурсов, потери параметров
запроса или ошибочной повторной доставки. Изменения ограничены существующими
HTTP, readiness, SQLite и Instagram boundaries; архитектура и зависимости
не менялись. Работа сохранена локально без коммитов, публикации и deployment.

## Новые проверки и исправления

Добавлено **32 параметризованных исполнения тестов** в четырёх файлах:

| Файл тестов | Сценарии | RED до исправления |
|---|---|---|
| `tests/core/test_job_store_transactions.py` | Реальная SQLite writer-блокировка; commit failure admission/receipt; атомарный rollback album reservation; повторное открытие store | 1 failed, 4 passed: не закрывалось соединение после неудачного `BEGIN IMMEDIATE` |
| `tests/test_readiness_lifecycle.py` | Ошибка одного durable probe; timeout/cancel; shared Local Bot API probe; freshness от момента завершения; failure cache/recovery; редактирование чувствительных ошибок | 1 failed, 5 passed: ответ readiness возвращался с работающими sibling probes |
| `tests/test_download_stream_lifecycle.py` | ASGI 2.3/2.4 disconnect до body и во время body; отмена; cleanup materialized/fallback; failed subprocess EOF; GIF relay завершение | 10 failed: lease/generator ownership, игнорирование nonzero exit и не дожидавшийся завершения relay |
| `tests/test_ig_callback_chains.py` | Сериализованный profile/menu/selection; story/highlight single/all; точный order/clip/auth; legacy list cache; malformed timestamp; partial receipt replay | 6 failed, 5 passed: два случая потери clip и четыре необработанных timestamp |

Исправлены семь групп воспроизведённых дефектов:

1. HTTP response теперь закрывает принадлежащий ему async iterator и освобождает
   materialization даже при disconnect до первого body chunk. Cleanup остаётся
   однократным и дожидается завершения при отмене.
2. Nonzero exit downloader/ffmpeg больше не выглядит успешным концом HTTP stream.
   Ошибка после отправки headers завершает stream; она не меняет уже отправленный
   HTTP status в JSON-ответ.
3. GIF relay отменяется с ожиданием завершения. Намеренный 60-секундный cutoff
   ffmpeg сохраняется: код не ждёт бесконечно непрочитанный ещё работающий source.
4. После ошибки durable readiness отменяются и ожидаются sibling async probes.
   Отмена одного HTTP caller не отменяет общий Local Bot API probe.
5. Если SQLite не смогла начать immediate transaction, connection закрывается
   до передачи ошибки вызывающему коду. Новые проверки также подтверждают
   атомарность reservation и сохранение uncertainty при неудачном receipt commit.
6. Highlight selection сохраняет исходный clip через новый cache envelope
   `{items, section}`. Readers продолжают принимать прежние list-shaped entries.
7. Неправильные timestamps в cached story/highlight завершают callback существующим
   сообщением об истёкшем кэше до materialization или отправки выбранных items.

Тесты проходят через реальные callback decoders, сериализацию msgpack,
`MediaPipeline`, `TelegramDelivery` и SQLite. Сеть, Telegram API и преобразование
медиа заменены управляемыми boundaries. SQLite transaction tests используют
настоящую WAL-базу, реальную writer-блокировку и SQL trigger; commit fault
вводится на connection boundary.

## Проверка

Сфокусированные GREEN-прогоны: **30 SQLite**, **20 readiness**,
**64 HTTP/entrypoint** и **66 Instagram/entrypoint** tests. Наборы пересекаются;
их нельзя складывать как число уникальных тестов. Исходные RED-результаты и
точные команды сохранены в `artifacts/test-coverage-2026-10-18/`.

Независимый ревьюер `gpt-6.1-sol` с reasoning `xhigh` проверил три production
diff и четыре новых test files: **нет Critical/Important findings**, спецификация
и качество соответствуют заявленной offline-границе. Три независимые области
разрабатывались `gpt-6.1-sol` с reasoning `high`.

Полный прогон с coverage: **1629 passed, 42 subtests passed, 1 skipped,
5 deselected, 4 warnings, 0 failures/errors; 637,60s, exit 0**. Он включает
настоящий aggregate offline acceptance, который сам запускает выбранные
release-critical regressions. Отдельно **32 новых исполнения прошли в обратном
порядке**. Полный Ruff и mypy прошли; mypy проверил все 76 source files.
Format check: 190 Python files already formatted.

Единственный skip — POSIX executable-mode check на Windows. Пять integration
cases исключены явно. Четыре предупреждения сохранены: два FastAPI/Starlette
deprecations и два curl_cffi уведомления о selector thread на Windows Proactor.

Перед coverage-прогоном сохранены SHA-256 для 179 Python source/test files;
после завершения все 179 совпали. Уточнение setup одного существующего
Windows-теста выполнено после этого snapshot; production sources не менялись.
Повторная проверка после test-only refinement записана ниже отдельно.

| Измеренная область | Baseline строки | Итог строки | Baseline ветви | Итог ветви |
|---|---|---|---|---|
| Весь `app/`, включая `main.py` | 9814/12202 = 80,43% | 10045/12266 = 81,89% | 2562/3780 = 67,78% | 2631/3796 = 69,31% |
| `scripts/` + `tools/` | 1004/1595 = 62,95% | 1004/1595 = 62,95% | 297/598 = 49,67% | 297/598 = 49,67% |
| Вся измеренная область | 10818/13797 = 78,41% | 11049/13861 = 79,71% | 2859/4378 = 65,30% | 2928/4394 = 66,64% |

Совместное statement/branch coverage всей области: **75,25% → 76,57%**.
Порог 60% сохранён. Denominator изменился из-за минимальных исправлений;
проценты не означают полноту behavioral verification.

| Изменённый модуль | Строки baseline → итог | Ветви baseline → итог |
|---|---|---|
| `app/api/routes.py` | 79,53% → 91,37% | 64,63% → 77,55% |
| `app/bot/ig_callbacks.py` | 30,83% → 68,10% | 14,55% → 54,55% |
| `app/core/job_store.py` | 93,70% → 93,97% | 73,58% → 74,53% |

Первый baseline запуск с общим `--timeout=60` был прерван timeout-плагином на
ожидании вложенного pytest в aggregate acceptance test. Он не считается
успешным baseline. Повторный baseline выполнен из `git archive` неизменного
HEAD `063930561d05ca775802721c4b18bf6147ffbd2f`; final — на текущем коде,
оба с `--timeout=180` и одинаковым offline selection. Архивный baseline:
**1596 passed, 1 failed, 42 subtests passed, 1 skipped, 5 deselected; 847,19s**.
Неуспешный baseline сохранён как измерение и не назван зелёным.

Его единственное падение —
`test_windows_job_kills_child_after_group_leader_exits`: startup helper не
дождался `child-ready` за одну секунду. Проверки отмены и освобождения сокета
не начинались. Тот же тест прошёл в итоговом полном наборе, отдельно без coverage
и в наборе всех 16 process-supervisor tests с изолированным coverage. Это
не доказательство production process-supervision defect. Test-only setup
уточнён: наблюдаемый child-ready/leader-exited handshake, гонка с premature
owned-task completion и диагностический startup watchdog. Двухсекундный предел
отмены и немедленная проверка socket bind сохранены.

Уточнённый setup прошёл все 16 process-supervisor tests с отдельным
`COVERAGE_FILE` (**17,51s, exit 0**); независимое scoped review не нашло
actionable findings. После coverage snapshot изменился только этот test file,
что подтверждено сравнением SHA-256. Итоговый повтор полного suite без coverage
после этого уточнения: **1629 passed, 42 subtests passed, 1 skipped,
5 deselected, 4 warnings, 0 failures/errors; 357,38s, exit 0**. Все 179 Python
source/test files совпали с snapshot повторного прогона. Оба JUnit содержат
по 1630 testcase elements (1629 passed + 1 skipped), без failures/errors;
счётчик subtests указан отдельно. Все 106 строк CSV и прежние
baseline/final/lint колонки проверены на сохранность. Пользовательские исходные
untracked files сохранены. Итоги проверки находятся в
`artifacts/test-coverage-2026-10-18/verification-summary.json`.

Воспроизводимые основные команды в Python 3.12 окружении с закреплёнными
requirements (фактический Python 3.12.14, coverage 7.16.1):

```powershell
$env:BOT_TOKEN='test_token'
$env:WEBHOOK_URL='https://example.com'
$env:TELEGRAM_SECRET_TOKEN='test-secret'
$env:MAX_MEDIA_FILE_MB='2000'
$env:MAX_DL_MB='2000'
$env:MAX_TG_UPLOAD_MB='2000'
python -m pytest tests -o addopts= -q -m 'not integration' --timeout=180 `
  --cov=app --cov=scripts --cov=tools --cov-branch --cov-fail-under=60 `
  --cov-report=json:artifacts/test-coverage-2026-10-18/coverage-final.json
python -m pytest tests -o addopts= -q -m 'not integration' --timeout=180 --no-cov
python -m ruff check . --no-cache
python -m ruff format --check .
python -m mypy app --no-incremental
```

Сырые результаты и проверяемое происхождение:
`artifacts/test-coverage-2026-10-18/{pytest-final.log,pytest-final.xml,coverage-final.json,
pytest-baseline-archive.log,coverage-baseline.json,measurement-summary.json,
source-snapshot.json,reverse-new.log,independent-review.md}`.

## Границы доказательств и оставшиеся приоритеты

- Readiness проверяет settlement async futures. Отмена `asyncio.to_thread`
  не останавливает уже работающий SQLite thread; тесты не доказывают его
  синхронное завершение. Реальный `write_probe` ограничивает SQLite busy timeout
  500 миллисекундами.
- Instagram clip проверен как точный вход в pipeline. Playable output и
  фактическая длительность после ffmpeg здесь не проверяются. Сохранено прежнее
  поведение legacy downloader при отсутствии общего pipeline.
- Partial/uncertain delivery regression доказывает отсутствие повторной отправки
  при повторном callback внутри **того же durable job**. Это не глобальная
  дедупликация независимых пользовательских запросов.
- Не проверены real Redis Lua/TTL/ownership, настоящий FFmpeg/ffprobe output,
  Linux descendants/flock/Compose UID/mount permissions: Redis, ffmpeg, ffprobe
  и Docker отсутствуют в PATH, WSL не установлен. Эти P2-runtime слои остаются
  следующими при наличии подходящей среды.
- Успешный полный lifespan с fake Telegram и настоящим worker, дополнительные
  ingress/DB fault windows, provider malformed numeric fields и config matrix
  остаются отдельными сценариями. Этот этап не закрывает все исходные P2/P3.
- Live providers, реальные Instagram sessions/cookies, production-IP 24-link run,
  настоящая Telegram delivery и performance acceptance не запускались.

Уточнённый постоянный контракт находится в
[offline acceptance](../acceptance.md#offline-acceptance).
Исторические baseline/final/lint поля
[CSV-карты](2026-10-09-coverage-matrix.csv) сохраняются; новые измерения
добавляются отдельно.
