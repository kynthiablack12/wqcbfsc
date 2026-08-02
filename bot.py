import os
import sys
import time
import asyncio
import logging
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import database as db
import edadeal
import scheduler as sched
from config import TG_BOT_TOKEN, YANDEX_OAUTH_URL

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)

WAITING_TOKEN = {}
SPIN_ALL_STOP = {}
EDIT_LOCKS = {}
LAST_ACTION = {}       # uid -> timestamp
RATE_LIMIT_SEC = 0.5
ACCOUNTS_CACHE = {}    # uid -> {"accounts": [...], "page": int, "total_pages": int}

# DDoS protection
_ACTION_HISTORY = {}    # uid -> [timestamps]
_BURST_WINDOW = 5       # seconds
_BURST_MAX = 10         # max actions in window
_MUTED = {}             # uid -> until timestamp
_GLOBAL_HISTORY = []    # [timestamps]
_GLOBAL_MAX = 30        # max actions per second globally


def _check_ratelimit(uid):
    """Returns True if request should be allowed."""
    now = time.time()

    # Check mute
    if uid in _MUTED and now < _MUTED[uid]:
        return False
    if uid in _MUTED and now >= _MUTED[uid]:
        del _MUTED[uid]

    # Per-user burst detection
    timestamps = _ACTION_HISTORY.get(uid, [])
    timestamps = [t for t in timestamps if now - t < _BURST_WINDOW]
    if len(timestamps) >= _BURST_MAX:
        _MUTED[uid] = now + 60
        del _ACTION_HISTORY[uid]
        logging.warning(f"User {uid} muted for 60s (burst)")
        return False
    timestamps.append(now)
    _ACTION_HISTORY[uid] = timestamps

    # Global rate limit
    _GLOBAL_HISTORY.append(now)
    _GLOBAL_HISTORY[:] = [t for t in _GLOBAL_HISTORY if now - t < 1]
    if len(_GLOBAL_HISTORY) > _GLOBAL_MAX:
        return False

    return True


def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account"),
            InlineKeyboardButton("📋 Мои аккаунты", callback_data="list_accounts"),
        ],
        [
            InlineKeyboardButton("📊 Статистика", callback_data="stats"),
            InlineKeyboardButton("📖 Помощь", callback_data="help"),
        ],
        [
            InlineKeyboardButton("📅 Отметиться", callback_data="daily_checkin"),
        ],
    ])


def account_keyboard(account_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑 Удалить", callback_data=f"confirm_delete:{account_id}"),
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="list_accounts")],
    ])


def back_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ В меню", callback_data="menu")],
    ])


def after_add_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить ещё", callback_data="add_account")],
        [InlineKeyboardButton("◀️ В меню", callback_data="menu")],
    ])


ACCOUNTS_PAGE_SIZE = 10


