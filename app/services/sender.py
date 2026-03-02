"""Telegram file/media sending service."""

import logging
from typing import Optional

from telegram import Bot, InputMediaPhoto
from telegram.error import NetworkError

logger = logging.getLogger("app.services.sender")

# Maximum photos in a Telegram media group
MAX_TELEGRAM_ALBUM_SIZE = 10


class TelegramSender:
    """Handles sending files and media groups to Telegram."""

    @staticmethod
    async def send_file(
        bot: Bot,
        chat_id: int,
        file_path: str,
        is_audio: bool = False,
        is_gif: bool = False,
        caption: str = "",
        parse_mode: str = None,
        reply_markup=None,
        reply_to_message_id: int = None,
    ) -> bool:
        """Sends a single file (video, audio, or GIF) to Telegram."""
        try:
            with open(file_path, "rb") as f:
                if is_gif:
                    await bot.send_animation(
                        chat_id=chat_id,
                        animation=f,
                        caption=caption,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
                    )
                elif is_audio:
                    await bot.send_audio(
                        chat_id=chat_id,
                        audio=f,
                        caption=caption,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                        reply_to_message_id=reply_to_message_id,
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
                    )
            return True

        except NetworkError:
            logger.warning(f"Network error sending file {file_path}")
            return False
        except Exception as e:
            logger.error(f"Send error: {e}", exc_info=True)
            return False

    @staticmethod
    async def send_slideshow_photos(
        bot: Bot,
        chat_id: int,
        images: list[str],
        caption: str = "",
        parse_mode: str = None,
        reply_to_message_id: int = None,
    ) -> bool:
        """
        Sends images as a Telegram media group (photo album).

        Max 10 photos per group. Returns True on success.
        """
        if not images:
            return False

        photos_to_send = images[:MAX_TELEGRAM_ALBUM_SIZE]

        file_handles = []
        try:
            media = []
            for i, img_path in enumerate(photos_to_send):
                fh = open(img_path, "rb")
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
            )
            return True

        except NetworkError:
            logger.warning("Network error sending slideshow photos")
            return False
        except Exception as e:
            logger.error(f"Slideshow send error: {e}", exc_info=True)
            return False
        finally:
            for fh in file_handles:
                try:
                    fh.close()
                except Exception:
                    pass
