# Очистка Ruff/mypy и согласование версий — 9 октября 2026

## Область и исходная точка

Работа продолжает [полный аудит тестов](2026-10-09-test-audit.md). Исходный снимок для этой очистки — исправленное после аудита рабочее дерево, а не Git HEAD: 186 Python/config files сохранены с SHA-256 в artifacts/lint-cleanup/baseline-manifest.json. Предыдущие исправления и пользовательские исходные untracked файлы сохранены; коммитов и публикации нет.

Ruff 0.16.7 давал 580 замечаний в app/tests/scripts/tools и семи root Python diagnostics. После обновления и настройки Python 3.12 первоначальный JSON содержал 604 замечания; safe-fix проход сообщает 638 с учётом последующих обнаружений, 336 исправлений и 302 оставшихся замечаний. Это разные счётчики, их нельзя считать одним числом уникальных исходных дефектов. Mypy 2.3.1 и 2.4.0 на исходном снимке сообщали одинаковые 31 ошибку в шести файлах.

## Версии и воспроизводимость

| Инструмент | До | После |
|---|---|---|
| Ruff, рабочее окружение | 0.16.7 | 0.16.10 |
| Ruff hook | 0.9.10 | 0.16.10 |
| Mypy, рабочее окружение | 2.3.1 | 2.4.0 |
| Mypy hook | isolated mirror 1.14.1 без project dependencies | 2.4.0 из активированного project environment |
| Pre-commit runner | не закреплён в requirements-ci | 4.6.2 |
| Hygiene hooks | 5.0.0 | 6.0.0 |

