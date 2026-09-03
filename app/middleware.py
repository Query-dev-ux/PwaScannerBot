from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.access import NEED_KEY, has_access

# commands anyone may run even without access
_OPEN_CMDS = ("/unlock", "/start")


class AccessMiddleware(BaseMiddleware):
    """One gate for the whole bot. An unauthorized user gets nothing except
    /unlock (and a bare /start telling them how to unlock)."""

    def __init__(self, settings, db) -> None:
        self.settings = settings
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        settings = data.get("settings") or self.settings
        db = data.get("db") or self.db

        user = getattr(event, "from_user", None)
        if user is None and isinstance(event, Update):
            inner = event.message or event.callback_query
            user = getattr(inner, "from_user", None)
        if user is None:
            return await handler(event, data)

        if await has_access(db, settings, user.id):
            return await handler(event, data)

        text = (getattr(event, "text", "") or "").strip()
        if text.split()[0:1] and text.split()[0].split("@")[0] in _OPEN_CMDS:
            return await handler(event, data)

        # blocked
        if isinstance(event, Message):
            await event.answer(NEED_KEY)
        elif isinstance(event, CallbackQuery):
            await event.answer(NEED_KEY, show_alert=True)
        return None
