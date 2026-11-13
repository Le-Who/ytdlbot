"""In-process ownership for overlapping presses of one media request button."""

from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

_active: set[tuple[str, int, str]] = set()
_Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]


def exclusive_media_callback(handler: _Handler) -> _Handler:
    @wraps(handler)
    async def run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.message is None or not isinstance(query.data, str):
            await handler(update, context)
            return
        values = query.data.split("|")
        if values[0] == "m2":
            values = values[1:]
        if not values:
            await handler(update, context)
            return
        # Legacy private slideshows have no token, so their message owns the work.
        token = (
            values[1]
            if len(values) > 2 or values[0] == "send" and len(values) > 1
            else f"message:{query.message.message_id}"
        )
        key = (handler.__name__, query.message.chat.id, token)
        if key in _active:
            await query.answer()
            return
        _active.add(key)
        try:
            await handler(update, context)
        finally:
            _active.discard(key)

    return run