Стабильные версии проверены по официальным [Ruff/PyPI](https://pypi.org/project/ruff/0.16.10/), [mypy/PyPI](https://pypi.org/project/mypy/2.4.0/), [pre-commit/PyPI](https://pypi.org/project/pre-commit/4.6.2/) и [pre-commit-hooks/PyPI](https://pypi.org/project/pre-commit-hooks/6.0.0/); используемые remote hook tags также проверены через Git. Runtime requirements.txt не изменён.

requirements-ci.txt закрепляет Ruff/mypy/pre-commit. Ruff hook остаётся remote с rev v0.16.10; mypy запускается через `python -m mypy` в активированном окружении с runtime dependencies, как в CI. Это устраняет отличие типов между изолированным hook и приложением. minimum_pre_commit_version требует runner 4.6.2 или новее. CI теперь запускает lint, formatter и mypy до pytest.

Сравнение `ruff check --show-settings` до/после: **413 активных правил совпадают точно; ни одно правило не отключено и не добавлено**. Python target задан как py312. Генерируемые artifacts/worktrees/SDD scratch и docs исключены из проверки Python-исходников; независимый reviewer подтвердил, что Ruff обнаруживает все 182 инвентаризированных Python-файла. Счётчик formatter отдельно сообщает 184 files; его значение не интерпретируется как число production modules.

## Что исправлено

- Уточнены реальные типы сообщений, кэша, коллекций, результатов и параметров; разделены bool, DeliveryReceipt и тексты ошибок. Устранено повторное использование имени exception binder вне его ветки. Избыточный unused mypy override tests.* удалён; активная проверка app не ослаблена.
- Выполнены safe import/annotation fixes и форматирование. Unsafe-fixes не применялись. Восстановлен Windows `getattr(proc, '_transport')` bridge, который механическая правка B009 превратила в доступ к отсутствующему в public stubs атрибуту; локальная причина сохранения dynamic access записана в коде.
- Исправлена работа legacy TikTok fallback с BytesIO: path-only операции и file_cache применяются к строковым путям; тот же открытый буфер доходит до sender. Два теста реальных private/group handlers воспроизвели ошибки до правок.
- Exporter ловит ожидаемые subprocess/OS/encoding ошибки; неожиданные programmer assertions больше не скрываются. Сохранены user-visible unavailable outcomes, synthetic isolation и clipboard timeout kill/wait.
- Temporary test handles закрываются до передачи файлов runtime; mutable response headers принадлежат экземпляру. Root TikWM diagnostic выполняет синхронный file/ffprobe inspection после закрытия asynchronous session/event loop; реальная диагностика не запускалась.

## Подтверждённые ошибки durable boundary

При независимом ревью сохранённых широких catches обнаружены четыре унаследованных места, где SQLite receipt failure скрывался. Все воспроизведены на исправленном до lint снимке и текущем коде; они не вызваны обновлением инструментов.

| Граница | Исправление | Независимый oracle |
|---|---|---|
| Cached GIF | Durable helper вынесен из recovery; регенерация только после explicit FAILED | Raw SQLite error наружу, нет source recovery/conversion; один send, stored UNCERTAIN |
| Generated GIF | Исключения durable helper проходят через recovery fence | Raw error наружу, owned GIF удалён, исходник сохранён, debounce очищен |
| Authorized Instagram | False API сохранён; неожиданный error отмечен в current execution | Реальные MediaPipeline/TelegramDelivery/SQLite; оригинальный error object в execution.error, одна отправка/release |
| Legacy X | Durable sender exceptions не превращаются в provider fallback; own output чистится в finally | Direct/local/picker варианты; real sender/delivery/SQLite; pre-send provider failures по-прежнему False |

Во всех четырёх группах проверена реальная UNCERTAIN/unsafe reservation. Оба GIF-теста дополнительно вызывают настоящий helper повторно и доказывают отсутствие новой отправки; для IG/X отдельный replay не заявляется. **Повторная доставка не была доказана и не заявляется**; исправлены скрытие ошибки и ошибочный recovery/fallback. Регрессии fault only `_finalize_delivery_attempt_sync` настоящего JobStore; внешние provider/Telegram boundaries синтетические. Existing BadRequest regeneration и NetworkError uncertainty controls сохранены.

## Осознанно сохранённые границы

Каждый retained BLE001/S110 catch рассмотрен по своему фактическому scope. Optional Telegram UI, extensible provider/cache adapters, redacted diagnostic protocol и cleanup после подтверждённой отправки сохраняют ограниченную обработку ошибок с локальным rationale. Критические durable calls отделены от этих catches. Глобальные suppression и новые type: ignore не добавлялись.

Небольшое синтетическое test I/O и отдельные legacy runtime file operations сохранены синхронными с точной причиной: текущая lexical ownership должна переживать cancellation и cleanup. Это не обещание отсутствия event-loop latency. Безопасный переход к offload для этих runtime мест требует отдельной проверки started-thread lifetime/ownership, а не простой замены на to_thread.

Обновлённые hygiene hooks исправили только trailing whitespace/EOF в CHANGELOG.md, deploy/setup_security.sh, .cursor/skills/core/SKILL.md, debug_ig.json, app/tasks/__init__.py, .dockerignore. Skill instruction text не изменялся; shell changes только whitespace, без выполнения deployment/security scripts.

## Итоговая проверка

**Окончательный полный офлайн-прогон: 1597 passed, 42 subtests passed, 1 skipped, 5 deselected, 0 failures/errors; 208,48s, exit 0.** Единственный skip — POSIX executable mode на Windows. Пять integration cases исключены явно. Четыре исходных предупреждения относятся к FastAPI/Starlette dependencies и curl_cffi Windows Proactor selector thread; предупреждения не скрыты. JUnit содержит 1598 testcase elements; tests counter 1640 включает subtests.

| Проверка окончательного кода | Результат |
|---|---|
| Ruff check . --no-cache | All checks passed |
| Ruff format --check . | 184 files already formatted |
| Mypy app/ --no-incremental | No issues in 76 source files; 31 → 0 |
| Реальные pre-commit hooks | Все семь PASS; явно перечислены tracked files и новые untracked tests |
| Независимые scoped reviews | SPEC/QUALITY PASS; итоговое bot re-review 194 focused tests |
| Независимое общее ревью | SPEC/QUALITY PASS, без открытых actionable findings |
| Source/config/hygiene fingerprint | Все 192 hashes совпадают после полного прогона |
| Shell hygiene | deploy/setup_security.sh bash -n PASS, script не выполнялся |
| Diff hygiene | git diff --check PASS |

All original 3238 AST assert/mock oracles сохранены; текущий код содержит 3309. Новые type-ignore tokens не добавлены. Независимое bot re-review загрузило frozen handlers только в памяти: все шесть новых durable fault instances упали по исходной причине; current regressions и controls затем прошли 11 tests. Support review отдельно повторило unexpected exporter errors и два BytesIO случая на frozen/current коде. Это доказательства чувствительности выбранных regressions, не общий mutation score.

| Область итогового coverage | Строки/statements | Ветви |
|---|---|---|
| App, включая main.py | 9814/12202 = 80,43% | 2561/3780 = 67,75% |
| Scripts/tools | 1004/1595 = 62,95% | 297/598 = 49,67% |
| Вся измеренная область | 10818/13797 = 78,41% | 2858/4378 = 65,28% |

Совместное coverage 75,25%, исходный порог 60% сохранён. Проценты отражают изменившийся код и исполнение, а не доказательство корректности. [Карта покрытия](2026-10-09-coverage-matrix.csv) сохраняет baseline/final значения предыдущего аудита и отдельные lint_* поля текущего прогона. [Независимое итоговое заключение](2026-10-09-lint-cleanup-review.md) сохраняет происхождение и закрытие найденных замечаний.

Raw evidence: artifacts/lint-cleanup/{ruff-final.log,format-final.log,mypy-final.log,precommit-final.log,pytest-final.log,pytest-final.xml,coverage-final.json,coverage-final.xml,measurement-summary.json,final-source-snapshot.json}. Scoped reports и review history: .superpowers/sdd/2026-10-09-lint-cleanup/.

## Повторение проверок

В активированном Python 3.12 окружении с requirements.txt и requirements-ci.txt:

```sh
python -m ruff check . --no-cache
python -m ruff format --check .
python -m mypy app/ --no-incremental
python -m pre_commit run --all-files
python -m pytest tests -m 'not integration' --timeout=60
```

Для ещё не добавленных в Git файлов parent отдельно запускал hooks через --files с полным source списком. `install-hooks` создал только изолированные environments в artifacts/lint-cleanup/precommit-cache; Git hooks/index не изменялись. На используемом uv-managed окружении packages установлены через `uv pip --python ...`, поскольку pip module в нём отсутствовал.

Live Redis/FFmpeg/Linux container/provider/browser/Telegram layers остаются за пределами этого офлайн-прогона. Real local SQLite, child-process/curl/socket component tests входят в suite. Clipboard/browser cookies и production не использовались.
