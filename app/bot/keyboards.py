from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from app.bot.format_formatter import format_label

from app.constants import (
    AUDIO_FORMAT_ID,
    SLIDESHOW_PHOTO_FORMAT_ID,
    SLIDESHOW_VIDEO_FORMAT_ID,
)
from app.core.texts import Texts


from typing import Any


def build_format_keyboard(
    formats: list[Any], special_format: Any, special_index: int | None = None
) -> InlineKeyboardMarkup:
    """Helper to build format selection buttons in 2 columns."""
    buttons = []
    formats_slice = formats[:8]
    for i in range(0, len(formats_slice), 2):
        row = [
            InlineKeyboardButton(
                format_label(formats_slice[i]),
                callback_data=f"pick|{i}",
            )
        ]
        if i + 1 < len(formats_slice):
            row.append(
                InlineKeyboardButton(
                    format_label(formats_slice[i + 1]),
                    callback_data=f"pick|{i + 1}",
                )
            )
        buttons.append(row)

    if special_format is not None:
        idx = special_index if special_index is not None else len(formats_slice)
        buttons.append(
            [
                InlineKeyboardButton(
                    format_label(special_format),
                    callback_data=f"pick|{idx}",
                )
            ]
        )
    return InlineKeyboardMarkup(buttons)


def build_slideshow_keyboard() -> InlineKeyboardMarkup:
    """Build keyboard for TikTok slideshow (image carousel) posts."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    Texts.BTN_SLIDESHOW_PHOTOS,
                    callback_data=f"slideshow|{SLIDESHOW_PHOTO_FORMAT_ID}",
                )
            ],
            [
                InlineKeyboardButton(
                    Texts.BTN_SLIDESHOW_VIDEO,
                    callback_data=f"slideshow|{SLIDESHOW_VIDEO_FORMAT_ID}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎵 Audio",
                    callback_data=f"pick|{AUDIO_FORMAT_ID}",
                )
            ],
        ]
    )


def build_sent_gif_keyboard(token: str) -> InlineKeyboardMarkup:
    """Keyboard attached to a sent GIF animation message.

    Provides a single 'Save as .gif file' button that triggers on-demand
    native GIF export via the giffile| callback.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    Texts.BTN_SAVE_GIF_FILE,
                    callback_data=f"giffile|{token}",
                )
            ]
        ]
    )

def build_video_keyboard(token: str) -> InlineKeyboardMarkup:
    """Keyboard attached to a standard video message to offer GIF conversions.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    Texts.BTN_SEND_ANIMATION_MP4, callback_data=f"gif|{token}"
                ),
                InlineKeyboardButton(
                    Texts.BTN_SAVE_GIF_FILE, callback_data=f"giffile|{token}"
                )
            ]
        ]
    )
