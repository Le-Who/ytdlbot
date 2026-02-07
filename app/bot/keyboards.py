from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def build_format_keyboard(formats: list, audio) -> InlineKeyboardMarkup:
    """Helper to build format selection buttons in 2 columns."""
    buttons = []
    formats_slice = formats[:8]
    for i in range(0, len(formats_slice), 2):
        row = [
            InlineKeyboardButton(
                formats_slice[i].label,
                callback_data=f"pick|{formats_slice[i].format_id}",
            )
        ]
        if i + 1 < len(formats_slice):
            row.append(
                InlineKeyboardButton(
                    formats_slice[i + 1].label,
                    callback_data=f"pick|{formats_slice[i+1].format_id}",
                )
            )
        buttons.append(row)

    buttons.append(
        [InlineKeyboardButton(audio.label, callback_data=f"pick|{audio.format_id}")]
    )
    return InlineKeyboardMarkup(buttons)
