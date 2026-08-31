from __future__ import annotations

from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

BTN_SCAN = "🔍 Scan"
BTN_SESSIONS = "🔔 Сессии"


def main_menu_kb():
    b = ReplyKeyboardBuilder()
    b.button(text=BTN_SCAN)
    b.button(text=BTN_SESSIONS)
    b.adjust(2)
    return b.as_markup(resize_keyboard=True, is_persistent=True)


def proxies_kb(proxies: list[dict]):
    b = InlineKeyboardBuilder()
    for i, p in enumerate(proxies):
        b.button(text=f"🌐 {p['name']}", callback_data=f"proxy:{i}")
    b.adjust(1)
    return b.as_markup()


def enable_push_kb(session_id: str):
    b = InlineKeyboardBuilder()
    b.button(text="🔔 Включить сбор пушей", callback_data=f"push_on:{session_id}")
    b.button(text="🗑 Отменить сессию", callback_data=f"cancel:{session_id}")
    b.adjust(1)
    return b.as_markup()


_STAGE_ADVANCE = {
    "install": ("✅ Зарегистрировался", "registration"),
    "registration": ("✅ Внёс депозит", "deposit"),
}


def collecting_actions_kb(session_id: str, stage: str | None = None):
    b = InlineKeyboardBuilder()
    b.button(text="🖥 Открыть браузер сессии", callback_data=f"ctl:{session_id}")
    adv = _STAGE_ADVANCE.get(stage or "install")
    if adv:
        b.button(text=adv[0], callback_data=f"stage:{session_id}:{adv[1]}")
    b.button(text="📥 Пуши", callback_data=f"pv:{session_id}")
    b.button(text="⏹ Стоп + архив", callback_data=f"pstop:{session_id}")
    b.adjust(1, 1, 2)
    return b.as_markup()
