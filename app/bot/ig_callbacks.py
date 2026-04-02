"""
Instagram callback handlers for story/highlight selection UI.

Handles:
- ig_stories|<token>  → Show story thumbnails with numbered selection
- ig_highlights|<token> → Show highlight list
- ig_hl_items|<token>|<hl_id> → Show items in a highlight
- ig_dl|<token>|<mediaid> → Download a single story/highlight item
- ig_dl_all|<token> → Download all stories
"""

import asyncio
import logging
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from app.core import state
from app.core.texts import Texts
from app.core.models import DownloadContext
from app.services.sender import TelegramSender

logger = logging.getLogger("app.bot.ig_callbacks")


async def on_ig_stories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show story thumbnails as a media group with an inline keyboard."""
    q = update.callback_query
    assert q is not None and isinstance(q.message, Message) and q.data
    await q.answer()

    try:
        _, token = q.data.split("|", 1)
    except ValueError:
        return

    payload = await state.link_cache.get(token)
    if not payload:
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    if isinstance(payload, dict):
        payload = DownloadContext(**payload)

    ig_data = payload.api_json
    if not ig_data or not ig_data.get("stories"):
        await q.edit_message_text(
            Texts.IG_NO_STORIES.format(username=payload.user_tag or "")
        )
        return

    stories = ig_data["stories"]

    # Build numbered story list as caption text
    lines = ["📸 <b>Истории</b>\n"]
    for i, s in enumerate(stories, 1):
        emoji = "🎬" if s.get("is_video") else "📸"
        ts = s.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts)
            time_str = dt.strftime("%d.%m %H:%M")
        except (ValueError, TypeError):
            time_str = "—"

        dur = ""
        if s.get("duration"):
            dur = f" ({int(s['duration'])}с)"
        lines.append(f"{i}. {emoji} {time_str}{dur}")

    caption = "\n".join(lines)

    # Build keyboard with numbered buttons (max 8 per row)
    btn_rows = []
    row: list[InlineKeyboardButton] = []
    for i, s in enumerate(stories):
        mediaid = s.get("mediaid", str(i))
        row.append(
            InlineKeyboardButton(
                str(i + 1),
                callback_data=f"ig_dl|{token}|{mediaid}",
            )
        )
        if len(row) >= 5:
            btn_rows.append(row)
            row = []
    if row:
        btn_rows.append(row)

    # Add "Download All" and "Back" buttons
    btn_rows.append(
        [InlineKeyboardButton("📥 Скачать все", callback_data=f"ig_dl_all|{token}")]
    )
    btn_rows.append(
        [InlineKeyboardButton("🔙 Назад", callback_data=f"ig_menu|{token}")]
    )

    try:
        await q.edit_message_text(
            caption,
            reply_markup=InlineKeyboardMarkup(btn_rows),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Failed to show stories: %s", e)


async def on_ig_highlights(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show highlights as inline buttons."""
    q = update.callback_query
    assert q is not None and isinstance(q.message, Message) and q.data
    await q.answer()

    try:
        _, token = q.data.split("|", 1)
    except ValueError:
        return

    payload = await state.link_cache.get(token)
    if not payload:
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    if isinstance(payload, dict):
        payload = DownloadContext(**payload)

    ig_data = payload.api_json
    if not ig_data or not ig_data.get("highlights"):
        await q.edit_message_text("📭 Хайлайты не найдены.")
        return

    highlights = ig_data["highlights"]

    btn_rows = []
    for hl in highlights:
        title = hl.get("title", "Highlight")
        count = hl.get("item_count", 0)
        hl_id = hl.get("highlight_id", "")
        btn_rows.append(
            [
                InlineKeyboardButton(
                    f"📁 {title} ({count})",
                    callback_data=f"ig_hl_items|{token}|{hl_id}",
                )
            ]
        )

    btn_rows.append(
        [InlineKeyboardButton("🔙 Назад", callback_data=f"ig_menu|{token}")]
    )

    await q.edit_message_text(
        Texts.IG_HIGHLIGHTS_HEADER.format(username=payload.user_tag or ""),
        reply_markup=InlineKeyboardMarkup(btn_rows),
        parse_mode="HTML",
    )


