from __future__ import annotations

NEED_KEY = "Нужен доступ: <code>/unlock КЛЮЧ</code>"


def is_admin(settings, user_id: int) -> bool:
    """Admins bypass every gate. If ADMIN_IDS is unset, nobody is an admin —
    access is then controlled purely by ACCESS_KEY / the authorized table."""
    return bool(settings.admin_ids) and user_id in settings.admin_ids


async def has_access(db, settings, user_id: int) -> bool:
    """Full-bot gate. Open to everyone only when ACCESS_KEY is unset."""
    if is_admin(settings, user_id):
        return True
    if not settings.access_key:
        return True
    return await db.is_authorized(user_id)


# kept as an alias — the bot is single-tier now (access == everything)
can_collect = has_access
