from __future__ import annotations

NEED_KEY = "Нужен доступ: <code>/unlock КЛЮЧ</code>"


async def can_collect(db, settings, user_id: int) -> bool:
    """Push-collection features. Open to all when ACCESS_KEY is unset."""
    if not settings.access_key:
        return True
    return await db.is_authorized(user_id)
