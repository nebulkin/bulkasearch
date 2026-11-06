"""Проверка доступности ников. Бот-токен это не умеет — нужна user-сессия Telethon."""
import asyncio
import os
import random
import time

import scoring
from telethon import TelegramClient, errors, events, functions
from telethon.sessions import StringSession

STATUS = {
    "free": "✅ свободен",
    "fragment": "💎 продаётся на Fragment",
    "taken": "❌ занят",
    "invalid": "⚠️ недопустимый ник",
    "unknown": "❔ не удалось проверить",
    "flood": "🧊 пауза: Telegram просит притормозить",
    "set": "✅ ник установлен на аккаунт",
    "same": "ℹ️ это уже твой ник",
    "fail": "⚠️ не удалось поставить ник",
}


class Checker:
    def __init__(self):
        self.client = TelegramClient(
            StringSession(os.environ["TG_SESSION"]), int(os.environ["API_ID"]), os.environ["API_HASH"]
        )
        self.lock = asyncio.Lock()  # проверяем строго по одному, чтобы не ловить FloodWait
        self.cooldown_until = 0.0  # после FloodWait молчим, а не долбим дальше
        self.day, self.day_count = time.strftime("%Y%m%d"), 0
        self.day_limit = int(os.getenv("MAX_CHECKS_PER_DAY", 800))

    def cooling(self) -> int:
        """Сколько секунд ещё отдыхаем (0 — можно работать)."""
        return max(0, int(self.cooldown_until - time.time()))

    def _budget(self) -> bool:
        today = time.strftime("%Y%m%d")
        if today != self.day:
            self.day, self.day_count = today, 0
        self.day_count += 1
        return self.day_count <= self.day_limit

    async def start(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("TG_SESSION недействителен — создай новый через gen_session.py")
        # режим userbot: пишешь `.nick name` в любом чате со своего аккаунта
        self.client.add_event_handler(self._on_nick, events.NewMessage(outgoing=True, pattern=r"^\.nick\s+(\S+)\s*$"))

    async def _on_nick(self, event):
        u = scoring.normalize(event.pattern_match.group(1))
        if not scoring.is_valid(u):
            return await event.edit(STATUS["invalid"])
        await event.edit(f"⏳ @{u}…")
        st = await self.set_username(u)
        await event.edit(f"@{u}: {STATUS[st]}")

    async def set_username(self, u: str) -> str:
        """Ставит ник на аккаунт, к которому привязана сессия."""
        async with self.lock:
            try:
                await self.client(functions.account.UpdateUsernameRequest(username=u))
                return "set"
            except errors.UsernameNotModifiedError:
                return "same"
            except errors.UsernameInvalidError:
                return "invalid"
            except errors.UsernameOccupiedError:
                return "taken"
            except errors.RPCError as e:
                msg = getattr(e, "message", "") or str(e)
                return "fragment" if "PURCHASE_AVAILABLE" in msg else "fail"

    async def check(self, u: str) -> str:
        if self.cooling() or not self._budget():
            return "flood"
        async with self.lock:
            try:
                ok = await self.client(functions.account.CheckUsernameRequest(username=u))
                await asyncio.sleep(random.uniform(1.2, 2.2))  # неровный темп, как у человека
                return "free" if ok else "taken"
            except errors.UsernameInvalidError:
                return "invalid"
            except errors.UsernameOccupiedError:
                return "taken"
            except errors.FloodWaitError as e:
                # не ждём и не повторяем: отдыхаем с запасом
                self.cooldown_until = time.time() + e.seconds + 300
                return "flood"
            except errors.RPCError as e:
                msg = getattr(e, "message", "") or str(e)
                if "PURCHASE_AVAILABLE" in msg:
                    return "fragment"
                if "OCCUPIED" in msg:
                    return "taken"
                return "unknown"