def _account_pages_kb(total_pages, page):
    buttons = []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️", callback_data=f"accounts_page:{page - 1}"))
    row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("▶️", callback_data=f"accounts_page:{page + 1}"))
    buttons.append(row)
    buttons.append([InlineKeyboardButton("🔄 Обновить балансы", callback_data="refresh_balances")])
    buttons.append([InlineKeyboardButton("➕ Добавить", callback_data="add_account")])
    buttons.append([InlineKeyboardButton("◀️ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def _accounts_text(uid, page):
    cache = ACCOUNTS_CACHE.get(uid)
    if not cache:
        return None, None
    total_pages = cache["total_pages"]
    page = max(0, min(page, total_pages - 1))
    cache["page"] = page
    start = page * ACCOUNTS_PAGE_SIZE
    chunk = cache["accounts"][start:start + ACCOUNTS_PAGE_SIZE]
    buttons = []
    total_bal = 0
    for acc in chunk:
        s = "✅" if acc["status"] == "active" else "⚠️"
        bal = acc["last_balance"] or 0
        total_bal += bal
        buttons.append([
            InlineKeyboardButton(
                f"{s} {acc['login']}  💎{bal}",
                callback_data=f"account:{acc['id']}",
            )
        ])
    kb = _account_pages_kb(total_pages, page)
    buttons.extend(list(kb.inline_keyboard))
    return total_bal, InlineKeyboardMarkup(buttons)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.username or "", user.first_name or "")
    await update.message.reply_text(
        f"🎡 <b>Привет, {user.first_name}!</b>\n\n"
        "Бот собирает 250 баллов Плюс\n"
        "в приложение Едадил покупая их за 1 алмаз.\n\n"
        "Выбери действие:",
        reply_markup=main_menu(), parse_mode="HTML",
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    try:
        await q.answer()
    except Exception:
        pass
    if not _check_ratelimit(uid):
        return
    now = time.time()
    if uid in LAST_ACTION and now - LAST_ACTION[uid] < RATE_LIMIT_SEC:
        return
    LAST_ACTION[uid] = now
    data = q.data

    if data == "menu":
        await q.edit_message_text(
            "🎡 <b>Меню</b>", reply_markup=main_menu(), parse_mode="HTML",
        )

    elif data == "spin_all":
        accounts = db.get_user_accounts(uid)
        active = [a for a in accounts if a["status"] == "active"]
        if not active:
            await q.edit_message_text(
                "❌ Нет активных аккаунтов\nДобавь: /add",
                reply_markup=back_kb(), parse_mode="HTML",
            )
            return

        SPIN_ALL_STOP[uid] = False
        stop_flag = SPIN_ALL_STOP
        edit_lock = asyncio.Lock()
        EDIT_LOCKS[uid] = edit_lock
        stop_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⛔ Стоп", callback_data="spin_all_stop")]
        ])

        msg = await q.edit_message_text(
            f"🎡 <b>Кручу все аккаунты одновременно...</b>\n\n"
            f"Аккаунтов: {len(active)}\n\n"
            f"<i>Уведомления о выигрышах приходят отдельно.\n"
            f"Нажми «Стоп» чтобы остановить</i>",
            reply_markup=stop_kb, parse_mode="HTML",
        )

        async def send_notification(text):
            async with edit_lock:
                await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")

        tasks = [
            sched.spin_account_loop(acc, stop_flag, uid, send_notification)
            for acc in active
        ]

        total_spins = 0
        total_balance = 0

        for coro in asyncio.as_completed(tasks):
            if SPIN_ALL_STOP.get(uid):
                for t in tasks:
                    t.cancel()
                break

            try:
                res = await coro
            except asyncio.CancelledError:
                break

            total_spins += res["spins"]
            total_balance += res.get("balance", 0)

            try:
                async with edit_lock:
                    await msg.edit_text(
                        f"✅ <b>Готово!</b>\n\n"
                        f"Аккаунтов: {len(active)}\n"
                        f"Прокруток: {total_spins}\n"
                        f"💎 Баланс: {total_balance}",
                        reply_markup=main_menu(), parse_mode="HTML",
                    )
            except Exception:
                pass

        SPIN_ALL_STOP.pop(uid, None)
        EDIT_LOCKS.pop(uid, None)

    elif data == "spin_all_stop":
        SPIN_ALL_STOP[uid] = True
        await q.answer("Останавливаю...")

    elif data == "daily_checkin":
        accounts = db.get_user_accounts(uid)
        active = [a for a in accounts if a["status"] == "active"]
        if not active:
            await q.edit_message_text(
                "❌ Нет активных аккаунтов\nДобавь: /add",
                reply_markup=back_kb(), parse_mode="HTML",
            )
            return

        await q.answer("Отмечаюсь...")
        msg = await q.edit_message_text(
            "📅 <b>Отмечаюсь за сегодня...</b>\n\n"
            f"<i>Аккаунтов: {len(active)}</i>\n"
            f"Обработано: 0 / {len(active)}",
            parse_mode="HTML",
        )

        async def do_checkin(chat_id, mid):
            ok = 0
            fail = 0
            sem = asyncio.Semaphore(5)
            async def process(acc):
                nonlocal ok, fail
                async with sem:
                    auth = await asyncio.to_thread(edadeal.authenticate, acc["yandex_token"])
                    if not auth["ok"]:
                        fail += 1
                        return
                    result = await asyncio.to_thread(
                        edadeal.claim_500_plus_bonus, auth["jwt"], auth.get("duid"), auth.get("uid")
                    )
                    if result.get("ok"):
                        ok += 1
                    else:
                        fail += 1
            tasks = [process(acc) for acc in active]
            done = 0
            for coro in asyncio.as_completed(tasks):
                await coro
                done += 1
                if done % 5 == 0:
                    try:
                        await context.bot.edit_message_text(
                            f"📅 <b>Отмечаюсь за сегодня...</b>\n\n"
                            f"<i>Аккаунтов: {len(active)}</i>\n"
                            f"Обработано: {done} / {len(active)}",
                            chat_id=chat_id, message_id=mid, parse_mode="HTML",
                        )
                    except Exception:
                        pass
            try:
                await context.bot.edit_message_text(
                    f"📅 <b>Отметка завершена</b>\n\n"
                    f"✅ Отмечено: {ok}\n"
                    f"❌ Ошибок: {fail}",
                    chat_id=chat_id, message_id=mid,
                    reply_markup=main_menu(), parse_mode="HTML",
                )
            except Exception:
                pass

        asyncio.create_task(do_checkin(msg.chat_id, msg.message_id))

    elif data == "noop":
        await q.answer()

    elif data == "refresh_balances":
        accounts = db.get_user_accounts(uid)
        active = [a for a in accounts if a["status"] == "active"]
        if not active:
            await q.answer("Нет активных аккаунтов")
            return

        await q.answer("Обновляю...")
        msg = await q.edit_message_text("🔄 <b>Обновляю балансы...</b>", parse_mode="HTML")

        async def fetch_one(acc):
            auth = await asyncio.to_thread(edadeal.authenticate, acc["yandex_token"])
            if auth["ok"]:
                bal = await asyncio.to_thread(
                    edadeal.get_diamond_balance,
                    auth["jwt"], auth.get("duid"), auth.get("uid"),
                )
                if bal["ok"]:
                    db.update_account_balance(acc["id"], bal["balance"])
                    return acc["id"], bal["balance"]
            return acc["id"], acc["last_balance"] or 0

        results = await asyncio.gather(*[fetch_one(a) for a in active], return_exceptions=True)
        bal_map = {}
        total = 0
        for r in results:
            if not isinstance(r, Exception):
                bal_map[r[0]] = r[1]
                total += r[1]

        accounts = db.get_user_accounts(uid)
        for acc in accounts:
            if acc["id"] in bal_map:
                acc["last_balance"] = bal_map[acc["id"]]

        total_pages = max(1, (len(accounts) + ACCOUNTS_PAGE_SIZE - 1) // ACCOUNTS_PAGE_SIZE)
        ACCOUNTS_CACHE[uid] = {"accounts": list(accounts), "page": 0, "total_pages": total_pages}
        bal, kb = _accounts_text(uid, 0)

        try:
            await msg.edit_text(
                f"📋 <b>Аккаунты</b>  💎 {bal} всего\nВыбери:",
                reply_markup=kb, parse_mode="HTML",
            )
        except Exception:
            pass

    elif data == "accounts_page":
        cache = ACCOUNTS_CACHE.get(uid)
        if not cache:
            await q.answer("Сначала открой список")
            return
        bal, kb = _accounts_text(uid, cache["page"])
        try:
            await q.edit_message_text(
                f"📋 <b>Аккаунты</b>  💎 {bal} всего\nВыбери:",
                reply_markup=kb, parse_mode="HTML",
            )
        except Exception:
            pass

    elif data.startswith("accounts_page:"):
        try:
            page = int(data.split(":")[1])
        except (ValueError, IndexError):
            await q.answer("Ошибка")
            return
        cache = ACCOUNTS_CACHE.get(uid)
        if not cache:
            await q.answer("Сначала открой список")
            return
        bal, kb = _accounts_text(uid, page)
        try:
            await q.edit_message_text(
                f"📋 <b>Аккаунты</b>  💎 {bal} всего\nВыбери:",
                reply_markup=kb, parse_mode="HTML",
            )
        except Exception:
            pass

    elif data == "add_account":
        WAITING_TOKEN[uid] = True
        await q.edit_message_text(
            "🔐 <b>Добавление аккаунта</b>\n\n"
            "1. Перейди по ссылке:\n"
            f"<code>{YANDEX_OAUTH_URL}</code>\n\n"
            "2. Залогинься Яндексом\n"
            "3. Скопируй ссылку из адресной строки\n"
            "4. Отправь её сюда целиком\n\n"
            "⏱ Токен живёт ~1 год",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔓 Открыть Яндекс", url=YANDEX_OAUTH_URL)],
                [InlineKeyboardButton("◀️ Назад", callback_data="menu")],
            ]),
            parse_mode="HTML",
        )

    elif data == "list_accounts":
        accounts = db.get_user_accounts(uid)
        if not accounts:
            await q.edit_message_text(
                "📋 <b>Аккаунтов нет</b>\n\nНажми «➕ Добавить» чтобы начать",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Добавить", callback_data="add_account")],
                    [InlineKeyboardButton("◀️ В меню", callback_data="menu")],
                ]),
                parse_mode="HTML",
            )
            return

        total_pages = max(1, (len(accounts) + ACCOUNTS_PAGE_SIZE - 1) // ACCOUNTS_PAGE_SIZE)
        ACCOUNTS_CACHE[uid] = {"accounts": list(accounts), "page": 0, "total_pages": total_pages}
        bal, kb = _accounts_text(uid, 0)

        await q.edit_message_text(
            f"📋 <b>Аккаунты</b>  💎 {bal} всего\nВыбери:",
            reply_markup=kb, parse_mode="HTML",
        )

    elif data.startswith("account:"):
        acc_id = int(data.split(":")[1])
        acc = db.get_account(acc_id)
        if not acc or acc["user_id"] != uid:
            return await q.edit_message_text("❌ Не найден")

        st = "✅" if acc["status"] == "active" else "⚠️"
        ls = acc["last_spin"]
        if ls:
            try:
                ls = datetime.fromisoformat(ls).strftime("%d.%m %H:%M")
            except Exception:
                pass
        else:
            ls = "нет"

        await q.edit_message_text(
            f"👤 <b>{acc['login']}</b>\n\n"
            f"💎 Баланс: {acc['last_balance'] or 0}\n"
            f"📊 {st} | 🔄 {ls}",
            reply_markup=account_keyboard(acc_id), parse_mode="HTML",
        )

    elif data.startswith("confirm_delete:"):
        acc_id = int(data.split(":")[1])
        acc = db.get_account(acc_id)
        if not acc or acc["user_id"] != uid:
            return
        await q.edit_message_text(
            f"🗑 Удалить <b>{acc['login']}</b>?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Да", callback_data=f"delete:{acc_id}"),
                    InlineKeyboardButton("❌ Нет", callback_data=f"account:{acc_id}"),
                ]
            ]),
            parse_mode="HTML",
        )

    elif data.startswith("delete:"):
        acc_id = int(data.split(":")[1])
        acc = db.get_account(acc_id)
        if not acc or acc["user_id"] != uid:
            return
        db.remove_account(acc_id, uid)
        await q.edit_message_text(
            f"✅ <code>{acc['login']}</code> удалён",
            reply_markup=back_kb(), parse_mode="HTML",
        )

    elif data == "stats":
        s = db.get_user_stats(uid)
        await q.edit_message_text(
            f"📊 <b>Статистика</b>\n\n"
            f"👤 Аккаунтов: {s['total_accounts']}\n"
            f"✅ Активных: {s['active_accounts']}\n"
            f"🎡 Прокруток: {s['total_spins']}\n"
            f"💎 Баланс: {s['total_balance']}",
            reply_markup=back_kb(), parse_mode="HTML",
        )

    elif data == "help":
        await q.edit_message_text(
            "📖 <b>Помощь</b>\n\n"
            "➕ <b>Добавить</b> — добавить Яндекс-аккаунт\n"
            "📋 <b>Аккаунты</b> — список и удаление\n"
            "📊 <b>Статистика</b> — общая статистика\n\n"
            "🔐 <b>Как добавить:</b>\n"
            "1. Нажми «➕ Добавить»\n"
            f"2. Зайди по ссылке: {YANDEX_OAUTH_URL}\n"
            "3. Залогинься Яндексом\n"
            "4. Скопируйте ссылку из адресной строки\n"
            "5. Отправьте её сюда целиком",
            reply_markup=back_kb(), parse_mode="HTML",
        )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _check_ratelimit(uid):
        return
    db.add_user(uid, update.effective_user.username or "", update.effective_user.first_name or "")
    WAITING_TOKEN[uid] = True
    try:
        await update.message.reply_text(
            "🔐 <b>Добавление аккаунта</b>\n\n"
            "1. Перейди по ссылке:\n"
            f"<code>{YANDEX_OAUTH_URL}</code>\n\n"
            "2. Залогинься Яндексом\n"
            "3. Скопируй ссылку  из адресной строки\n"
            "4. Отправь её сюда целиком",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔓 Открыть Яндекс", url=YANDEX_OAUTH_URL)],
                [InlineKeyboardButton("◀️ В меню", callback_data="menu")],
            ]),
            parse_mode="HTML",
        )
    except Exception:
        logging.exception(f"cmd_add error for uid {uid}")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    accounts = db.get_user_accounts(uid)
    if not accounts:
        return await update.message.reply_text("📋 Нет аккаунтов. /add", parse_mode="HTML")

    lines = ["📋 <b>Аккаунты</b>\n"]
    buttons = []
    for acc in accounts:
        s = "✅" if acc["status"] == "active" else "⚠️"
        lines.append(f"{s} <code>{acc['login']}</code>")
        buttons.append([InlineKeyboardButton(f"{s} {acc['login']}", callback_data=f"account:{acc['id']}")])
    buttons.append([InlineKeyboardButton("➕ Добавить", callback_data="add_account")])

    await update.message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML",
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = db.get_user_stats(update.effective_user.id)
    await update.message.reply_text(
        f"📊 Аккаунтов: {s['total_accounts']} | Активных: {s['active_accounts']}\n"
        f"🎡 Прокруток: {s['total_spins']}\n"
        f"💎 Баланс: {s['total_balance']}",
        parse_mode="HTML",
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _check_ratelimit(uid):
        return
    now = time.time()
    if uid in LAST_ACTION and now - LAST_ACTION[uid] < RATE_LIMIT_SEC:
        return
    LAST_ACTION[uid] = now
    text = update.message.text.strip()

    if uid not in WAITING_TOKEN:
        return

    # Extract token from URL or raw token
    token = text
    if "access_token=" in text:
        token = text.split("access_token=")[1].split("&")[0]
    elif "yandex.ru/verification_code#" in text:
        token = text.split("#access_token=")[1].split("&")[0]

    if not token.startswith("y0__"):
        del WAITING_TOKEN[uid]
        return await update.message.reply_text(
            "❌ Не найден Яндекс-токен (должен начинаться с <code>y0__</code>)\n"
            "Нажми /add для инструкции",
            parse_mode="HTML",
        )

    del WAITING_TOKEN[uid]

    await update.message.reply_text("🔍 Проверяю удалось ли войти...", parse_mode="HTML")

    try:
        yandex_check = await asyncio.to_thread(edadeal.check_yandex_token, token)
        if not yandex_check["ok"]:
            return await update.message.reply_text(
                f"❌ Яндекс-токен недействителен\n{yandex_check['error']}\n\nПопробуй: /add",
                parse_mode="HTML",
            )

        login = yandex_check["login"]

        existing = db.get_account_by_login(uid, login)
        if existing:
            db.update_account_status(existing["id"], "active")
            return await update.message.reply_text(
                f"✅ <b>{login}</b> уже добавлен и активирован!",
                reply_markup=back_kb(), parse_mode="HTML",
            )

        auth = await asyncio.to_thread(edadeal.authenticate, token)
        if not auth["ok"]:
            return await update.message.reply_text(
                f"❌ Ошибка авторизации Едадил\n{auth['error']}\n\nПопробуй: /add",
                parse_mode="HTML",
            )

        duid = auth.get("duid", "")
        edadeal_uid = auth.get("uid", "")

        db.add_account(uid, token, login, duid, edadeal_uid)

        bonus_text = ""
        wb = await asyncio.to_thread(edadeal.claim_welcome_bonus, auth["jwt"], duid, edadeal_uid)
        if wb.get("claimed"):
            bonus_text += "\n🎁 Приветственный бонус +500 💎"
        cb = await asyncio.to_thread(edadeal.claim_chain_bonus, auth["jwt"], duid, edadeal_uid)
        if cb.get("claimed"):
            bonus_text += f"\n⛓ Цепочки: +{cb['claimed'] * 50} 💎"
        pb = await asyncio.to_thread(edadeal.claim_plus_bonuses, auth["jwt"], duid, edadeal_uid)
        if pb.get("claimed"):
            if pb["claimed"] == 4:
                bonus_text += "\n➕ Бонусы Плюс: 250 баллов"
            else:
                bonus_text += f"\n➕ Бонусы Плюс: {pb['claimed']} шт"
        elif pb.get("details"):
            texts = [d.get("text", "") for d in pb["details"] if d.get("text")]
            if texts:
                bonus_text += f"\n➕ Бонусы Плюс: {texts[0][:100]}"

        bal = await asyncio.to_thread(edadeal.get_diamond_balance, auth["jwt"], duid, edadeal_uid)
        balance = bal.get("balance", 0) if bal["ok"] else 0
        acc_id = db.get_account_by_login(uid, login)
        if acc_id:
            db.update_account_balance(acc_id["id"], balance)

        await update.message.reply_text(
            f"✅ <b>Аккаунт добавлен!</b>\n\n"
            f"👤 <code>{login}</code>\n"
            f"💎 Баланс: {balance}"
            f"{bonus_text}",
            reply_markup=after_add_kb(), parse_mode="HTML",
        )
    except Exception as e:
        logging.exception(f"message_handler error for uid {uid}")
        await update.message.reply_text(
            f"❌ Внутренняя ошибка: {e}\n\nПопробуй: /add",
            parse_mode="HTML",
        )


def main():
    if not TG_BOT_TOKEN:
        print("Set TG_BOT_TOKEN")
        sys.exit(1)

    db.init_db()
    print("[DB] OK")

    proxy_url = os.environ.get("TG_PROXY", "")
    builder = Application.builder().token(TG_BOT_TOKEN)
    if proxy_url:
        builder = builder.proxy(proxy_url)
    app = builder.build()

    async def post_init(application):
        print("[Bot] Started!")
        asyncio.create_task(sched.daily_checkin_loop())
        asyncio.create_task(sched.tracking_loop())

    app.post_init = post_init

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
