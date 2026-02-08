from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def build_format_keyboard(formats: list, special_format) -> InlineKeyboardMarkup:
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
        [InlineKeyboardButton(special_format.label, callback_data=f"pick|{special_format.format_id}")]
    )
    return InlineKeyboardMarkup(buttons)
