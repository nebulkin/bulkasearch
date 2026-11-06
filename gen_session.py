"""Запусти ОДИН раз локально: python gen_session.py — получишь строку для TG_SESSION."""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API_ID: "))
api_hash = input("API_HASH: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as c:
    print("\nTG_SESSION=" + c.session.save())
