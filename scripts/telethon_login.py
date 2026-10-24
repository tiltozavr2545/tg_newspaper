"""
One-off interactive Telethon login under the project's account.

Run: uv run python scripts/telethon_login.py
Will ask for the phone number, the confirmation code from Telegram, and (if
enabled) the two-factor password. On successful login creates a session file
(TG_SESSION_NAME from .env, e.g. tg_newspaper.session) — this is a secret,
keep it as is, don't commit it (already in .gitignore).
"""

import os

from dotenv import load_dotenv
from telethon.sync import TelegramClient

from tg_newspaper.config import env_path, session_base

# The same login can be done in the browser — the setup wizard in the console
# (scripts/console.py); this script remains a terminal alternative.
load_dotenv(env_path())

api_id = os.environ["TG_API_ID"]
api_hash = os.environ["TG_API_HASH"]
session_name = os.environ.get("TG_SESSION_NAME", "tg_newspaper")
session_path = session_base(session_name)

with TelegramClient(session_path, int(api_id), api_hash) as client:
    me = client.get_me()
    print(f"Успешный логин: {me.first_name} (@{me.username}, id={me.id})")
    print(f"Файл сессии: {session_name}.session")
