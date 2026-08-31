from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.config import Settings
from app.db import Database
from app.keyboards import (
    BTN_SCAN,
    BTN_SESSIONS,
    collecting_actions_kb,
    main_menu_kb,
    proxies_kb,
)
from app.proxies import load_proxies
from app.services.session_manager import STAGE_LABEL
from app.states import Flow
from app.utils import session_card

router = Router()

WELCOME = (
    "<b>{scan}</b> — просканировать ссылку / включить сбор пушей\n"
    "<b>{sessions}</b> — активные сессии сбора пушей"
)


async def _begin_scan(message: Message, state: FSMContext, settings: Settings) -> None:
    proxies = load_proxies(settings.proxies_file)
    if not proxies:
        await message.answer("Прокси сервиса не настроены (proxies.json пуст).")
        return
    await state.set_state(Flow.choosing_proxy)
    await state.update_data(proxies=proxies)
    await message.answer("Выбери прокси:", reply_markup=proxies_kb(proxies))


async def _show_sessions(message: Message, db: Database) -> None:
    rows = await db.sessions_for_user(message.from_user.id)
    active = [r for r in rows if r["status"] == "collecting"]
    if not active:
        await message.answer("Активных сессий сбора нет. Нажми «" + BTN_SCAN + "».")
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
            ),
            reply_markup=collecting_actions_kb(r["id"], stage),
        )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, settings: Settings):
    await state.clear()
    await message.answer(
        WELCOME.format(scan=BTN_SCAN, sessions=BTN_SESSIONS),
        reply_markup=main_menu_kb(),
    )


@router.message(F.text == BTN_SCAN)
async def btn_scan(message: Message, state: FSMContext, settings: Settings):
    await state.clear()
    await _begin_scan(message, state, settings)


@router.message(F.text == BTN_SESSIONS)
async def btn_sessions(message: Message, state: FSMContext, db: Database):
    await state.clear()
    await _show_sessions(message, db)


@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext, settings: Settings):
    await state.clear()
    await _begin_scan(message, state, settings)


@router.message(Command("status"))
async def cmd_status(message: Message, db: Database):
    await _show_sessions(message, db)
