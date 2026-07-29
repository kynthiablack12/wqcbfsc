import asyncio
from concurrent.futures import ThreadPoolExecutor

import database as db
import edadeal

# Dedicated thread pool for spin operations (won't block user handlers)
_spin_executor = ThreadPoolExecutor(max_workers=3)
# Global semaphore — max 3 accounts spinning concurrently
_spin_semaphore = asyncio.Semaphore(3)


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
