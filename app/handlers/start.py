from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.access import NEED_KEY, can_collect
from app.config import Settings
from app.db import Database
from app.keyboards import (
    BTN_COLLECT,
    BTN_LINK,
    BTN_SESSIONS,
    collecting_actions_kb,
    main_menu_kb,
    proxies_kb,
)
from app.proxies import load_proxies
from app.services.session_manager import STAGE_LABEL
from app.states import Flow
from app.utils import esc, session_card

router = Router()

WELCOME_PUBLIC = (
    "<b>{link}</b> — быстро достать ссылку внутри PWA (оффер-линк)\n\n"
    "Сбор push-уведомлений — по доступу: <code>/unlock КЛЮЧ</code>"
)
WELCOME_FULL = (
    "<b>{link}</b> — быстро достать ссылку внутри PWA\n"
    "<b>{collect}</b> — пробить клоаку и запустить сбор push\n"
    "<b>{sessions}</b> — активные сессии сбора push"
)


async def _begin_scan(
    message: Message, state: FSMContext, settings: Settings, mode: str
) -> None:
    proxies = load_proxies(settings.proxies_file)
    if not proxies:
        await message.answer("Прокси не настроены (proxies.json пуст).")
        return
    await state.set_state(Flow.choosing_proxy)
    await state.update_data(proxies=proxies, mode=mode)
    prompt = "Прокси для поиска оффер-линка:" if mode == "link" else "Прокси для сбора push:"
    await message.answer(prompt, reply_markup=proxies_kb(proxies))


async def _show_sessions(message: Message, db: Database) -> None:
    rows = await db.sessions_for_user(message.from_user.id)
    active = [r for r in rows if r["status"] == "collecting"]
    if not active:
        await message.answer("Активных сессий сбора нет.")
        return
    for r in active:
        cnt = await db.count_pushes(r["id"])
        stage = r["stage"] or "install"
        until = (
            datetime.fromtimestamp(r["expires_at"]).strftime("%d.%m %H:%M")
            if r["expires_at"] else None
        )
        await message.answer(
            session_card(
                r["pwa_name"] or r["site_url"], r["start_url"], r["deep_link"],
                STAGE_LABEL.get(stage, stage), cnt, until,
                bool(r["push_subscribed"]),
            ),
            reply_markup=collecting_actions_kb(r["id"], stage),
        )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, settings: Settings, db: Database):
    await state.clear()
    ok = await can_collect(db, settings, message.from_user.id)
    text = (WELCOME_FULL if ok else WELCOME_PUBLIC).format(
        link=BTN_LINK, collect=BTN_COLLECT, sessions=BTN_SESSIONS
    )
    await message.answer(text, reply_markup=main_menu_kb(ok))


@router.message(Command("unlock"))
async def cmd_unlock(message: Message, command, settings: Settings, db: Database):
    key = (command.args or "").strip()
    if not settings.access_key:
        await message.answer("Доступ по ключу не настроен (ACCESS_KEY).")
        return
    if key != settings.access_key:
        await message.answer("Неверный ключ.")
        return
    await db.authorize(message.from_user.id)
    await message.answer(
        "✅ Доступ к сбору push открыт.", reply_markup=main_menu_kb(True)
    )


@router.message(Command("lock"))
async def cmd_lock(message: Message, db: Database):
    await db.deauthorize(message.from_user.id)
    await message.answer("Доступ к сбору push закрыт.", reply_markup=main_menu_kb(False))


@router.message(F.text == BTN_LINK)
async def btn_link(message: Message, state: FSMContext, settings: Settings):
    await state.clear()
    await _begin_scan(message, state, settings, mode="link")


@router.message(F.text == BTN_COLLECT)
async def btn_collect(message: Message, state: FSMContext, settings: Settings, db: Database):
    await state.clear()
    if not await can_collect(db, settings, message.from_user.id):
        await message.answer(NEED_KEY)
        return
    await _begin_scan(message, state, settings, mode="collect")


@router.message(F.text == BTN_SESSIONS)
async def btn_sessions(message: Message, state: FSMContext, settings: Settings, db: Database):
    await state.clear()
    if not await can_collect(db, settings, message.from_user.id):
        await message.answer(NEED_KEY)
        return
    await _show_sessions(message, db)


@router.message(Command("status"))
async def cmd_status(message: Message, settings: Settings, db: Database):
    if not await can_collect(db, settings, message.from_user.id):
        await message.answer(NEED_KEY)
        return
    await _show_sessions(message, db)
