from __future__ import annotations

NEED_KEY = "Нужен доступ: <code>/unlock КЛЮЧ</code>"
NEED_PUSH = "Нужен доступ к сбору push — обратись к админу"

# access tiers, low → high
SCAN = "scan"   # only "🔎 Сканер PWA"
PUSH = "push"   # everything, incl. push collection


def is_admin(settings, user_id: int) -> bool:
    """Admins bypass every gate. If ADMIN_IDS is unset, nobody is an admin —
    access is then controlled purely by the keys / the authorized table."""
    return bool(settings.admin_ids) and user_id in settings.admin_ids


async def access_level(db, settings, user_id: int) -> str | None:
    """Highest tier the user holds: "push", "scan" or None.

    - admins → "push"
    - a stored level is honoured as-is
    - ACCESS_KEY unset  → the scan tier is open to everyone
    - PUSH_KEY unset    → anyone with scan access also gets push
    """
    if is_admin(settings, user_id):
        return PUSH
    stored = await db.auth_level(user_id)          # "push" | "scan" | None
    has_scan = stored in (SCAN, PUSH) or not settings.access_key
    has_push = stored == PUSH or (not settings.push_key and has_scan)
    if has_push:
        return PUSH
    if has_scan:
        return SCAN
    return None


async def has_access(db, settings, user_id: int) -> bool:
    """Front door — any tier may use the bot."""
    return await access_level(db, settings, user_id) is not None


async def can_push(db, settings, user_id: int) -> bool:
    """Push-collection tier."""
    return await access_level(db, settings, user_id) == PUSH


# kept as an alias for the scan tier / front door
can_collect = has_access
