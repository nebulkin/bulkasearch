"""Проверка доступности ников. Бот-токен это не умеет — нужна user-сессия Telethon."""
import asyncio
import logging
import os
import random
import time

import db
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
    "offline": "📡 нет связи с Telegram, попробуй через минуту",
}

LINK_ERRORS = (ConnectionError, OSError, asyncio.TimeoutError)  # связь пропала или запрос завис
REQ_TIMEOUT = 30  # сек: дольше этого запрос к Telegram считаем зависшим
OFFLINE_PAUSE = 45  # сек: после неудачного переподключения аккаунт не трогаем


def make_client(session: str) -> TelegramClient:
    """Telethon по умолчанию сдаётся после 5 попыток с паузой 1 с и потом остаётся отключённым навсегда
    (все запросы дают ConnectionError). Здесь терпеливее: 20 попыток по 3 с. Если и это не помогло,
    связь чинит ensure_connected()."""
    return TelegramClient(StringSession(session), int(os.environ["API_ID"]), os.environ["API_HASH"],
                          connection_retries=20, retry_delay=3, auto_reconnect=True)


async def ensure_connected(client, timeout: float = 45) -> bool:
    """Если клиент отключился (сам Telethon больше не пробует) — подключаем заново. True — связь есть."""
    if client.is_connected():
        return True
    try:
        await asyncio.wait_for(client.connect(), timeout)
    except Exception as e:
        logging.warning("переподключение к Telegram не удалось: %s", type(e).__name__)
        return False
    return client.is_connected()


class Checker:
    def __init__(self):
        self.client = make_client(os.environ["TG_SESSION"])
        self.lock = asyncio.Lock()  # проверяем строго по одному, чтобы не ловить FloodWait
        self.taken: dict[str, float] = {}  # недавно проверенные занятые — повторно не спрашиваем
        self.cooldown_until = 0.0  # после FloodWait молчим, а не долбим дальше
        self.offline_until = 0.0  # после обрыва связи ~45 сек не дёргаем аккаунт
        self.day, self.day_count = time.strftime("%Y%m%d"), 0
        self.day_limit = int(os.getenv("MAX_CHECKS_PER_DAY", 800))
        self.key, self.name, self.last_used = "main", "основной", 0.0

    def is_taken(self, u: str) -> bool:
        return time.time() - self.taken.get(u, 0) < 6 * 3600

    def _mark_taken(self, u: str):
        if len(self.taken) > 20000:
            self.taken.clear()
        self.taken[u] = time.time()

    def cooling(self) -> int:
        """Сколько секунд ещё отдыхаем (0 — можно работать)."""
        return max(0, int(self.cooldown_until - time.time()))

    def ready(self) -> bool:
        """Можно ли прямо сейчас давать этому аккаунту проверки."""
        today = time.strftime("%Y%m%d")
        if today != self.day:
            self.day, self.day_count = today, 0
        return not self.cooling() and self.day_count < self.day_limit and time.time() >= self.offline_until

    async def rest(self, minutes: int):
        """Отправить аккаунт отдыхать вручную (команда /rest). 0 — разбудить."""
        self.cooldown_until = time.time() + minutes * 60 if minutes > 0 else 0.0
        await db.set_cooldown(self.key, self.cooldown_until)

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
        self.cooldown_until = max(self.cooldown_until, await db.get_cooldown(self.key))
        # режим userbot: пишешь `.nick name` в любом чате со своего аккаунта
        self.client.add_event_handler(self._on_nick, events.NewMessage(outgoing=True, pattern=r"^\.nick\s+(\S+)\s*$"))
        self._watchdog_task = asyncio.create_task(self._watchdog())  # держим ссылку, чтобы задачу не собрал GC

    async def _on_nick(self, event):
        u = scoring.normalize(event.pattern_match.group(1))
        if not scoring.is_valid(u):
            return await event.edit(STATUS["invalid"])
        await event.edit(f"⏳ @{u}…")
        st = await self.set_username(u)
        await event.edit(f"@{u}: {STATUS[st]}")

    async def _link(self) -> bool:
        """Есть ли связь с Telegram; если нет — пробуем переподключиться."""
        if await ensure_connected(self.client):
            return True
        self.offline_until = time.time() + OFFLINE_PAUSE
        return False

    async def _heal(self):
        """Один шаг сторожа: если связь пропала и пауза прошла — переподключаемся."""
        if self.client.is_connected() or time.time() < self.offline_until:
            return
        async with self.lock:  # не мешаем идущей проверке
            if await self._link():
                logging.info("основной аккаунт снова на связи")

    async def _watchdog(self):
        """Раз в 30 сек проверяет связь, даже когда никто не ищет."""
        while True:
            await asyncio.sleep(30)
            try:
                await self._heal()
            except Exception:
                logging.exception("watchdog")

    async def set_username(self, u: str) -> str:
        """Ставит ник на аккаунт, к которому привязана сессия."""
        async with self.lock:
            if not await self._link():
                return "offline"
            try:
                await asyncio.wait_for(self.client(functions.account.UpdateUsernameRequest(username=u)), REQ_TIMEOUT)
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
            except LINK_ERRORS:
                self.offline_until = time.time() + OFFLINE_PAUSE
                return "offline"

    async def check(self, u: str, fresh: bool = False) -> str:
        """fresh=True — всегда спросить Telegram; иначе занятые из кеша отдаём сразу (экономим лимиты)."""
        if not fresh and self.is_taken(u):
            return "taken"
        if self.cooling() or not self._budget():
            return "flood"
        async with self.lock:
            if time.time() < self.offline_until or not await self._link():
                self.day_count = max(0, self.day_count - 1)  # запрос в Telegram не ушёл — из лимита не списываем
                return "offline"
            for attempt in (1, 2):
                try:
                    ok = await asyncio.wait_for(
                        self.client(functions.account.CheckUsernameRequest(username=u)), REQ_TIMEOUT)
                    self.last_used = time.time()
                    await asyncio.sleep(random.uniform(1.2, 2.2))  # неровный темп, как у человека
                    if not ok:
                        self._mark_taken(u)
                    return "free" if ok else "taken"
                except errors.UsernameInvalidError:
                    return "invalid"
                except errors.UsernameOccupiedError:
                    self._mark_taken(u)
                    return "taken"
                except errors.FloodWaitError as e:
                    # не ждём и не повторяем: отдыхаем с запасом
                    self.cooldown_until = time.time() + e.seconds + 300
                    await db.set_cooldown(self.key, self.cooldown_until)
                    return "flood"
                except errors.RPCError as e:
                    msg = getattr(e, "message", "") or str(e)
                    if "PURCHASE_AVAILABLE" in msg:
                        return "fragment"
                    if "OCCUPIED" in msg:
                        return "taken"
                    return "unknown"
                except LINK_ERRORS as e:
                    # связь оборвалась: один раз переподключаемся и повторяем запрос
                    logging.warning("check @%s: обрыв связи (%s), попытка %d", u, type(e).__name__, attempt)
                    if attempt == 2 or not await self._link():
                        break
            self.offline_until = time.time() + OFFLINE_PAUSE
            self.day_count = max(0, self.day_count - 1)
            return "offline"
