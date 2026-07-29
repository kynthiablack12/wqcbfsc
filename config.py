import os

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")

YANDEX_CLIENT_ID = "b80693a227aa4c5393b01721f4ae49f7"

YANDEX_OAUTH_URL = (
    "https://oauth.yandex.ru/authorize"
    f"?response_type=token&client_id={YANDEX_CLIENT_ID}"
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edadil.db")
