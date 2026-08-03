import json
import os
import re
import uuid
import base64
import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TMO = 8  # global timeout for all Edadeal requests

EDADEAL_PROXY = os.environ.get("EDADEAL_PROXY", "")
_http = None


def _session():
    global _http
    if _http is None:
        s = requests.Session()
        s.verify = False
        if EDADEAL_PROXY:
            s.proxies = {"http": EDADEAL_PROXY, "https": EDADEAL_PROXY}
        s.headers.update({
            "User-Agent": "okhttp/4.11.0 Edadeal/26.28.0",
            "Accept": "application/json",
            "Accept-Language": "ru_RU",
            "Content-Type": "application/json",
            "x-platform": "android",
            "x-os-version": "12.0.0",
            "x-app-version": "26.28.0",
            "x-app-id": "edadeal",
            "x-locality-geoid": "66",
            "x-locality-countrygeoid": "225",
            "x-real-locality-geoid": "66",
            "x-real-locality-countrygeoid": "225",
            "x-position-latitude": "54.98934200",
            "x-position-longitude": "73.36821200",
            "x-device-timezone": "Asia/Omsk",
            "x-device-manufacturer": "SAMSUNG",
            "x-device-model": "SM-F711B",
            "x-device-ram-class": "3",
            "amversion": "7.54.1",
        })
        _http = s
    return _http


DEVICE_URL = "https://api.edadeal.ru/api/usr/auth/v1/device"
AUTH_URL = "https://api.edadeal.ru/api/usr/auth/v1/auth"
SPIN_URL = (
    "https://api.edadeal.ru/api/mosaic/api/v1/blocks"
    "?format=patch&patchid=spinarea&position=0"
    "&block_id=2586/0&patch_actions_only=true"
    '&macros=%7B%22useFreespin%22%3A%22false%22%7D'
    "&supports_phoenix=1"
)
SPIN_URL_FREESPIN = (
    "https://api.edadeal.ru/api/mosaic/api/v1/blocks"
    "?format=patch&patchid=spinarea&position=0"
    "&block_id=2586/0&patch_actions_only=true"
    '&macros=%7B%22useFreespin%22%3A%22true%22%7D'
    "&supports_phoenix=1"
)
DIAMOND_BALANCE_URL = "https://api.edadeal.ru/api/mangekyo/api/v1/almazilo/total"
YANDEX_INFO_URL = "https://login.yandex.ru/info"
BONUS_BLOCK_URL = "https://api.edadeal.ru/api/mosaic/api/v1/blocks?id=18467%2F2&supports_phoenix=1"
HEADER_BLOCK_URL = "https://api.edadeal.ru/api/mosaic/api/v1/blocks?id=19773%2F0&supports_phoenix=1"
TRIGGER_BASE = "https://trigger-proxy.edadeal.ru/triggers"
WELCOME_TRIGGER_ID = "7964dad0-5589-4c5f-8594-aa227deba4b8"
CHAIN_TRIGGER_ID = "41d7366f-83b1-4fa1-9a51-47ae69b99fae"
PLUS_TRIGGER_ID = "22ddec2c-8662-434a-a205-64038cc75fc3"
PLUS_BLOCK_URL = "https://api.edadeal.ru/api/mosaic/api/v1/blocks?id=19741%2F19&supports_phoenix=1&experiment_id=x5reward"
PLUS_POINTS_BLOCK_URL = "https://api.edadeal.ru/api/mosaic/api/v1/blocks?id=19628%2F11&supports_phoenix=1"
BONUS_500_TRIGGER_URL = f"{TRIGGER_BASE}/{WELCOME_TRIGGER_ID}?krokenUuid=51d222d1-e01a-4b81-a765-c51977a60be2"

PLUS_AWARD_URLS = [
    f"{TRIGGER_BASE}/{PLUS_TRIGGER_ID}?awardUuid=13b47f71-16c2-4e28-a7bd-69502c77a29d",
    f"{TRIGGER_BASE}/{PLUS_TRIGGER_ID}?awardUuid=8d6d68c6-5607-4ce6-a465-6974c4610541",
    f"{TRIGGER_BASE}/{PLUS_TRIGGER_ID}?awardUuid=66301f74-6d60-4463-9bbe-a89b96346720",
    f"{TRIGGER_BASE}/{PLUS_TRIGGER_ID}?awardUuid=260b22f2-0192-4690-8fb2-39607859155a",
]


