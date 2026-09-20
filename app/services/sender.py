"""Backward-compatible facade for receipt-returning Telegram delivery."""

from __future__ import annotations

import io
import os
from typing import Any

from telegram import Bot, ReplyParameters

from app.services.media.delivery import (
    MAX_TELEGRAM_ALBUM_SIZE,
    DeliveryAsset,
    TelegramDelivery,
)
from app.services.media.models import (
    DeliveryReceipt,
    DeliveryTarget,
    MediaItem,
    MediaKind,
)


class TelegramSender:
    """Keep legacy call signatures while returning structured receipts."""

    @staticmethod
    async def send_file(
        bot: Bot,
        chat_id: int,
        file_path_or_buffer: str | io.BytesIO,
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
        use_local_api: bool | None = None,
        has_spoiler: bool = False,
        thumbnail: str | io.BytesIO | None = None,
        file_name: str | None = None,
    ) -> DeliveryReceipt:
        from app.core.metrics import metrics

        kind = (
            MediaKind.ANIMATION
            if is_gif
            else MediaKind.AUDIO
            if is_audio
            else MediaKind.VIDEO
        )
        source_name = (
            os.path.basename(file_path_or_buffer)
            if isinstance(file_path_or_buffer, str)
            else file_name or "buffer"
        )
        item = MediaItem(
            media_id=source_name,
            kind=kind,
            url=str(file_path_or_buffer)
            if isinstance(file_path_or_buffer, str)
            else "",
            duration_seconds=float(duration) if duration is not None else None,
            width=width,
            height=height,
        )
        reply_params = reply_parameters
        if reply_params is None and reply_to_message_id:
            reply_params = ReplyParameters(
                message_id=reply_to_message_id, allow_sending_without_reply=True
            )
        delivery = TelegramDelivery(bot, local_mode=use_local_api)
        with metrics.upload_duration.time():
            return await delivery.deliver(
                DeliveryAsset(item=item, source=file_path_or_buffer),
                DeliveryTarget(str(chat_id)),
                caption=caption,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                reply_parameters=reply_params,
                duration=duration,
                width=width,
                height=height,
                has_spoiler=has_spoiler,
                thumbnail=thumbnail,
                file_name=file_name,
            )

    @staticmethod
    async def send_slideshow_photos(
        bot: Bot,
        chat_id: int,
        images: list[str],
        caption: str = "",
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
        reply_parameters: Any = None,
        use_local_api: bool | None = None,
    ) -> DeliveryReceipt:
        reply_params = reply_parameters
        if reply_params is None and reply_to_message_id:
            reply_params = ReplyParameters(
                message_id=reply_to_message_id, allow_sending_without_reply=True
            )
        assets = [
            DeliveryAsset(
                item=MediaItem(
                    media_id=f"photo:{index}",
                    kind=MediaKind.PHOTO,
                    url=image,
                ),
                source=image,
                item_index=index,
            )
            for index, image in enumerate(images)
        ]
        delivery = TelegramDelivery(bot, local_mode=use_local_api)
        return await delivery.deliver(
            assets,
            DeliveryTarget(str(chat_id)),
            caption=caption,
            parse_mode=parse_mode,
            reply_parameters=reply_params,
        )


__all__ = ["MAX_TELEGRAM_ALBUM_SIZE", "TelegramSender"]
