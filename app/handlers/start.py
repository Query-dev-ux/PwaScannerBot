from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.access import NEED_KEY, can_collect
from app.config import Settings
from app.db import Database
from app.keyboards import (
    BTN_BACK,
    BTN_COLLECT,
    BTN_LINK,
    BTN_PUSH_MENU,
    BTN_SESSIONS,
    collecting_actions_kb,
    main_menu_kb,
    proxies_kb,
    push_menu_kb,
)
from app.proxies import load_proxies
from app.services.session_manager import STAGE_LABEL, SessionManager
from app.states import Flow
from app.utils import esc, session_card

router = Router()

WELCOME_PUBLIC = "<b>{link}</b> — пробить клоаку и достать ссылку внутри PWA"
WELCOME_FULL = (
    "<b>{link}</b> — пробить клоаку и достать ссылку внутри PWA\n"
    "<b>{push}</b> — запуск сбора push и активные сессии"
)


def _is_admin(settings: Settings, user_id: int) -> bool:
    return not settings.admin_ids or user_id in settings.admin_ids


async def _begin_scan(
    message: Message, state: FSMContext, settings: Settings, mode: str
) -> None:
    proxies = load_proxies(settings.proxies_file)
    if not proxies:
        await message.answer("Прокси не настроены (proxies.json пуст)")
        return
    await state.set_state(Flow.choosing_proxy)
    await state.update_data(proxies=proxies, mode=mode)
    prompt = {
        "link": "Прокси для поиска оффер-линка:",
        "probe": "Прокси для диагностики:",
        "js": "Прокси для выгрузки JS:",
    }.get(mode, "Прокси для сбора push:")
    await message.answer(prompt, reply_markup=proxies_kb(proxies))


async def _show_sessions(message: Message, db: Database) -> None:
    rows = await db.sessions_for_user(message.from_user.id)
    active = [r for r in rows if r["status"] == "collecting"]
    if not active:
        await message.answer("Активных сессий сбора нет")
        return
    for r in active:
        cnt = await db.count_pushes(r["id"])
        stage = r["stage"] or "install"
        await message.answer(
            session_card(
                r["pwa_name"] or r["site_url"], r["start_url"], r["deep_link"],
                STAGE_LABEL.get(stage, stage), cnt, None,
                bool(r["push_subscribed"]),
            ),
            reply_markup=collecting_actions_kb(r["id"], stage),
        )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, settings: Settings, db: Database):
    await state.clear()
    ok = await can_collect(db, settings, message.from_user.id)
    text = (WELCOME_FULL if ok else WELCOME_PUBLIC).format(
        link=BTN_LINK, push=BTN_PUSH_MENU
    )
    await message.answer(text, reply_markup=main_menu_kb(ok))


@router.message(Command("unlock"))
async def cmd_unlock(message: Message, command, settings: Settings, db: Database):
    key = (command.args or "").strip()
    if not settings.access_key:
        await message.answer("Доступ по ключу не настроен (ACCESS_KEY)")
        return
    if key != settings.access_key:
        await message.answer("Неверный ключ")
        return
    await db.authorize(message.from_user.id)
    await message.answer(
        "✅ Доступ к сбору push открыт", reply_markup=main_menu_kb(True)
    )


@router.message(Command("lock"))
async def cmd_lock(message: Message, db: Database):
    await db.deauthorize(message.from_user.id)
    await message.answer("Доступ к сбору push закрыт", reply_markup=main_menu_kb(False))


@router.message(Command("users"))
async def cmd_users(message: Message, settings: Settings, db: Database):
    if not _is_admin(settings, message.from_user.id):
        await message.answer("Только для админа")
        return
    ids = await db.list_authorized()
    if not ids:
        await message.answer("Нет пользователей с доступом")
        return
    lines = ["<b>Доступ к сбору push:</b>"]
    lines += [f"• <code>{uid}</code>" for uid in ids]
    lines.append("\nОтозвать: <code>/revoke ID</code>")
    await message.answer("\n".join(lines))


@router.message(Command("grant"))
async def cmd_grant(message: Message, command, settings: Settings, db: Database):
    if not _is_admin(settings, message.from_user.id):
        await message.answer("Только для админа")
        return
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await message.answer("Формат: <code>/grant ID</code>")
        return
    await db.authorize(int(arg))
    await message.answer(f"✅ Доступ выдан пользователю <code>{arg}</code>")


@router.message(Command("revoke"))
async def cmd_revoke(
    message: Message, command, settings: Settings, db: Database,
    manager: SessionManager,
):
    if not _is_admin(settings, message.from_user.id):
        await message.answer("Только для админа")
        return
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await message.answer("Формат: <code>/revoke ID</code>")
        return
    uid = int(arg)
    await db.deauthorize(uid)
    # stop that user's active collecting sessions
    stopped = 0
    for sid in list(manager._sessions):
        s = manager._sessions.get(sid)
        if s and s.get("user_id") == uid:
            try:
                await manager.cancel(sid)
                stopped += 1
            except Exception:  # noqa: BLE001
                pass
    tail = f", закрыто сессий: {stopped}" if stopped else ""
    await message.answer(f"🚫 Доступ отозван у <code>{uid}</code>{tail}")
    try:
        await message.bot.send_message(
            uid, "🚫 Доступ к сбору push отозван",
            reply_markup=main_menu_kb(False),
        )
    except Exception:  # noqa: BLE001
        pass


@router.message(F.text == BTN_LINK)
async def btn_link(message: Message, state: FSMContext, settings: Settings):
    await state.clear()
    await _begin_scan(message, state, settings, mode="link")


@router.message(F.text == BTN_PUSH_MENU)
async def btn_push_menu(message: Message, state: FSMContext, settings: Settings, db: Database):
    await state.clear()
    if not await can_collect(db, settings, message.from_user.id):
        await message.answer(NEED_KEY)
        return
    await message.answer(
        f"<b>{BTN_PUSH_MENU}</b>\n\n"
        f"<b>{BTN_COLLECT}</b> — пробить клоаку и начать сбор push\n"
        f"<b>{BTN_SESSIONS}</b> — активные сессии сбора",
        reply_markup=push_menu_kb(),
    )


@router.message(F.text == BTN_BACK)
async def btn_back(message: Message, state: FSMContext, settings: Settings, db: Database):
    await state.clear()
    ok = await can_collect(db, settings, message.from_user.id)
    await message.answer("Главное меню", reply_markup=main_menu_kb(ok))


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


@router.message(Command("probe"))
async def cmd_probe(message: Message, state: FSMContext, settings: Settings, db: Database):
    await state.clear()
    if not await can_collect(db, settings, message.from_user.id):
        await message.answer(NEED_KEY)
        return
    await _begin_scan(message, state, settings, mode="probe")


@router.message(Command("js"))
async def cmd_js(message: Message, state: FSMContext, settings: Settings, db: Database):
    await state.clear()
    if not await can_collect(db, settings, message.from_user.id):
        await message.answer(NEED_KEY)
        return
    await _begin_scan(message, state, settings, mode="js")


@router.message(Command("status"))
async def cmd_status(message: Message, settings: Settings, db: Database):
    if not await can_collect(db, settings, message.from_user.id):
        await message.answer(NEED_KEY)
        return
    await _show_sessions(message, db)


@router.message(Command("subcheck"))
async def cmd_subcheck(message: Message, settings: Settings, db: Database,
                       manager: SessionManager):
    import asyncio as _a
    if not await can_collect(db, settings, message.from_user.id):
        await message.answer(NEED_KEY)
        return
    sids = list(manager._sessions)
    if not sids:
        await message.answer("Активных сессий в памяти нет (после рестарта не восстанавливаются)")
        return
    for sid in sids:
        sess = manager._sessions.get(sid)
        if not sess:
            continue
        try:
            r = await _a.to_thread(manager.subcheck, sess)
        except Exception as e:  # noqa: BLE001
            await message.answer(f"<code>{sid[:8]}</code>: ошибка {esc(str(e))}")
            continue
        if r.get("dead"):
            await message.answer(
                f"<code>{sid[:8]}</code> {esc(sess.get('pwa_name',''))}: "
                f"❌ браузер мёртв — {esc(r.get('err',''))}")
            continue
        lines = [f"<code>{sid[:8]}</code> <b>{esc(sess.get('pwa_name',''))}</b>",
                 f"url: {esc((r.get('url') or '')[:80])}",
                 f"Notification.permission: {esc(str(r.get('perm')))}"]
        for reg in r.get("regs", []):
            lines.append(
                f"• SW {esc((reg.get('sw') or '').split('/')[-1])} scope={esc(reg.get('scope',''))}\n"
                f"  push: {'✅ ' + esc(reg['endpoint']) if reg.get('endpoint') else '⚠️ нет подписки'}")
        if r.get("err"):
            lines.append(f"err: {esc(r['err'])}")
        await message.answer("\n".join(lines), disable_web_page_preview=True)