def decode_jwt(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def extract_balance_from_actions(actions):
    for action in actions:
        url = action.get("url", "")
        if "pubSubPublish" in url:
            try:
                parts = url.split("pubSubPublish?")
                if len(parts) == 2:
                    b64 = parts[1]
                    b64 += "=" * (4 - len(b64) % 4)
                    data = json.loads(base64.urlsafe_b64decode(b64))
                    return data.get("message", {}).get("data", {}).get("balance")
            except Exception:
                pass
        typed = action.get("typed", {})
        if typed.get("variable_name") == "money":
            return typed.get("value", {}).get("value")
    return None


def _h(jwt=None, duid=None, uid=None):
    h = {}
    if jwt:
        h["Authorization"] = jwt
    if duid:
        h["edadeal-duid"] = duid
    if uid:
        h["edadeal-uid"] = uid
    return h


def check_yandex_token(yandex_token):
    try:
        resp = _session().get(YANDEX_INFO_URL, headers={"Authorization": f"OAuth {yandex_token}"}, timeout=TMO)
        if resp.status_code == 200:
            data = resp.json()
            return {"ok": True, "login": data.get("login", ""), "name": data.get("display_name", data.get("real_name", ""))}
        return {"ok": False, "error": f"Yandex HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def authenticate(yandex_token):
    ts = str(int(time.time()))
    device_id = str(uuid.uuid4())
    uuid_val = str(uuid.uuid4())

    try:
        http = _session()
        r1 = http.post(DEVICE_URL, json={"platform": "android", "device_id": device_id, "uuid": uuid_val}, headers={"x-device-init-timestamp": ts}, timeout=TMO)
        if r1.status_code != 200:
            return {"ok": False, "error": f"Device registration failed: HTTP {r1.status_code}"}

        anon_jwt = r1.headers.get("authorization", "")
        anon_duid = r1.headers.get("edadeal-duid", "")
        if not anon_jwt:
            return {"ok": False, "error": "No anonymous JWT returned"}

        body = {"duid": anon_duid, "provider": "am", "token": yandex_token}
        r2 = http.post(AUTH_URL, json=body, headers=_h(anon_jwt, anon_duid), timeout=TMO)
        if r2.status_code != 200:
            error = r2.headers.get("Www-Authenticate", r2.text[:200])
            return {"ok": False, "error": f"Auth failed: HTTP {r2.status_code}: {error}"}

        jwt = r2.headers.get("authorization", "")
        edadeal_uid = r2.headers.get("edadeal-uid", "")
        edadeal_duid = r2.headers.get("edadeal-duid", anon_duid)

        if not jwt:
            return {"ok": False, "error": "No JWT in auth response"}

        login = ""
        jwt_data = decode_jwt(jwt)
        if jwt_data:
            login = jwt_data.get("sub", edadeal_uid)

        yandex_info = check_yandex_token(yandex_token)
        if yandex_info["ok"]:
            login = yandex_info["login"]

        return {"ok": True, "jwt": jwt, "duid": edadeal_duid, "uid": edadeal_uid, "login": login}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_spin(jwt, duid=None, edadeal_uid=None):
    if not jwt:
        return {"ok": False, "error": "No JWT"}
    try:
        http = _session()
        for spin_url in [SPIN_URL, SPIN_URL_FREESPIN]:
            resp = http.get(spin_url, headers=_h(jwt, duid, edadeal_uid), timeout=TMO)
            if resp.status_code == 204:
                continue
            if resp.status_code != 200:
                return {"ok": False, "error": f"HTTP {resp.status_code}"}

            data = resp.json()
            actions = data.get("patch", {}).get("on_applied_actions", [])
            if not actions:
                continue

            result = {"prize_title": None, "prize_img": None, "prize_url": None, "balance": None}
            result["balance"] = extract_balance_from_actions(actions)
            for action in actions:
                typed = action.get("typed", {})
                var = typed.get("variable_name", "")
                val = typed.get("value", {}).get("value")
                if var == "prize_title":
                    result["prize_title"] = val
                elif var == "prize_img":
                    result["prize_img"] = val
                elif var == "prize_url":
                    result["prize_url"] = val
            return {"ok": True, **result}

        return {"ok": False, "error": "No spin available (204)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_diamond_balance(jwt, duid=None, edadeal_uid=None):
    if not jwt:
        return {"ok": False, "error": "No JWT"}
    try:
        resp = _session().get(DIAMOND_BALANCE_URL, headers=_h(jwt, duid, edadeal_uid), timeout=TMO)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}"}
        data = resp.json()
        return {"ok": True, "balance": data.get("balance", 0), "possible": data.get("possibleActivationAmount", 0)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_plus_points_counts(jwt, duid=None, edadeal_uid=None):
    """Fetch the 'Plus points for diamonds' block and extract per-card 'шт' badge counts."""
    if not jwt:
        return {"ok": False, "error": "No JWT"}
    try:
        r = _session().get(PLUS_POINTS_BLOCK_URL, headers=_h(jwt, duid, edadeal_uid), timeout=TMO)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        data = r.json()
        cards = []
        try:
            cards = data["blocks"][0]["card"]["states"][0]["div"]["items"][0]["items"][1]["items"]
        except (KeyError, IndexError, TypeError):
            return {"ok": True, "cards": [], "total": 0}

        parsed = []
        for card in cards:
            info = {"count": 0, "points": 0, "slug": ""}
            def walk(o):
                if isinstance(o, dict):
                    t = o.get("text")
                    if isinstance(t, str) and "toInteger" in t and "+ '" in t:
                        m = re.search(r"toInteger\('(\d+)'\)", t)
                        if m:
                            info["count"] = int(m.group(1))
                    elif isinstance(t, str) and re.fullmatch(r"\d[\d ]*", t) and info["points"] == 0:
                        info["points"] = int(t.replace(" ", ""))
                    p = o.get("payload")
                    if isinstance(p, dict) and p.get("slug"):
                        info["slug"] = p["slug"]
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(card)
            if info["points"] in (25, 50, 100, 250):
                parsed.append(info)

        parsed.sort(key=lambda x: x["points"])
        return {"ok": True, "cards": parsed, "total": sum(c["count"] for c in parsed)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _extract_trigger_urls(text, trigger_id):
    pattern = rf'https://trigger-proxy\.edadeal\.ru/triggers/{re.escape(trigger_id)}\?[^\"]+'
    urls = re.findall(pattern, text)
    clean = []
    for u in urls:
        u = u.split("'")[0].split('"')[0].strip()
        if u not in clean:
            clean.append(u)
    return clean


def claim_welcome_bonus(jwt, duid=None, edadeal_uid=None):
    if not jwt:
        return {"ok": False, "error": "No JWT"}
    try:
        http = _session()
        h = _h(jwt, duid, edadeal_uid)
        r = http.get(BONUS_BLOCK_URL, headers=h, timeout=TMO)
        if r.status_code != 200:
            return {"ok": False, "error": f"Block request failed: HTTP {r.status_code}"}
        urls = _extract_trigger_urls(r.text, WELCOME_TRIGGER_ID)
        if not urls:
            return {"ok": False, "error": "No trigger URL (bonus already claimed or unavailable)"}
        r2 = http.get(urls[0], headers=h, timeout=TMO, allow_redirects=False)
        if r2.status_code == 302:
            return {"ok": True, "claimed": True}
        return {"ok": False, "error": f"Trigger returned HTTP {r2.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def claim_chain_bonus(jwt, duid=None, edadeal_uid=None):
    if not jwt:
        return {"ok": False, "error": "No JWT"}
    try:
        http = _session()
        h = _h(jwt, duid, edadeal_uid)
        r = http.get(HEADER_BLOCK_URL, headers=h, timeout=TMO)
        if r.status_code != 200:
            return {"ok": False, "error": f"Block request failed: HTTP {r.status_code}"}
        urls = _extract_trigger_urls(r.text, CHAIN_TRIGGER_ID)
        if not urls:
            return {"ok": False, "error": "No chain trigger URL found"}
        claimed = 0
        for url in urls:
            r2 = http.get(url, headers=h, timeout=TMO, allow_redirects=False)
            if r2.status_code == 302:
                claimed += 1
        return {"ok": True, "claimed": claimed}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def claim_plus_bonuses(jwt, duid=None, edadeal_uid=None):
    if not jwt:
        return {"ok": False, "error": "No JWT"}
    h = _h(jwt, duid, edadeal_uid)
    http = _session()
    titles = []
    try:
        r = http.get(PLUS_BLOCK_URL, headers=h, timeout=TMO)
        if r.status_code == 200:
            data = r.json()
            for action in data.get("patch", {}).get("on_applied_actions", []):
                typed = action.get("typed", {})
                if typed.get("variable_name") == "title":
                    t = typed.get("value", {}).get("value")
                    if t:
                        titles.append(t)
    except Exception:
        pass

    results = []
    for url in PLUS_AWARD_URLS:
        try:
            r2 = http.get(url, headers=h, timeout=TMO, allow_redirects=False)
            results.append({"claimed": r2.status_code == 302, "title": None, "text": r2.text[:500] if r2.status_code != 302 and r2.text else None})
        except Exception as e:
            results.append({"claimed": False, "title": None, "text": str(e)})

    total_claimed = sum(1 for r in results if r["claimed"])
    return {"ok": True, "claimed": total_claimed, "titles": titles, "details": results}


def claim_500_plus_bonus(jwt, duid=None, edadeal_uid=None):
    if not jwt:
        return {"ok": False, "error": "No JWT"}
    try:
        http = _session()
        h = _h(jwt, duid, edadeal_uid)
        r = http.get(BONUS_500_TRIGGER_URL, headers=h, timeout=TMO, allow_redirects=False)
        return {"ok": r.status_code == 302, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}
