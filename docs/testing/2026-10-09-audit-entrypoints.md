# Входные точки, обработчики и объективность тестов

Исходный HEAD: cc6e06edc1ad699c6540aa85ede771c53429bd9c. Дата: 9 октября 2026. Основной агент прочитал полностью перечисленные исходники; ядро, сервисы, медиапайплайн и инфраструктуру параллельно прочитали независимые аудиторы. Это карта контракта, а не утверждение о production.

| Исходник | Назначение/риск | Проверки | Остаточные сценарии |
|---|---|---|---|
| app/__init__.py | Пустой маркер пакета | Импорты приложения | Отдельный тест не нужен |
| app/api/__init__.py | Пустой маркер пакета | Импорты API | Отдельный тест не нужен |
| app/bot/__init__.py | Пустой маркер пакета | Импорты обработчиков | Отдельный тест не нужен |
| app/constants.py | Домены, идентификаторы форматов и размер чтения | utils, injection, parsers, pipeline consumers | Проверять поведение потребителей; не дублировать буквальные константы |
| app/main.py | Запуск, регистрация Telegram-обработчиков, durable worker, shutdown, error handler, middleware | test_webhook_durability, test_main_utils, test_global_error_handler, test_security_headers | Полный successful lifespan с настоящим worker и fake Telegram, отказ каждой стадии инициализации; Redis/deferred close; polling durability отличается от webhook |
| app/api/routes.py | Readiness, metrics, authenticated durable webhook, HTTP lease/stream | test_health_readiness, test_webhook_durability, test_webhook_security, test_security, test_security_main, test_pipeline_entrypoints, test_endpoints | Disconnect до начала body, fallback pipe/ffmpeg nonzero, поздние stream failures, concurrency readiness cancellation, trusted forwarded-IP contract |
| app/bot/commands.py | Command download и сохранение preferences | test_media_retry, test_pipeline_entrypoints, test_bot_boundary_contracts | start/help HTML и placeholders, missing/replied URL, лимит до chat action, отказ status reply, send partial/uncertain |
| app/bot/format_formatter.py | Отображение формата/размера/HLS | test_ytdlp_parsers, test_property_based, test_integration | Границы размера и nullable protocol через настоящий consumer; не тестировать getter ради строки |
| app/bot/keyboards.py | Ограниченные кнопки, индекс→format, versioned callback | test_bot_keyboards, test_pipeline_entrypoints, slideshow/retry consumers | special_index и нечётные/нулевые форматы с настоящим FormatItem, callback64-byte boundary |
| app/bot/callbacks.py | Picker, cancel owner, send, GIF/slideshow, durable GIF receipt | test_bot_callbacks, test_callbacks_exceptions, test_slideshow_callbacks, test_webhook_durability, test_pipeline_entrypoints | GIF temp source при раннем отказе размера, cancellation между conversion и send/cleanup, malformed mode, copy through Redis dict, partial album cleanup |
| app/bot/messages.py | Private pipeline, preferences, legacy parse dedup/cache, IG/X routing | test_bot_messages, test_messages_errors, test_logic_stability, test_media_retry, test_pipeline_entrypoints | Actual legacy TikTok/Cobalt send failure cleanup, concurrent parse failure/cancel waiter, IG profile/highlight menus and large callback payloads |
| app/bot/group_logic.py | Passive group admission, pipeline/legacy paths, slideshow callback | test_group_logic, test_group_and_converter_extended, test_media_retry, test_pipeline_entrypoints | Group origin binding, failed UI before acquired queue, cancellation around delivery/cache, actual mixed legacy receipt |
| app/bot/ig_callbacks.py | Authorized story/highlight candidate, selection menus and delivery | test_pipeline_entrypoints, test_bot_boundary_contracts | Existing coverage originally9% combined; successful menu→selection→delivery serialization, highlights clip context, malformed cached timestamp, all-item partial receipts |
| app/bot/retry.py | Owner-bound snapshot, safe retries, durable polling tombstone | test_media_retry | Discovery slideshow retry, UI failure with durable predecessor, Redis SET failure and expiry, process-restart same-token concurrency |

## Проверка качества

Проверены полностью тестовые файлы bot_callbacks, bot_messages, bot_keyboards, callbacks_exceptions, messages_errors, group_logic, group_and_converter_extended, slideshow_callbacks, media_retry, webhook_durability, webhook_security, health_readiness, endpoints, security, security_main, security_headers, security_html_injection, injection_prevention, logic_stability, new_features, main_utils, global_error_handler, integration, integration_real. Сервисные части некоторых файлов дополнительно проверил аудитор services. Полностью прочитаны root diagnostics test_cobalt.py, test_pin.py, test_tikwm_api.py, test_tikwm_bvc2.py, benchmark.py, benchmark_tikwm.py, ig_wizard.py; они не собираются pytest из-за testpaths=tests и не запускались с сетью/пользовательскими cookies. .pre-commit-config.yaml и .dockerignore просмотрены как конфигурационные контракты.

Обнаружены и заменены:

- test_security.py: копия hmac-условия вместо реального admission route.
- test_security_main.py: копия regex вместо Content-Disposition настоящего ответа.
- test_security_html_injection.py: stdlib html.escape и локально собранный caption вместо callback.
- test_logic_stability.py: simulated_on_message вместо реального on_message.
- test_new_features.py: буквальный local set вместо выбора очереди в DownloadOrchestrator.
- test_injection_prevention.py: условные assertions при неустановленном факте вызова процесса.
- test_bot_callbacks.py: тест progress exception не вводил исключение; теперь вводит ошибку редактирования и проверяет продолжение доставки.
- test_janitor_service.py: заранее установленный stop проверял ноль циклов и подменял другую функцию; теперь исполняется настоящий цикл. Добавлен disk-state/alert/recovery contract.

Новыми проверками найдено и исправлено: on_ig_download/on_ig_download_all не передавали сохранённый payload.section в authorized pipeline. Правильный RED:2 failed/14 passed; после минимального forwarding GREEN:16 passed.

## Доказательства и пределы

Исходные focused bot tests:86 passed; reverse-order bot+entrypoints:124 passed. Поэтому общая утечка state.* отмечена как статический риск, а не воспроизведённая ошибка порядка. Повсеместный autouse state reset не добавлялся: он изменил бы модель тестирования без доказательства нужного исправления.

После замен:94 passed/5 subtests в наборе границ; janitor:10 passed. Изолированные in-memory faults вызвали ожидаемые падения новых tests: auth bypass3, raw caption6, unclean filename3, no gallery invocation1, broken actual-handler dedup1. Production-файлы и установленные библиотеки при подменах не менялись. Это ограниченные проверки чувствительности, не процент mutation coverage всей базы.

Типизация и lint проверяются отдельно от pytest. Полный mypy исходного HEAD имеет31 ошибку в6 файлах. Возникшие4 ошибки типизации от изменений устранены; промежуточный повтор снова31/6. Не считать общий mypy зелёным, пока исходные проблемы не исправлены. Сохраняется разделение unit/contract, local component integration и unexecuted provider/production acceptance.
