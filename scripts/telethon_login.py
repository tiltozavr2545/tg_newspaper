"""
Одноразовый интерактивный логин Telethon под аккаунтом проекта.

Запуск: uv run python scripts/telethon_login.py
Спросит номер телефона, код подтверждения из Telegram и (если включена)
пароль двухфакторки. После успешного входа создаст файл сессии
(TG_SESSION_NAME из .env, например tg_newspaper.session) — это секрет,
хранить как есть, не коммитить (уже в .gitignore).
"""

import os

from dotenv import load_dotenv
from telethon.sync import TelegramClient

load_dotenv()

api_id = os.environ["TG_API_ID"]
api_hash = os.environ["TG_API_HASH"]
session_name = os.environ.get("TG_SESSION_NAME", "tg_newspaper")

with TelegramClient(session_name, int(api_id), api_hash) as client:
    me = client.get_me()
    print(f"Успешный логин: {me.first_name} (@{me.username}, id={me.id})")
    print(f"Файл сессии: {session_name}.session")
