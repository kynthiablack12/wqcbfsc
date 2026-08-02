import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import database as db
import edadeal

# Dedicated thread pool for spin operations (won't block user handlers)
_spin_executor = ThreadPoolExecutor(max_workers=3)
# Global semaphore — max 3 accounts spinning concurrently
_spin_semaphore = asyncio.Semaphore(3)

CHECKIN_INTERVAL = 12 * 60 * 60  # 12 hours = 2x/day
TRACKING_INTERVAL = 60  # poll tracking account balance every 1 minute


async def to_thread(func, *args, timeout=25):
    """Run sync func in dedicated executor with a timeout."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_spin_executor, func, *args),
        timeout=timeout,
    )


async def spin_account_loop(account, stop_flag, uid, send_message):
    async with _spin_semaphore:
        return await _spin_one(account, stop_flag, uid, send_message)


async def _spin_one(account, stop_flag, uid, send_message):
    login = account["login"] or "unknown"
    yandex_token = account["yandex_token"]
    spins = 0

    auth = None
    jwt = duid = edadeal_uid = None

    async def ensure_auth():
        nonlocal auth, jwt, duid, edadeal_uid
        if auth is None or not auth["ok"]:
            auth = await to_thread(edadeal.authenticate, yandex_token)
            if auth["ok"]:
                jwt = auth["jwt"]
                duid = auth.get("duid")
                edadeal_uid = auth.get("uid")
        return auth["ok"]

    while not stop_flag.get(uid, False):
        await asyncio.sleep(7)
        if not await ensure_auth():
            db.update_account_status(account["id"], "expired")
            break

        spin_result = await to_thread(edadeal.do_spin, jwt, duid, edadeal_uid)

        if not spin_result["ok"]:
            error = spin_result.get("error", "")
            if "401" in error or "403" in error:
                auth = None
                continue
            if "No spin available" in error:
                break
            break

        spins += 1
        prize = spin_result.get("prize_title") or "Без приза"

        bal_result = await to_thread(edadeal.get_diamond_balance, jwt, duid, edadeal_uid)
        balance = bal_result["balance"] if bal_result["ok"] else 0
        db.update_account_balance(account["id"], balance)

        try:
            await send_message(f"🎁 <b>{login}</b> — {prize}")
        except Exception:
            pass

    db.update_account_spin(account["id"], 0)
    return {"login": login, "spins": spins}


async def _checkin_one(account):
    try:
        auth = await to_thread(edadeal.authenticate, account["yandex_token"])
        if not auth["ok"]:
            db.update_account_status(account["id"], "expired")
            return {"login": account["login"], "ok": False, "error": "auth failed"}
        result = await to_thread(
            edadeal.claim_500_plus_bonus, auth["jwt"], auth.get("duid"), auth.get("uid")
        )
        return {"login": account["login"], "ok": result.get("ok", False), "status": result.get("status")}
    except Exception as e:
        return {"login": account["login"], "ok": False, "error": str(e)}


async def daily_checkin_loop():
    logging.info("[daily_checkin] start")
    sem = asyncio.Semaphore(5)
    while True:
        accounts = db.get_all_active_accounts()
        if accounts:
            logging.info("[daily_checkin] processing %d accounts", len(accounts))
            async def checked(acc):
                async with sem:
                    result = await _checkin_one(acc)
                    log = f"[daily_checkin] {result['login']}: {'OK' if result.get('ok') else 'FAIL'} ({result.get('status') or result.get('error', '?')})"
                    logging.info(log)
            tasks = [checked(acc) for acc in accounts]
            await asyncio.gather(*tasks)
        await asyncio.sleep(CHECKIN_INTERVAL)


def _run_all_triggers_for_account(account):
    """Run the 4 bonus triggers (welcome, chain, plus, 500) for one account."""
    login = account["login"] or "unknown"
    try:
        auth = edadeal.authenticate(account["yandex_token"])
        if not auth["ok"]:
            return {"login": login, "ok": False, "error": "auth failed"}

        jwt = auth["jwt"]
        duid = auth.get("duid")
        uid = auth.get("uid")

        welcome = edadeal.claim_welcome_bonus(jwt, duid, uid)
        chain = edadeal.claim_chain_bonus(jwt, duid, uid)
        plus = edadeal.claim_plus_bonuses(jwt, duid, uid)
        b500 = edadeal.claim_500_plus_bonus(jwt, duid, uid)

        return {
            "login": login,
            "ok": True,
            "welcome": bool(welcome.get("claimed")),
            "chain": int(chain.get("claimed", 0) or 0),
            "plus": int(plus.get("claimed", 0) or 0),
            "b500": bool(b500.get("ok")),
        }
    except Exception as e:
        return {"login": login, "ok": False, "error": str(e)}


async def _run_all_triggers_async():
    accounts = db.get_all_active_accounts()
    if not accounts:
        return {"ok": False, "error": "no accounts"}

    sem = asyncio.Semaphore(5)

    async def one(acc):
        async with sem:
            return await to_thread(_run_all_triggers_for_account, acc, timeout=120)

    results = await asyncio.gather(*[one(a) for a in accounts], return_exceptions=True)

    ok = [r for r in results if isinstance(r, dict) and r.get("ok")]
    fail = [r for r in results if not (isinstance(r, dict) and r.get("ok"))]
    return {
        "ok": True,
        "total": len(accounts),
        "ok_count": len(ok),
        "fail_count": len(fail),
        "welcome": sum(1 for r in ok if r.get("welcome")),
        "chain": sum(r.get("chain", 0) for r in ok),
        "plus": sum(r.get("plus", 0) for r in ok),
        "b500": sum(1 for r in ok if r.get("b500")),
        "details": ok + fail,
    }


async def run_all_triggers():
    """Public wrapper — run the 4 bonus triggers on all active accounts."""
    return await _run_all_triggers_async()


async def tracking_loop():
    """Poll the tracking account's diamond balance every minute.
    When it changes, run the 4 triggers on ALL active accounts and log to the admin dashboard."""
    logging.info("[tracking] start")
    while True:
        try:
            tracker = db.get_tracking_account()
            if tracker is None:
                await asyncio.sleep(TRACKING_INTERVAL)
                continue

            auth = await to_thread(edadeal.authenticate, tracker["yandex_token"], timeout=30)
            if not auth["ok"]:
                db.update_tracking_status(tracker["id"], "expired")
                await asyncio.sleep(TRACKING_INTERVAL)
                continue

            bal = await to_thread(
                edadeal.get_diamond_balance,
                auth["jwt"], auth.get("duid"), auth.get("uid"),
                timeout=30,
            )
            if not bal["ok"]:
                await asyncio.sleep(TRACKING_INTERVAL)
                continue

            new_balance = bal["balance"]
            old_balance = tracker["last_balance"]

            if old_balance is None:
                # First observation — just record it, no trigger yet
                db.update_tracking_balance(tracker["id"], new_balance)
                db.add_tracking_log(
                    f"🟢 Отслеживание запущено. Текущий баланс: {new_balance} 💎"
                )
                _dash_log("tracking", "init", f"balance={new_balance}")
            elif new_balance != old_balance:
                db.update_tracking_balance(tracker["id"], new_balance)
                db.add_tracking_log(
                    f"⚡ Баланс изменился: {old_balance} → {new_balance} 💎. Запускаю триггеры на все аккаунты..."
                )
                _dash_log("tracking", "balance_change", f"{old_balance}->{new_balance}")

                result = await _run_all_triggers_async()
                if result.get("ok"):
                    msg = (
                        f"✅ Триггеры выполнены: {result['ok_count']}/{result['total']} аккаунтов "
                        f"(welcome={result['welcome']}, chain={result['chain']}, plus={result['plus']}, b500={result['b500']})"
                    )
                else:
                    msg = f"❌ Триггеры не выполнены: {result.get('error', '?')}"
                db.add_tracking_log(msg)
                _dash_log("tracking", "triggers_done", msg)
                logging.info("[tracking] %s", msg)
            else:
                # Balance unchanged — nothing to do
                db.update_tracking_balance(tracker["id"], new_balance)

        except Exception as e:
            logging.exception("[tracking] error: %s", e)
            _dash_log("tracking", "error", str(e))

        await asyncio.sleep(TRACKING_INTERVAL)


def _dash_log(user_id, action, detail):
    """Safe proxy to the admin dashboard log (avoids hard import of web.py)."""
    try:
        from web import add_log
        add_log(user_id, action, detail)
    except Exception:
        pass
