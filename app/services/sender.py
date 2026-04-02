"""Telegram file/media sending service."""

import asyncio
import logging
import io
import os
from typing import Any, Union
from contextlib import contextmanager

from telegram import Bot, InputMediaPhoto
from telegram.error import NetworkError

logger = logging.getLogger("app.services.sender")

# Maximum photos in a Telegram media group
MAX_TELEGRAM_ALBUM_SIZE = 10


@contextmanager
def _open_media(file_path_or_buffer: Union[str, io.BytesIO]) -> Any:
    if isinstance(file_path_or_buffer, str):
        with open(file_path_or_buffer, "rb") as f:
            yield f
    else:
        # It's a BytesIO, just yield it
        yield file_path_or_buffer


class TelegramSender:
    """Handles sending files and media groups to Telegram."""

    @staticmethod
    async def send_file(
        bot: Bot,
        chat_id: int,
        file_path_or_buffer: Union[str, io.BytesIO],
        is_audio: bool = False,
        is_gif: bool = False,
        caption: str = "",
        parse_mode: str | None = None,
        reply_markup: Any = None,
        reply_to_message_id: int | None = None,
        duration: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> bool:
        """Sends a single file (video, audio, or GIF) to Telegram."""
        from app.core.metrics import metrics as _m

        try:
            with _m.upload_duration.time(), _open_media(file_path_or_buffer) as f:
                if is_gif:
                    await bot.send_animation(
                        chat_id=chat_id,
                        animation=f,
                        caption=caption,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                        duration=duration,
                        width=width,
                        height=height,
                    )
                elif is_audio:
                    await bot.send_audio(
                        chat_id=chat_id,
                        audio=f,
                        caption=caption,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                        duration=duration,
                    )
                else:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=caption,
                        parse_mode=parse_mode,
                        supports_streaming=True,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                        duration=duration,
                        width=width,
                        height=height,
                    )
            return True

        except NetworkError:
            file_name_debug = (
                file_path_or_buffer
                if isinstance(file_path_or_buffer, str)
                else "BytesIO"
            )
            logger.warning(
                "Network error sending file", extra={"file": file_name_debug}
            )
            return False
        except Exception as e:
            logger.error("Send error", extra={"error": str(e)}, exc_info=True)
            return False

    @staticmethod
    async def send_slideshow_photos(
        bot: Bot,
        chat_id: int,
        images: list[str],
        caption: str = "",
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> bool:
        """
        Sends images as a Telegram media group (photo album).

        Max 10 photos per group. Returns True on success.
        """
        if not images:
            return False

        # Filter out missing or empty files
        valid_images: list[str] = []
        for img in images[:MAX_TELEGRAM_ALBUM_SIZE]:
            try:
                size = await asyncio.to_thread(os.path.getsize, img)
                if size > 0:
                    valid_images.append(img)
                else:
                    logger.warning("Skipping empty image file: %s", img)
            except OSError:
                logger.warning("Skipping missing image file: %s", img)

        if not valid_images:
            logger.error("No valid images to send")
            return False

        # Telegram sendMediaGroup requires 2-10 items;
        # fall back to sendPhoto for a single image.
        if len(valid_images) == 1:
            try:
                with open(valid_images[0], "rb") as fh:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=fh,
                        caption=caption,
                        parse_mode=parse_mode,
                        reply_to_message_id=reply_to_message_id,
                    )
                return True
            except Exception as e:
                logger.error("Single photo send error: %s", e, exc_info=True)
                return False

        file_handles = []
        try:
            media = []
            for i, img_path in enumerate(valid_images):
                fh = await asyncio.to_thread(open, img_path, "rb")
                file_handles.append(fh)
                media.append(
                    InputMediaPhoto(
                        media=fh,
                        caption=caption if i == 0 else None,
                        parse_mode=parse_mode if i == 0 else None,
                    )
                )


            await bot.send_media_group(
                chat_id=chat_id,
                media=media,
                reply_to_message_id=reply_to_message_id,
                write_timeout=30,
                read_timeout=30,
            )
            return True

        except NetworkError as e:
            logger.warning("Network error sending slideshow photos: %s", e)
            return False
        except Exception as e:
            logger.error("Slideshow send error: %s", e, exc_info=True)
            return False
        finally:
            for fh in file_handles:
                try:
                    fh.close()
                except Exception:
                    pass
