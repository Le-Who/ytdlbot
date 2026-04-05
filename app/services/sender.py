"""Telegram file/media sending service."""

import asyncio
import logging
import io
import os
from typing import Any, Union
from contextlib import contextmanager

from telegram import Bot, InputMediaPhoto, ReplyParameters
from telegram.error import NetworkError, RetryAfter

logger = logging.getLogger("app.services.sender")

# Maximum photos in a Telegram media group
MAX_TELEGRAM_ALBUM_SIZE = 10


@contextmanager
def _open_media(
    file_path_or_buffer: Union[str, io.BytesIO], use_local_api: bool = False
) -> Any:
    if isinstance(file_path_or_buffer, str):
        if use_local_api:
            # Pass the file:// URI directly; no need to open the file in Python
            yield f"file://{os.path.abspath(file_path_or_buffer)}"
        else:
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
        reply_parameters: Any = None,
        duration: int | None = None,
        width: int | None = None,
        height: int | None = None,
        use_local_api: bool = False,
        has_spoiler: bool = False,
        thumbnail: Union[str, io.BytesIO, None] = None,
        file_name: str | None = None,
    ) -> bool:
        """Sends a single file (video, audio, or GIF) to Telegram."""
        from app.core.metrics import metrics as _m

        reply_params = reply_parameters
        if not reply_params and reply_to_message_id:
            reply_params = ReplyParameters(
                message_id=reply_to_message_id, allow_sending_without_reply=True
            )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                with (
                    _m.upload_duration.time(),
                    _open_media(file_path_or_buffer, use_local_api) as f,
                ):
                    if is_gif:
                        await bot.send_animation(
                            chat_id=chat_id,
                            animation=f,
                            caption=caption,
                            parse_mode=parse_mode,
                            reply_markup=reply_markup,
                            reply_parameters=reply_params,
                            duration=duration,
                            width=width,
                            height=height,
                            has_spoiler=has_spoiler,
                            show_caption_above_media=True,
                            thumbnail=thumbnail,
                            filename=file_name,
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=30,
                        )
                    elif is_audio:
                        await bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            caption=caption,
                            parse_mode=parse_mode,
                            reply_markup=reply_markup,
                            reply_parameters=reply_params,
                            duration=duration,
                            thumbnail=thumbnail,
                            filename=file_name,
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=30,
                        )
                    else:
                        await bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            caption=caption,
                            parse_mode=parse_mode,
                            supports_streaming=True,
                            reply_markup=reply_markup,
                            reply_parameters=reply_params,
                            duration=duration,
                            width=width,
                            height=height,
                            has_spoiler=has_spoiler,
                            show_caption_above_media=True,
                            thumbnail=thumbnail,
                            filename=file_name,
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=30,
                        )
                return True

            except RetryAfter as e:
                delay = (
                    e.retry_after.total_seconds()
                    if hasattr(e.retry_after, "total_seconds")
                    else float(e.retry_after)
                )
                logger.warning("FloodWait sending file. Sleeping %s s.", delay)
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                else:
                    return False
            except NetworkError:
                file_name_debug = (
                    file_path_or_buffer
                    if isinstance(file_path_or_buffer, str)
                    else "BytesIO"
                )
                logger.warning(
                    "Network error sending file, attempt %s/%s",
                    attempt + 1,
                    max_retries,
                    extra={"file": file_name_debug},
                    exc_info=True,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(2**attempt)  # 1s, 2s
                else:
                    return False
            except Exception as e:
                logger.error("Send error", extra={"error": str(e)}, exc_info=True)
                return False
        return False

    @staticmethod
    async def send_slideshow_photos(
        bot: Bot,
        chat_id: int,
        images: list[str],
        caption: str = "",
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
        reply_parameters: Any = None,
        use_local_api: bool = False,
    ) -> bool:
        """
        Sends images as a Telegram media group (photo album).
        Automatically chunked if more than 10 images are provided.
        """
        if not images:
            return False

        reply_params = reply_parameters
        if not reply_params and reply_to_message_id:
            reply_params = ReplyParameters(
                message_id=reply_to_message_id, allow_sending_without_reply=True
            )

        # Filter out missing or empty files
        valid_images: list[str] = []
        for img in images:
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

        # Chunk into groups of MAX_TELEGRAM_ALBUM_SIZE (10)
        chunks = [
            valid_images[i : i + MAX_TELEGRAM_ALBUM_SIZE]
            for i in range(0, len(valid_images), MAX_TELEGRAM_ALBUM_SIZE)
        ]
        overall_success = True

        for chunk_idx, chunk in enumerate(chunks):
            current_caption = caption if chunk_idx == 0 else ""
            current_parse_mode = parse_mode if chunk_idx == 0 else None

            # Telegram sendMediaGroup requires 2-10 items; fall back to sendPhoto for a single image.
            if len(chunk) == 1:
                try:
                    # Using context manager for local API or standard open
                    with _open_media(chunk[0], use_local_api) as fh:
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=fh,
                            caption=current_caption,
                            parse_mode=current_parse_mode,
                            reply_parameters=reply_params if chunk_idx == 0 else None,
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=30,
                        )
                except Exception as e:
                    logger.error("Single photo send error: %s", e, exc_info=True)
                    overall_success = False
                continue

            file_handles = []
            try:
                media = []
                for i, img_path in enumerate(chunk):
                    if use_local_api:
                        fh = f"file://{os.path.abspath(img_path)}"
                        media.append(
                            InputMediaPhoto(
                                media=fh,
                                caption=current_caption if i == 0 else None,
                                parse_mode=current_parse_mode if i == 0 else None,
                            )
                        )
                    else:
                        fh = await asyncio.to_thread(open, img_path, "rb")
                        file_handles.append(fh)
                        media.append(
                            InputMediaPhoto(
                                media=fh,
                                caption=current_caption if i == 0 else None,
                                parse_mode=current_parse_mode if i == 0 else None,
                            )
                        )

                await bot.send_media_group(
                    chat_id=chat_id,
                    media=media,
                    reply_parameters=reply_params if chunk_idx == 0 else None,
                    write_timeout=120,
                    read_timeout=120,
                    connect_timeout=30,
                )

            except RetryAfter as e:
                delay = (
                    e.retry_after.total_seconds()
                    if hasattr(e.retry_after, "total_seconds")
                    else float(e.retry_after)
                )
                logger.warning(
                    "FloodWait sending slideshow chunk. Sleeping %s s.", delay
                )
                await asyncio.sleep(delay)
                overall_success = False  # Consider retry mechanism globally later
            except NetworkError as e:
                logger.warning("Network error sending slideshow photos: %s", e)
                overall_success = False
            except Exception as e:
                logger.error("Slideshow send error: %s", e, exc_info=True)
                overall_success = False
            finally:
                for fh in file_handles:
                    try:
                        fh.close()
                    except Exception:
                        pass

        return overall_success
