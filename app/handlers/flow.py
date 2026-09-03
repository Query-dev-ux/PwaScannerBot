from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.access import NEED_PUSH, can_push
from app.config import Settings
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
    mode = data.get("mode", "collect")
    await state.clear()

    if mode == "probe":
        await message.answer("🔬 Диагностика… (30–90 сек, пришлю отчёт)")
        try:
            await manager.probe_offer(message.chat.id, proxy, url)
        except Exception as e:  # noqa: BLE001
            await message.answer(f"Ошибка: <code>{esc(str(e))}</code>")
        return

    if mode == "js":
        await message.answer("📥 Выгружаю JS воронки…")
        try:
            await manager.dump_js(message.chat.id, proxy, url)
        except Exception as e:  # noqa: BLE001
            await message.answer(f"Ошибка: <code>{esc(str(e))}</code>")
        return

    if mode == "link":
        status = await message.answer("⏳ Пробиваю клоаку, ищу ссылку внутри PWA…")
        try:
            res = await manager.extract_link(proxy, url)
        except Exception as e:  # noqa: BLE001
            await status.edit_text(f"Ошибка: <code>{esc(str(e))}</code>")
            return
        await status.edit_text(
            f"Название PWA: <b>{esc(res['name'])}</b>\n"
            f"Ссылка на PWA: {esc(res['start_url'])}\n"
            f"Ссылка внутри PWA: {esc(res['deep_link'])}",
            disable_web_page_preview=True,
        )
        return

    status = await message.answer("⏳ Пробиваю клоаку, ставлю PWA, подписываюсь на push…")
    try:
        res = await manager.open_site(message.from_user.id, message.chat.id, proxy, url)
    except SessionLimit:
        await status.edit_text("Слишком много активных сессий, попробуй позже")
        return
    except Exception as e:  # noqa: BLE001
        await status.edit_text(f"Ошибка: <code>{esc(str(e))}</code>")
        return

    tail = "\n\nСессия <b>не сохранена</b> — включи сбор push, чтобы начать"
    if res.shell:
        tail = (
            "\n\n⚠️ Воронка вернула <b>пустую страницу</b> (нет manifest, пустой "
            "body) — обычно это декой клоаки"
        )
        if res.exit_hosting and not res.exit_mobile:
            tail += (
                f"\n\n🛑 Выходной IP <code>{esc(res.exit_ip or '?')}</code> "
                f"(<i>{esc(res.exit_isp or '?')}</i>) — это <b>дата-центр/хостинг</b>, "
                "клоаки мобильных офферов режут такие IP сразу. "
                "Нужна <b>мобильная</b> или резидентная прокси гео-страны оффера"
            )
        else:
            tail += (
                f"\n\nВыходной IP: <code>{esc(res.exit_ip or '?')}</code> "
                f"(<i>{esc(res.exit_isp or '?')}</i>)"
            )
        tail += (
            "\n\nЕщё проверь: ссылка с <b>валидными</b> трек-параметрами "
            "(не пустая и не с плейсхолдерами <code>{{campaign.name}}</code>, "
            "<code>{pixel}</code>), гео прокси совпадает с гео оффера"
        )
    await status.edit_text(
        session_card(res.name, res.start_url, res.deep_link,
                     push_subscribed=res.push_subscribed)
        + tail,
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
    tail = (
        "\n\nОткрой браузер сессии (🖥), зарегистрируйся и внеси депозит, "
        "затем отмечай стадию"
    )
    if not info.push_subscribed:
        tail += (
            "\n\n⚠️ Push-подписка не создана — воронка не подписала браузер "
            "автоматически, пуши могут не приходить. Открой браузер сессии и "
            "пройди воронку/установку до конца"
        )
    await cb.message.answer(
        session_card(
            info.name, info.download_url, info.deep_link,
            STAGE_LABEL.get(info.stage, info.stage), 0, None, info.push_subscribed,
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
    await cb.answer(f"Стадия: {STAGE_LABEL.get(stage, stage)}")
    try:
        await cb.message.edit_text(
            session_card(
                row["pwa_name"] or row["site_url"], row["start_url"], row["deep_link"],
                STAGE_LABEL.get(stage, stage), cnt, None,
                bool(row["push_subscribed"]),
            ),
            reply_markup=collecting_actions_kb(session_id, stage),
        )
    except Exception:
        pass


def _row_img(p) -> str | None:
    try:
        v = p["image"]
    except (KeyError, IndexError, TypeError):
        v = None
    if v:
        return v
    try:
        import json as _j
        from app.utils import extract_push_fields
        if p["raw"]:
            return extract_push_fields(_j.loads(p["raw"])).get("image")
    except Exception:
        return None
    return None


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
        await cb.message.answer("Пушей пока нет")
        return

    groups: dict = {}
    for p in pushes:
        groups.setdefault(p["stage"], []).append(p)
    out = [f"📥 <b>{esc(row['pwa_name'] or row['site_url'])}</b> — всего {len(pushes)}"]
    for stage in STAGE_ORDER + [None]:
        g = groups.get(stage)
        if not g:
            continue
        label = str(STAGE_LABEL.get(stage, "без стадии")).capitalize()
        out.append(f"\n<b>{esc(label)} ({len(g)})</b>\n")
        for p in g[-15:]:
            ts = datetime.fromtimestamp(p["ts"]).strftime("%d.%m %H:%M")
            t = esc(p["title"] or p["event"] or "—")
            b = esc((p["body"] or "").strip())
            img = _row_img(p)
            line = f"• <b>{ts}</b> {t}"
            if b:
                line += f"\n  {b}"
            if img:
                line += f'\n  <a href="{esc(img)}">🖼 картинка</a>'
            out.append(line + "\n")
    await cb.message.answer("\n".join(out)[:4000], disable_web_page_preview=True)


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
        await cb.message.answer("⏹ Сбор остановлен, архив отправлен, браузер закрыт")
    except Exception as e:  # noqa: BLE001
        await cb.message.answer(f"Ошибка: <code>{esc(str(e))}</code>")


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_session(cb: CallbackQuery, manager: SessionManager):
    session_id = cb.data.split(":", 1)[1]
    await manager.cancel(session_id)
    await cb.answer("Сессия отменена")
    await cb.message.answer("🗑 Сессия отменена, браузер закрыт")


@router.message(Command("cancel_all"))
async def cancel_all(
    message: Message, state: FSMContext, manager: SessionManager,
    db: Database, settings: Settings,
):
    await state.clear()
    if not await can_push(db, settings, message.from_user.id):
        await message.answer(NEED_PUSH)
        return
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
        await cb.message.answer("Веб-доступ не настроен (PUBLIC_URL)")
        return
    await cb.message.answer(
        f'🖥 <a href="{esc(url)}"><b>Открыть живой браузер сессии</b></a>\n\n'
        "Увидишь экран сессии, сможешь кликать, печатать и вводить URL. "
        "Зарегистрируйся и внеси депозит здесь, затем отметь стадию\n\n"
        f"<code>{esc(url)}</code>",
        disable_web_page_preview=True,
    )
