"""
Centralized UI text constants for i18n.

All user-facing Russian strings are collected here.
To localize the bot, replace these values or implement a lookup system.
"""

__all__ = ["Texts"]


class Texts:
    # ── Commands (/start, /help) ──────────────────────────────────
    WELCOME = (
        "👋 <b>Привет! Я помогу скачать видео.</b>\n\n"
        "Просто отправь мне ссылку с:\n"
        "• YouTube\n"
        "• TikTok\n"
        "• VK / VK Video\n"
        "• RuTube (включая Shorts)\n"
        "• Facebook\n"
        "• Pinterest\n"
        "• Instagram (истории, хайлайты, посты)\n\n"
        "<i>Я найду доступные форматы и отправлю видео прямо сюда.</i>"
    )

    HELP = (
        "ℹ️ <b>Справка по боту</b>\n\n"
        "<b>Поддерживаемые сервисы:</b>\n"
        "• {platforms}\n\n"
        "<b>Инструкция:</b>\n"
        "1. Скопируйте ссылку на видео.\n"
        "2. Отправьте ссылку боту.\n"
        "3. Выберите качество кнопок под сообщением.\n"
        "4. Выберите: получить ссылку или файл в Telegram.\n\n"
        "❗️ <i>Если файл > {max_tg_mb} МБ, он не сможет быть загружен в Telegram "
        "(ограничение API). Используйте прямую ссылку.</i>\n"
        "📦 Максимальный размер для прямой загрузки: {max_dl_mb} МБ."
    )

    # ── Messages (on_message handler) ─────────────────────────────
    URL_NOT_SUPPORTED = "❌ Ссылка не поддерживается. Проверьте список доступных платформ с помощью команды /help."
    RATE_LIMITED = "⏳ Слишком часто. Пожалуйста, подождите минуту и попробуйте снова."
    SEARCHING = "🔎 Ищу видео..."
    TIMEOUT_RETRY = (
        "⏳ Время ожидания истекло. Сервер перегружен, пожалуйста, попробуйте еще раз."
    )
    CMD_MP3_USAGE = (
        "🎵 Использование: <code>/mp3 &lt;ссылка&gt;</code>\n"
        "Или ответьте этой командой на сообщение со ссылкой."
    )
    CMD_MP4_USAGE = (
        "🎬 Использование: <code>/mp4 &lt;ссылка&gt;</code>\n"
        "Или ответьте этой командой на сообщение со ссылкой."
    )
    CMD_FAST_DL_START = "⏳ Скачиваю..."
    SETTINGS_HEADER = (
        "⚙️ <b>Ваши настройки</b>\n\n"
        "• Формат: <b>{fmt}</b>\n"
        "• Качество: <b>{quality}</b>\n\n"
        "<i>Команды для изменения:</i>\n"
        "/setformat video|audio\n"
        "/setquality best|1080|720|480|360\n"
        "/settings reset — сбросить настройки"
    )
    SETTINGS_FMT_SET = "✅ Формат по умолчанию установлен: <b>{fmt}</b>"
    SETTINGS_QUALITY_SET = "✅ Качество по умолчанию установлено: <b>{quality}</b>"
    SETTINGS_RESET = "✅ Настройки сброшены."
    SETTINGS_INVALID_FMT = "❌ Неверный формат. Используйте: <code>video</code> или <code>audio</code>."
    SETTINGS_INVALID_QUALITY = "❌ Неверное качество. Используйте: <code>best</code>, <code>1080</code>, <code>720</code>, <code>480</code>, <code>360</code>."
    FETCH_ERROR_RETRY = "⚠️ Ошибка при получении данных. Пожалуйста, проверьте ссылку и попробуйте еще раз."
    TIMEOUT_UNAVAILABLE = "❌ Время ожидания истекло. Сервис недоступен."
    ACCESS_DENIED = (
        "❌ Доступ запрещен. Контент может быть приватным или требуется авторизация."
    )
    VIDEO_NOT_FOUND = "❌ Видео не найдено. Проверьте правильность ссылки."
    LIVE_NOT_SUPPORTED = "❌ Прямые трансляции (Live) не поддерживаются."
    PINTEREST_ERROR = "❌ Ошибка загрузки с Pinterest. Попробуйте позже или используйте прямую ссылку на видео."
    GENERIC_ERROR = "❌ Ошибка: {detail}"

    # ── Callbacks ─────────────────────────────────────────────────
    CACHE_EXPIRED_RESEND = "⚠️ Данные устарели. Пожалуйста, отправьте ссылку заново."
    CACHE_REFRESHING = "⏳ Кэш истек. Обновляю данные..."
    CACHE_REFRESH_FAIL = "⚠️ Ошибка обновления данных. Отправьте ссылку заново."
    PREPARING_LINK = "⏳ Подготовка ссылки..."
    DATA_EXPIRED_RESEND = (
        "⚠️ Данные устарели. Пожалуйста, отправьте ссылку на видео еще раз."
    )
    BTN_DOWNLOAD_LINK = "📥 Скачать (Ссылка)"
    BTN_SEND_TG = "📤 Отправить файл в TG"
    BTN_BACK = "🔙 Назад"
    READY_LINK = "✅ <b>Готово{quality}</b>\n🔗 Ссылка ({ttl} мин):\n{link}"
    CANCELLING = "🚫 Отменяю..."
    CANCELLED = "❌ Загрузка отменена пользователем."
    DOWNLOAD_STARTED = "🚀 Загрузка началась"
    TOO_MANY_REQUESTS = "⚠️ Слишком много запросов. Подождите немного."
    LINK_EXPIRED = "⚠️ Ссылка устарела."
    QUEUE_FULL = "⚠️ Очередь переполнена. Скачайте по ссылке."
    QUEUE_POSITION = "⏳ Вы #{pos} в очереди (из {total}). Ожидайте ~{eta}..."
    QUEUE_TIMEOUT = "❌ Время ожидания в очереди истекло. Попробуйте позже."
    MAINTENANCE_MODE = "🔧 Сервер обслуживается (мало места на диске). Попробуйте через несколько минут."
    FILE_TOO_BIG = (
        "⚠️ Файл слишком большой (~{size_mb:.1f} МБ).\n"
        "Telegram Bot API не позволяет отправлять файлы больше {max_mb} МБ.\n"
        "Пожалуйста, используйте прямую ссылку ниже."
    )
    STARTING_DOWNLOAD = "⏳ Начинаю загрузку..."
    GENERIC_ERROR_SHORT = "⚠️ Ошибка."
    SENDING_TO_TG = "📤 Отправляю в Telegram..."
    SEND_ERROR = "⚠️ Ошибка при отправке файла."
    # Progress threshold bar (phase 1)
    # bar: filled/empty blocks, pct: 0-100, eta: ETA string or empty string
    DOWNLOAD_PROGRESS = "⬇️ Загрузка {pct}%\n{bar}\n{eta}"
    BTN_CANCEL_DOWNLOAD = "✖️ Отмена"
    DOWNLOAD_CLIP_HINT = (
        "⚡️ Совет: отправьте ссылку с таймкодом для нарезки фрагмента:\n"
        "<code>ссылка 01:20 01:35</code>"
    )
    GIF_CONVERTING = "⏳ Конвертирую в GIF..."
    GIF_DOWNLOAD_CANCEL = "❌ Загрузка отменена."
    GIF_FILE_EXPIRED = "⚠️ Файл не найден или устарел."
    GIF_ALREADY_IN_PROGRESS = "⏳ У вас уже идет генерация..."
    GIF_CONVERSION_ERROR = "⚠️ Ошибка конвертации."
    GIF_SEND_ERROR = "⚠️ Не удалось отправить GIF."
    # Native .gif file export (on-demand)
    BTN_SEND_ANIMATION_MP4 = "🔄 Анимация (MP4)"
    BTN_SAVE_GIF_FILE = "💾 Файлом (.gif)"
    BTN_SAVE_GIF_WAIT = "⏳ Готовлю .gif файл..."
    BTN_SAVE_GIF_DONE = "✅ .gif файл отправлен"
    GIF_FILE_PREPARING_TOAST = "Конвертирую в нативный .gif, подождите..."
    GIF_FILE_ERROR = "⚠️ Ошибка конвертации. Попробуйте ещё раз."
    GIF_FILE_QUEUE_TOAST = "Сервер занят, добавляю в очередь..."

    # ── Slideshow ─────────────────────────────────────────────────
    SLIDESHOW_DETECTED = "🖼 <b>{title}</b>\n📸 Это фотоальбом (TikTok Slideshow)"
    BTN_SLIDESHOW_PHOTOS = "📸 Фото (альбом)"
    BTN_SLIDESHOW_VIDEO = "🎬 Видео (slideshow)"
    SLIDESHOW_DOWNLOADING = "⏳ Скачиваю фото..."
    SLIDESHOW_CONVERTING = "⏳ Создаю видео из фото..."
    SLIDESHOW_SENDING = "📤 Отправляю фотоальбом..."
    SLIDESHOW_TRUNCATED = "ℹ️ Показаны первые 10 из {total} фото."
    SLIDESHOW_ERROR = "⚠️ Ошибка загрузки слайдшоу."
    # Shown as caption on the first album when content spans multiple batches of 10
    SLIDESHOW_MULTI_ALBUM = "📸 1/{total_batches} • {total} фото — остальные придут следующими 🤫"

    # ── Group logic ───────────────────────────────────────────────
    GROUP_ERROR = "❌ Ошибка."
    GROUP_SENDING = "📤 Отправляю..."
    GROUP_SEND_ERROR = "⚠️ Ошибка отправки."
    GROUP_SLIDESHOW_CHOICE = "🖼 TikTok Slideshow\nВыберите формат:"

    # ── Service layer (ytdlp exceptions) ──────────────────────────
    SVC_ACCESS_DENIED = (
        "Доступ запрещен. Возможно, контент приватный или требуется авторизация."
    )
    SVC_VIDEO_NOT_FOUND = "Видео не найдено. Проверьте ссылку."
    SVC_LIVE_NOT_SUPPORTED = "Прямые трансляции (Live) не поддерживаются."
    SVC_FORMAT_UNAVAILABLE = (
        "Выбранный формат или видео недоступны. Попробуйте другую ссылку."
    )
    SVC_EXTRACTION_ERROR = "Ошибка извлечения: {detail}"
    SVC_VK_AUTH_REQUIRED = (
        "🔐 VK аудио требует авторизации.\n"
        "Настройте <code>VK_COOKIES_B64</code> в конфигурации бота."
    )
    SVC_DEFAULT_TITLE = "Видео"

    # ── Instagram ─────────────────────────────────────────────────
    IG_LOADING_PROFILE = "🔎 Загружаю профиль @{username}..."
    IG_NO_STORIES = "📭 У @{username} сейчас нет активных историй."
    IG_NO_CONTENT = "📭 У @{username} нет доступных историй или хайлайтов."
    IG_STORIES_HEADER = (
        "📸 <b>Истории @{username}</b>\nВыберите историю для скачивания:"
    )
    IG_HIGHLIGHTS_HEADER = (
        "📁 <b>Хайлайты @{username}</b>\nВыберите хайлайт для просмотра:"
    )
    IG_HIGHLIGHT_ITEMS_HEADER = "📁 <b>{title}</b>\nВыберите элемент для скачивания:"
    IG_DOWNLOADING = "⏳ Скачиваю {type}..."
    IG_DOWNLOAD_ERROR = "⚠️ Ошибка загрузки из Instagram."
    IG_SESSION_EXPIRED = "⚠️ Сессия Instagram истекла. Обратитесь к администратору."
    IG_MENU = "📷 <b>Instagram — @{username}</b>\nВыберите раздел:"