async def on_ig_highlight_items(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Fetch and show items of a specific highlight."""
    q = update.callback_query
    assert q is not None and isinstance(q.message, Message) and q.data
    await q.answer()

    try:
        _, token, hl_id = q.data.split("|", 2)
    except ValueError:
        return

    payload = await state.link_cache.get(token)
    if not payload:
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    if isinstance(payload, dict):
        payload = DownloadContext(**payload)

    ig_data = payload.api_json
    if not ig_data:
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    # Find the highlight title
    hl_title = "Highlight"
    for hl in ig_data.get("highlights", []):
        if hl.get("highlight_id") == hl_id:
            hl_title = hl.get("title", "Highlight")
            break

    await q.edit_message_text(Texts.IG_DOWNLOADING.format(type=f"хайлайт «{hl_title}»"))

    from app.services.instagram import InstagramService

    items, error = await InstagramService.get_highlight_items(hl_id)
    if error or not items:
        await q.edit_message_text(error or "⚠️ Не удалось загрузить элементы хайлайта.")
        return

    # Store highlight items in a sub-cache
    hl_cache_key = f"{token}_hl_{hl_id}"
    hl_items_data = [
        {
            "mediaid": item.mediaid,
            "is_video": item.is_video,
            "url": item.url,
            "thumbnail_url": item.thumbnail_url,
            "timestamp": item.timestamp.isoformat(),
            "duration": item.duration,
            "typename": item.typename,
        }
        for item in items
    ]
    await state.link_cache.set(hl_cache_key, hl_items_data)

    # Build item list
    lines = [f"📁 <b>{hl_title}</b>\n"]
    for i, item in enumerate(items, 1):
        emoji = "🎬" if item.is_video else "📸"
        ts = item.timestamp.strftime("%d.%m %H:%M")
        dur = f" ({int(item.duration)}с)" if item.duration else ""
        lines.append(f"{i}. {emoji} {ts}{dur}")

    btn_rows = []
    row_buf: list[InlineKeyboardButton] = []
    for i, item in enumerate(items):
        row_buf.append(
            InlineKeyboardButton(
                str(i + 1),
                callback_data=f"ig_hl_dl|{hl_cache_key}|{item.mediaid}",
            )
        )
        if len(row_buf) >= 5:
            btn_rows.append(row_buf)
            row_buf = []
    if row_buf:
        btn_rows.append(row_buf)

    btn_rows.append(
        [
            InlineKeyboardButton(
                "📥 Скачать все",
                callback_data=f"ig_hl_dl_all|{hl_cache_key}",
            )
        ]
    )
    btn_rows.append(
        [
            InlineKeyboardButton(
                "🔙 К хайлайтам",
                callback_data=f"ig_highlights|{token}",
            )
        ]
    )

    await q.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(btn_rows),
        parse_mode="HTML",
    )


async def on_ig_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download a single story item by mediaid."""
    q = update.callback_query
    assert q is not None and isinstance(q.message, Message) and q.data
    await q.answer(Texts.IG_DOWNLOADING.format(type=""), show_alert=False)

    try:
        _, token, mediaid = q.data.split("|", 2)
    except ValueError:
        return

    payload = await state.link_cache.get(token)
    if not payload:
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    if isinstance(payload, dict):
        payload = DownloadContext(**payload)

    ig_data = payload.api_json
    if not ig_data:
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    story_data = next(
        (s for s in ig_data.get("stories", []) if s.get("mediaid") == mediaid),
        None,
    )
    if not story_data:
        await q.edit_message_text("⚠️ Элемент не найден.")
        return

    from app.services.instagram import IGStoryItem

    story = IGStoryItem(
        mediaid=story_data["mediaid"],
        is_video=story_data["is_video"],
        url=story_data["url"],
        thumbnail_url=story_data["thumbnail_url"],
        timestamp=datetime.fromisoformat(story_data["timestamp"]),
        duration=story_data.get("duration"),
        typename=story_data.get("typename", ""),
    )

    from app.services.instagram import InstagramService

    file_path, error = await InstagramService.download_story_item(story)
    if error or not file_path:
        try:
            await q.message.reply_text(error or Texts.IG_DOWNLOAD_ERROR)
        except Exception:
            pass
        return

    try:
        await context.bot.send_chat_action(
            chat_id=q.message.chat_id,
            action=(
                ChatAction.UPLOAD_VIDEO if story.is_video else ChatAction.UPLOAD_PHOTO
            ),
        )
    except Exception:
        pass

    success = await TelegramSender.send_file(
        context.bot,
        q.message.chat_id,
        file_path,
        is_audio=False,
        is_gif=False,
        caption=f"📷 {story.label}",
    )
    if not success:
        try:
            await q.message.reply_text(Texts.IG_DOWNLOAD_ERROR)
        except Exception:
            pass


async def on_ig_download_all(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Download all stories concurrently."""
    q = update.callback_query
    assert q is not None and isinstance(q.message, Message) and q.data
    await q.answer("📥 Скачиваю все...")

    try:
        _, token = q.data.split("|", 1)
    except ValueError:
        return

    payload = await state.link_cache.get(token)
    if not payload:
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    if isinstance(payload, dict):
        payload = DownloadContext(**payload)

    ig_data = payload.api_json
    if not ig_data or not ig_data.get("stories"):
        await q.edit_message_text(Texts.IG_NO_STORIES.format(username=""))
        return

    await q.edit_message_text("⏳ Скачиваю все истории...")

    from app.services.instagram import InstagramService, IGStoryItem

    stories = [
        IGStoryItem(
            mediaid=s["mediaid"],
            is_video=s["is_video"],
            url=s["url"],
            thumbnail_url=s["thumbnail_url"],
            timestamp=datetime.fromisoformat(s["timestamp"]),
            duration=s.get("duration"),
            typename=s.get("typename", ""),
        )
        for s in ig_data["stories"]
    ]

    sem = asyncio.Semaphore(3)
    results: list[tuple[str | None, IGStoryItem]] = []

    async def _dl(item: IGStoryItem) -> tuple[str | None, IGStoryItem]:
        async with sem:
            path, _ = await InstagramService.download_story_item(item)
            return path, item

    done = await asyncio.gather(*[_dl(s) for s in stories])
    results = list(done)

    sent = 0
    for path, item in results:
        if not path:
            continue
        try:
            await context.bot.send_chat_action(
                chat_id=q.message.chat_id,
                action=(
                    ChatAction.UPLOAD_VIDEO
                    if item.is_video
                    else ChatAction.UPLOAD_PHOTO
                ),
            )
        except Exception:
            pass

        success = await TelegramSender.send_file(
            context.bot,
            q.message.chat_id,
            path,
            is_audio=False,
            is_gif=False,
            caption=f"📷 {item.label}",
        )
        if success:
            sent += 1

    total = len(stories)
    try:
        await q.edit_message_text(f"✅ Скачано {sent}/{total} историй.")
    except Exception:
        pass


async def on_ig_hl_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download a single highlight item."""
    q = update.callback_query
    assert q is not None and isinstance(q.message, Message) and q.data
    await q.answer(Texts.IG_DOWNLOADING.format(type=""), show_alert=False)

    try:
        _, cache_key, mediaid = q.data.split("|", 2)
    except ValueError:
        return

    items_data = await state.link_cache.get(cache_key)
    if not items_data or not isinstance(items_data, list):
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    item_data = next((i for i in items_data if i.get("mediaid") == mediaid), None)
    if not item_data:
        await q.edit_message_text("⚠️ Элемент не найден.")
        return

    from app.services.instagram import IGStoryItem, InstagramService

    story = IGStoryItem(
        mediaid=item_data["mediaid"],
        is_video=item_data["is_video"],
        url=item_data["url"],
        thumbnail_url=item_data["thumbnail_url"],
        timestamp=datetime.fromisoformat(item_data["timestamp"]),
        duration=item_data.get("duration"),
        typename=item_data.get("typename", ""),
    )

    file_path, error = await InstagramService.download_story_item(story)
    if error or not file_path:
        try:
            await q.message.reply_text(error or Texts.IG_DOWNLOAD_ERROR)
        except Exception:
            pass
        return

    success = await TelegramSender.send_file(
        context.bot,
        q.message.chat_id,
        file_path,
        is_audio=False,
        is_gif=False,
        caption=f"📷 {story.label}",
    )
    if not success:
        try:
            await q.message.reply_text(Texts.IG_DOWNLOAD_ERROR)
        except Exception:
            pass


async def on_ig_hl_download_all(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Download all items in a highlight."""
    q = update.callback_query
    assert q is not None and isinstance(q.message, Message) and q.data
    await q.answer("📥 Скачиваю все...")

    try:
        _, cache_key = q.data.split("|", 1)
    except ValueError:
        return

    items_data = await state.link_cache.get(cache_key)
    if not items_data or not isinstance(items_data, list):
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    await q.edit_message_text("⏳ Скачиваю все элементы хайлайта...")

    from app.services.instagram import IGStoryItem, InstagramService

    items = [
        IGStoryItem(
            mediaid=d["mediaid"],
            is_video=d["is_video"],
            url=d["url"],
            thumbnail_url=d["thumbnail_url"],
            timestamp=datetime.fromisoformat(d["timestamp"]),
            duration=d.get("duration"),
            typename=d.get("typename", ""),
        )
        for d in items_data
    ]

    sem = asyncio.Semaphore(3)
    sent = 0
    chat_id = q.message.chat_id

    async def _dl_send(item: IGStoryItem) -> bool:
        async with sem:
            path, _ = await InstagramService.download_story_item(item)
            if not path:
                return False
            return await TelegramSender.send_file(
                context.bot,
                chat_id,
                path,
                is_audio=False,
                is_gif=False,
                caption=f"📷 {item.label}",
            )

    results = await asyncio.gather(*[_dl_send(i) for i in items])
    sent = sum(1 for r in results if r)

    try:
        await q.edit_message_text(f"✅ Скачано {sent}/{len(items)} элементов хайлайта.")
    except Exception:
        pass


async def on_ig_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to the main Instagram profile menu."""
    q = update.callback_query
    assert q is not None and isinstance(q.message, Message) and q.data
    await q.answer()

    try:
        _, token = q.data.split("|", 1)
    except ValueError:
        return

    payload = await state.link_cache.get(token)
    if not payload:
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    if isinstance(payload, dict):
        payload = DownloadContext(**payload)

    ig_data = payload.api_json
    if not ig_data:
        await q.edit_message_text(Texts.CACHE_EXPIRED_RESEND)
        return

    stories = ig_data.get("stories", [])
    highlights = ig_data.get("highlights", [])

    buttons = []
    if stories:
        buttons.append(
            InlineKeyboardButton(
                f"📸 Истории ({len(stories)})",
                callback_data=f"ig_stories|{token}",
            )
        )
    if highlights:
        buttons.append(
            InlineKeyboardButton(
                f"📁 Хайлайты ({len(highlights)})",
                callback_data=f"ig_highlights|{token}",
            )
        )

    rows = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])

    if stories:
        rows.append(
            [
                InlineKeyboardButton(
                    "📥 Скачать все истории",
                    callback_data=f"ig_dl_all|{token}",
                )
            ]
        )

    await q.edit_message_text(
        Texts.IG_MENU.format(username=payload.user_tag or ""),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )
