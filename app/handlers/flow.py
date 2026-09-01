from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.db import Database
from app.keyboards import collecting_actions_kb, enable_push_kb
from app.services.session_manager import (
    STAGE_LABEL,
    STAGE_ORDER,
    SessionLimit,
    SessionManager,
)
from app.states import Flow
from app.utils import esc, is_http_url, session_card

router = Router()


# ------------------------------------------------------------------ scan flow
@router.callback_query(Flow.choosing_proxy, F.data.startswith("proxy:"))
async def choose_proxy(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":", 1)[1])
    data = await state.get_data()
    try:
        proxy = data["proxies"][idx]
    except (KeyError, IndexError):
        await cb.answer("Прокси не найден, начни заново", show_alert=True)
        return

    await state.update_data(proxy=proxy)
    await state.set_state(Flow.waiting_url)
    await cb.message.answer(
        f"Прокси: <b>{esc(proxy['name'])}</b>\n\nПришли ссылку на PWA (https://…)"
    )
    await cb.answer()


@router.message(Flow.waiting_url, F.text)
async def got_url(message: Message, state: FSMContext, manager: SessionManager):
    url = message.text.strip()
    if not is_http_url(url):
        await message.answer("Нужна ссылка вида https://example.com")
        return

    data = await state.get_data()
    proxy = data.get("proxy")

    status = await message.answer("⏳ Открываю сайт, устанавливаю PWA, ищу ссылку внутри PWA…")
    try:
        res = await manager.open_site(message.from_user.id, message.chat.id, proxy, url)
    except SessionLimit:
        await status.edit_text("Слишком много активных сессий. Попробуй позже.")
        await state.clear()
        return
    except Exception as e:  # noqa: BLE001
        await status.edit_text(f"Ошибка: <code>{esc(str(e))}</code>")
        await state.clear()
        return

    await state.clear()
    await status.edit_text(
        session_card(res.name, res.start_url, res.deep_link)
        + "\n\nСессия <b>не сохранена</b>. Включи сбор пушей, чтобы начать.",
        reply_markup=enable_push_kb(res.session_id),
    )


# ------------------------------------------------------------------ collection
@router.callback_query(F.data.startswith("push_on:"))
async def push_on(cb: CallbackQuery, manager: SessionManager):
    session_id = cb.data.split(":", 1)[1]
    await cb.answer("Включаю…")
    try:
        info = await manager.enable_push_collection(session_id)
    except Exception as e:  # noqa: BLE001
        await cb.message.answer(f"Не удалось включить сбор: <code>{esc(str(e))}</code>")
        return
    until = datetime.fromtimestamp(info.expires_at).strftime("%d.%m %H:%M")
    tail = (
        "\n\nОткрой браузер сессии (🖥), зарегистрируйся и внеси депозит, "
        "затем отмечай стадию."
    )
    if not info.push_subscribed:
        tail += (
            "\n\n⚠️ Push-подписка не создана — воронка не подписала браузер "
            "автоматически. Пуши могут не приходить. Открой браузер сессии и "
            "пройди воронку/установку до конца."
        )
    await cb.message.answer(
        session_card(
            info.name, info.download_url, info.deep_link,
            STAGE_LABEL.get(info.stage, info.stage), 0, until, info.push_subscribed,
        )
        + tail,
        reply_markup=collecting_actions_kb(session_id, info.stage),
    )


@router.callback_query(F.data.startswith("stage:"))
async def advance_stage(cb: CallbackQuery, manager: SessionManager, db: Database):
    _, session_id, stage = cb.data.split(":", 2)
    if stage not in STAGE_ORDER:
        await cb.answer("?", show_alert=True)
        return
    try:
        await manager.set_stage(session_id, stage)
    except Exception as e:  # noqa: BLE001
        await cb.answer(str(e), show_alert=True)
        return
    row = await db.get_session(session_id)
    cnt = await db.count_pushes(session_id)
    until = (
        datetime.fromtimestamp(row["expires_at"]).strftime("%d.%m %H:%M")
        if row and row["expires_at"] else None
    )
    await cb.answer(f"Стадия: {STAGE_LABEL.get(stage, stage)}")
    try:
        await cb.message.edit_text(
            session_card(
                row["pwa_name"] or row["site_url"], row["start_url"], row["deep_link"],
                STAGE_LABEL.get(stage, stage), cnt, until,
                bool(row["push_subscribed"]),
            ),
            reply_markup=collecting_actions_kb(session_id, stage),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("pv:"))
async def view_pushes(cb: CallbackQuery, db: Database):
    session_id = cb.data.split(":", 1)[1]
    row = await db.get_session(session_id)
    if not row:
        await cb.answer("Сессия не найдена", show_alert=True)
        return
    await cb.answer()
    pushes = [p for p in await db.list_pushes(session_id) if (p["service"] or "") != "stage"]
    if not pushes:
        await cb.message.answer("Пушей пока нет.")
        return

    groups: dict = {}
    for p in pushes:
        groups.setdefault(p["stage"], []).append(p)
    out = [f"📥 <b>{esc(row['pwa_name'] or row['site_url'])}</b> — всего {len(pushes)}"]
    for stage in STAGE_ORDER + [None]:
        g = groups.get(stage)
        if not g:
            continue
        out.append(f"\n<b>— {esc(STAGE_LABEL.get(stage, 'без стадии'))} ({len(g)})</b>")
        for p in g[-15:]:
            ts = datetime.fromtimestamp(p["ts"]).strftime("%d.%m %H:%M")
            t = esc(p["title"] or p["event"] or "—")
            b = esc((p["body"] or "").strip())
            out.append(f"• <b>{ts}</b> {t}" + (f"\n  {b}" if b else ""))
    await cb.message.answer("\n".join(out)[:4000])


@router.callback_query(F.data.startswith("pdl:"))
async def download_pack(cb: CallbackQuery, manager: SessionManager, db: Database):
    session_id = cb.data.split(":", 1)[1]
    if not await db.get_session(session_id):
        await cb.answer("Сессия не найдена", show_alert=True)
        return
    await cb.answer("Собираю архив…")
    try:
        await manager.export_pack(session_id)
    except Exception as e:  # noqa: BLE001
        await cb.message.answer(f"Ошибка: <code>{esc(str(e))}</code>")


@router.callback_query(F.data.startswith("pstop:"))
async def stop_and_deliver(cb: CallbackQuery, manager: SessionManager, db: Database):
    session_id = cb.data.split(":", 1)[1]
    if not await db.get_session(session_id):
        await cb.answer("Сессия не найдена", show_alert=True)
        return
    await cb.answer("Останавливаю и собираю архив…")
    try:
        await manager.deliver(session_id)
        await cb.message.answer("⏹ Сбор остановлен, архив отправлен, браузер закрыт.")
    except Exception as e:  # noqa: BLE001
        await cb.message.answer(f"Ошибка: <code>{esc(str(e))}</code>")


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_session(cb: CallbackQuery, manager: SessionManager):
    session_id = cb.data.split(":", 1)[1]
    await manager.cancel(session_id)
    await cb.answer("Сессия отменена")
    await cb.message.answer("🗑 Сессия отменена, браузер закрыт.")


@router.message(Command("cancel_all"))
async def cancel_all(message: Message, state: FSMContext, manager: SessionManager, db: Database):
    await state.clear()
    n = 0
    for sid in list(manager._sessions):
        await manager.cancel(sid)
        n += 1
    await message.answer(f"Закрыто сессий: {n}")


# ------------------------------------------------------------- live browser
@router.callback_query(F.data.startswith("ctl:"))
async def ctl_open(cb: CallbackQuery, manager: SessionManager):
    session_id = cb.data.split(":", 1)[1]
    if session_id not in manager._sessions:
        await cb.answer("Сессия не активна", show_alert=True)
        return
    url = manager.control_url(session_id)
    await cb.answer()
    if not url:
        await cb.message.answer("Веб-доступ не настроен (PUBLIC_URL).")
        return
    await cb.message.answer(
        f'🖥 <a href="{esc(url)}"><b>Открыть живой браузер сессии</b></a>\n\n'
        "Увидишь экран сессии и сможешь кликать, печатать и вводить URL. "
        "Зарегистрируйся и внеси депозит здесь, затем отметь стадию.\n\n"
        f"<code>{esc(url)}</code>",
        disable_web_page_preview=True,
    )
