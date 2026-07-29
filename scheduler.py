import asyncio

import database as db
import edadeal


async def spin_account_loop(account, stop_flag, uid, send_message):
    login = account["login"] or "unknown"
    yandex_token = account["yandex_token"]
    spins = 0

    # Auth once, reuse until 401
    auth = None
    jwt = duid = edadeal_uid = None

    async def ensure_auth():
        nonlocal auth, jwt, duid, edadeal_uid
        if auth is None or not auth["ok"]:
            auth = await asyncio.to_thread(edadeal.authenticate, yandex_token)
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

        spin_result = await asyncio.to_thread(edadeal.do_spin, jwt, duid, edadeal_uid)

        if not spin_result["ok"]:
            error = spin_result.get("error", "")
            if "401" in error or "403" in error:
                auth = None  # force re-auth next cycle
                continue
            if "No spin available" in error:
                break
            break

        spins += 1
        prize = spin_result.get("prize_title") or "Без приза"

        bal_result = await asyncio.to_thread(edadeal.get_diamond_balance, jwt, duid, edadeal_uid)
        balance = bal_result["balance"] if bal_result["ok"] else 0
        db.update_account_balance(account["id"], balance)

        try:
            await send_message(f"🎁 <b>{login}</b> — {prize}")
        except Exception:
            pass

    db.update_account_spin(account["id"], 0)
    return {"login": login, "spins": spins}
