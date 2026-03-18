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
        "• Pinterest\n\n"
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
    FILE_TOO_BIG = (
        "⚠️ Файл слишком большой (~{size_mb:.1f} МБ).\n"
        "Telegram Bot API не позволяет отправлять файлы больше {max_mb} МБ.\n"
        "Пожалуйста, используйте прямую ссылку ниже."
    )
    STARTING_DOWNLOAD = "⏳ Начинаю загрузку..."
    GENERIC_ERROR_SHORT = "⚠️ Ошибка."
    SENDING_TO_TG = "📤 Отправляю в Telegram..."
    SEND_ERROR = "⚠️ Ошибка при отправке файла."
    GIF_CONVERTING = "⏳ Конвертирую в GIF..."
    GIF_DOWNLOAD_CANCEL = "❌ Загрузка отменена."
    GIF_FILE_EXPIRED = "⚠️ Файл не найден или устарел."
    GIF_ALREADY_IN_PROGRESS = "⏳ У вас уже идет генерация..."
    GIF_CONVERSION_ERROR = "⚠️ Ошибка конвертации."
    GIF_SEND_ERROR = "⚠️ Не удалось отправить GIF."

    # ── Slideshow ─────────────────────────────────────────────────
    SLIDESHOW_DETECTED = "🖼 <b>{title}</b>\n📸 Это фотоальбом (TikTok Slideshow)"
    BTN_SLIDESHOW_PHOTOS = "📸 Фото (альбом)"
    BTN_SLIDESHOW_VIDEO = "🎬 Видео (slideshow)"
    SLIDESHOW_DOWNLOADING = "⏳ Скачиваю фото..."
    SLIDESHOW_CONVERTING = "⏳ Создаю видео из фото..."
    SLIDESHOW_SENDING = "📤 Отправляю фотоальбом..."
    SLIDESHOW_TRUNCATED = "ℹ️ Показаны первые 10 из {total} фото."
    SLIDESHOW_ERROR = "⚠️ Ошибка загрузки слайдшоу."

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
    SVC_DEFAULT_TITLE = "Видео"
