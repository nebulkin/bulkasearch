"""Проверка доступности ников. Бот-токен это не умеет — нужна user-сессия Telethon."""
import asyncio
import os

import scoring
from telethon import TelegramClient, errors, events, functions
from telethon.sessions import StringSession

STATUS = {
    "free": "✅ свободен",
    "fragment": "💎 продаётся на Fragment",
    "taken": "❌ занят",
    "invalid": "⚠️ недопустимый ник",
    "unknown": "❔ не удалось проверить",
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
        async with self.lock:
            for _ in range(3):
                try:
                    ok = await self.client(functions.account.CheckUsernameRequest(username=u))
                    await asyncio.sleep(0.7)
                    return "free" if ok else "taken"
                except errors.UsernameInvalidError:
                    return "invalid"
                except errors.UsernameOccupiedError:
                    return "taken"
                except errors.FloodWaitError as e:
                    if e.seconds > 120:
                        return "unknown"
                    await asyncio.sleep(e.seconds + 1)
                except errors.RPCError as e:
                    msg = getattr(e, "message", "") or str(e)
                    if "PURCHASE_AVAILABLE" in msg:
                        return "fragment"
                    if "OCCUPIED" in msg:
                        return "taken"
                    return "unknown"
            return "unknown"
