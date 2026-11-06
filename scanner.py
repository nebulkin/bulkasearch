"""Фоновые сканеры: на отдельных аккаунтах медленно ищут свободные ники и кладут их в пул (таблица pool)."""
import asyncio
import logging
import os
import random
import time

from telethon import TelegramClient, errors, functions
from telethon.sessions import StringSession

import db
import scoring

LENGTHS = [int(x) for x in os.getenv("SCAN_LENGTHS", "5,5,6,6,7").split(",") if x.strip().isdigit()] or [6]
DELAY = float(os.getenv("SCAN_DELAY", 60))  # средняя пауза между проверками одного сканера, сек
DAILY = int(os.getenv("SCAN_DAILY", 600))  # максимум проверок на сканер в сутки
POOL_MAX = int(os.getenv("POOL_MAX", 300))  # столько свободных ников держим «на складе»
POOL_LENGTHS = sorted(set(LENGTHS))
seen: dict[str, float] = {}  # общий для всех сканеров список уже проверенных
SCANNERS: list["Scanner"] = []


class Scanner:
    def __init__(self, name: str, session: str):
        self.name, self.session = name, session
        self.state = "запускается"
        self.checked = self.found = 0
        self.day, self.day_count = time.strftime("%Y%m%d"), 0

    def _candidate(self):
        for _ in range(5):
            n = random.choice(LENGTHS)
            cs = [u for u in scoring.candidates(n, "pron", 30) if u not in seen]
            if cs:
                u = max(cs, key=lambda x: scoring.score(x)[0])
                if len(seen) > 50000:
                    seen.clear()
                seen[u] = time.time()
                return u
        return None

    async def run(self, start_delay: float = 0):
        await asyncio.sleep(start_delay)
        try:
            client = TelegramClient(StringSession(self.session), int(os.environ["API_ID"]), os.environ["API_HASH"])
            await client.connect()
            if not await client.is_user_authorized():
                self.state = "сессия недействительна ❌"
                return logging.error("scanner %s: сессия недействительна", self.name)
        except Exception:
            self.state = "не запустился ❌"
            return logging.exception("scanner %s не запустился", self.name)
        last_clean = 0.0
        while True:
            try:
                today = time.strftime("%Y%m%d")
                if today != self.day:
                    self.day, self.day_count = today, 0
                if self.day_count >= DAILY:
                    self.state = "дневной лимит, отдыхает 💤"
                    await asyncio.sleep(600)
                    continue
                if time.time() - last_clean > 3600:  # старые находки могли уже уйти
                    await db.run("DELETE FROM pool WHERE found < ?", (int(time.time()) - 24 * 3600,))
                    last_clean = time.time()
                if (await db.run("SELECT COUNT(*) FROM pool", fetch=True))[0][0] >= POOL_MAX:
                    self.state = "склад полон 📦"
                    await asyncio.sleep(300)
                    continue
                u = self._candidate()
                if not u:
                    await asyncio.sleep(5)
                    continue
                self.state = "ищет 🔎"
                ok = await client(functions.account.CheckUsernameRequest(username=u))
                self.checked += 1
                self.day_count += 1
                if ok:
                    self.found += 1
                    await db.run(
                        "INSERT INTO pool(username,length,score,found) VALUES(?,?,?,?) "
                        "ON CONFLICT(username) DO UPDATE SET found=excluded.found",
                        (u, len(u), scoring.score(u)[0], int(time.time())))
                await asyncio.sleep(random.uniform(0.7 * DELAY, 1.3 * DELAY))
            except (errors.UsernameInvalidError, errors.UsernameOccupiedError):
                self.checked += 1
                await asyncio.sleep(random.uniform(0.7 * DELAY, 1.3 * DELAY))
            except errors.FloodWaitError as e:
                wait = e.seconds + 300
                self.state = f"FloodWait, спит до {time.strftime('%H:%M', time.gmtime(time.time() + wait))} UTC 🧊"
                logging.warning("scanner %s: FloodWait %s сек", self.name, e.seconds)
                await asyncio.sleep(wait)
            except (errors.AuthKeyUnregisteredError, errors.UserDeactivatedError, errors.UserDeactivatedBanError):
                self.state = "аккаунт отключён ❌"
                return logging.error("scanner %s: аккаунт отключён", self.name)
            except errors.RPCError:
                await asyncio.sleep(120)  # PURCHASE_AVAILABLE и прочее — просто идём дальше
            except Exception:
                logging.exception("scanner %s", self.name)
                await asyncio.sleep(120)


def start_all(main_session: str = ""):
    raw = [s.strip() for s in os.getenv("SCAN_SESSIONS", "").split(",") if s.strip()]
    for i, sess in enumerate(raw, 1):
        if sess and sess == main_session:
            logging.warning("SCAN_SESSIONS: сессия №%d совпадает с основной, пропускаю", i)
            continue
        sc = Scanner(f"сканер {i}", sess)
        SCANNERS.append(sc)
        asyncio.create_task(sc.run(start_delay=(i - 1) * DELAY / 2))
    if not SCANNERS:
        logging.info("SCAN_SESSIONS пуст — фоновый поиск выключен")


def status_lines():
    return [f"• {s.name}: {s.state} · проверено {s.checked}, найдено {s.found}" for s in SCANNERS] or ["сканеров нет"]
