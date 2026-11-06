"""Фоновые сканеры: на отдельных аккаунтах медленно ищут свободные ники и кладут их в пул (таблица pool)."""
import asyncio
import logging
import os
import random
import re
import time

from telethon import TelegramClient, errors, functions
from telethon.sessions import StringSession

import db
import scoring

LENGTHS = [int(x) for x in os.getenv("SCAN_LENGTHS", "5,5,5,5,6,6,7").split(",") if x.strip().isdigit()] or [6]
DELAY = float(os.getenv("SCAN_DELAY", 60))  # средняя пауза между проверками одного сканера, сек
DAILY = int(os.getenv("SCAN_DAILY", 1500))  # максимум проверок на сканер в сутки
POOL_MAX = int(os.getenv("POOL_MAX", 300))  # столько свободных ников держим «на складе»
POOL_LENGTHS = sorted(set(LENGTHS))
MIN_SCORE = int(os.getenv("POOL_MIN_SCORE", 54))  # слабее этого сканеры даже не проверяют (54 = только слова и бренды)
seen: dict[str, float] = {}  # общий для всех сканеров список уже проверенных

# Оповещения: находка этой длины и от этой оценки сразу уходит владельцу (в Render можно поменять ALERT_LENGTHS / ALERT_MIN_SCORE)
ALERT_MIN = int(os.getenv("ALERT_MIN_SCORE", 50))
ALERT_LENGTHS = {int(x) for x in os.getenv("ALERT_LENGTHS", "5,6").split(",") if x.strip().isdigit()}
_alert = None  # функция оповещения, её передаёт bot.py
_alert_tasks: set = set()


def set_alert(fn):
    global _alert
    _alert = fn


def alert_line() -> str:
    if not _alert:
        return "🚨 Оповещения: выключены (не задан OWNER_ID)"
    return f"🚨 Оповещения: {', '.join(map(str, sorted(ALERT_LENGTHS)))} симв., от {ALERT_MIN} баллов"

NEAR_MIN = int(os.getenv("NEAR_MIN_SCORE", 35))  # минимальная оценка «почти-слов»
_V, _C = "aeiou", "bdfgklmnprstv"
_BY_LEN: dict[int, list[str]] = {}
_VPAIRS = {"ea", "ou", "ai", "oo", "ee", "ie", "io", "ia", "ay", "oa", "ui", "ey", "oy", "eu"}
_CPAIRS = set("bl br ch cl cr dr fl fr gl gr ll nd ng nk nt pl pr rs sh sk sl sm sn sp st sw th tr ss tt rn rt rd rk rm "
              "lm ld lk lt mp mb nn pp rr ck ft ct tc lv rv".split())


def _pretty(u: str) -> bool:
    """Похоже на английское слово: нет трёх согласных/гласных подряд и странных пар."""
    if re.search(r"[^aeiouy]{3}|[aeiouy]{3}", u):
        return False
    if any(m.group() not in _VPAIRS for m in re.finditer(r"[aeiouy]{2}", u)):
        return False
    return all(m.group() in _CPAIRS for m in re.finditer(r"[^aeiouy]{2}", u))


def _near_words(n: int, count: int = 30) -> list[str]:
    """Настоящее слово из словаря с одной буквой заменённой/убранной/добавленной: lumen -> lumon, brava -> brave."""
    if not _BY_LEN:
        for w in scoring.WORDS | scoring.BRANDS:
            _BY_LEN.setdefault(len(w), []).append(w)
    src = _BY_LEN.get(n - 1, []) + _BY_LEN.get(n, []) + _BY_LEN.get(n + 1, [])
    if not src:
        return []
    out = set()
    for _ in range(count * 5):
        w = random.choice(src)
        i = random.randrange(len(w))
        op = random.choice(("sub", "drop", "add"))
        if op == "sub":
            u = w[:i] + random.choice(_V if w[i] in "aeiouy" else _C) + w[i + 1:]
        elif op == "drop":
            u = w[:i] + w[i + 1:]
        else:
            u = w[:i] + random.choice(_V + _C) + w[i:]
        if len(u) == n and u not in scoring.WORDS and u not in scoring.BRANDS and scoring.is_valid(u) and _pretty(u):
            out.add(u)
            if len(out) >= count:
                break
    return list(out)

SCANNERS: list["Scanner"] = []


class Scanner:
    def __init__(self, name: str, session: str, key: str):
        self.name, self.session, self.key = name, session, key
        self.client = None  # появляется после успешного подключения
        self.lock = asyncio.Lock()  # один запрос за раз на аккаунт (фон + живой поиск)
        self.cooldown_until = 0.0
        self.last_used = 0.0
        self.state = "запускается"
        self.checked = self.found = 0
        self.day, self.day_count = time.strftime("%Y%m%d"), 0

    def cooling(self) -> int:
        return max(0, int(self.cooldown_until - time.time()))

    def ready(self) -> bool:
        """Можно ли прямо сейчас дать этому сканеру живую проверку для пользователя."""
        today = time.strftime("%Y%m%d")
        if today != self.day:
            self.day, self.day_count = today, 0
        return self.client is not None and not self.cooling() and self.day_count < DAILY

    async def _flood(self, seconds: int):
        self.cooldown_until = time.time() + seconds + 300
        await db.set_cooldown(self.key, self.cooldown_until)

    async def check(self, u: str, fresh: bool = True) -> str:
        """Живая проверка ника для поиска пользователя (тот же формат ответа, что у Checker.check)."""
        if not self.ready():
            return "flood"
        async with self.lock:
            try:
                ok = await self.client(functions.account.CheckUsernameRequest(username=u))
                self.day_count += 1
                self.last_used = time.time()
                await asyncio.sleep(random.uniform(1.2, 2.2))
                return "free" if ok else "taken"
            except errors.UsernameInvalidError:
                return "invalid"
            except errors.UsernameOccupiedError:
                return "taken"
            except errors.FloodWaitError as e:
                logging.warning("scanner %s: FloodWait %s сек (живой поиск)", self.name, e.seconds)
                await self._flood(e.seconds)
                return "flood"
            except errors.RPCError as e:
                msg = getattr(e, "message", "") or str(e)
                if "PURCHASE_AVAILABLE" in msg:
                    return "fragment"
                if "OCCUPIED" in msg:
                    return "taken"
                return "unknown"

    def _candidate(self):
        # слова и бренды целиком — самые ценные; «почти-слова» (слово с одной изменённой буквой) — запасной
        # бесконечный источник: выглядят как настоящие слова, а не случайная каша
        for _ in range(10):
            n = random.choice(LENGTHS)
            mode = random.choices(("word", "brand", "near"), weights=(5, 2, 3))[0]
            if mode == "near":
                cs = [u for u in _near_words(n) if u not in seen and scoring.score(u)[0] >= NEAR_MIN]
            else:
                cs = [u for u in scoring.candidates(n, mode, 30) if u not in seen and scoring.score(u)[0] >= MIN_SCORE]
                if not cs:  # слова нужной длины кончились — берём «почти-слова»
                    cs = [u for u in _near_words(n) if u not in seen and scoring.score(u)[0] >= NEAR_MIN]
            if cs:
                top = max(scoring.score(x)[0] for x in cs)
                u = random.choice([x for x in cs if scoring.score(x)[0] == top])
                if len(seen) > 50000:
                    seen.clear()
                seen[u] = time.time()
                return u
        return None

    async def run(self, start_delay: float = 0):
        await asyncio.sleep(start_delay)
        try:
            self.state = "подключается…"
            client = TelegramClient(StringSession(self.session), int(os.environ["API_ID"]), os.environ["API_HASH"])
            await asyncio.wait_for(client.connect(), 90)
            if not await asyncio.wait_for(client.is_user_authorized(), 90):
                self.state = "сессия недействительна ❌"
                return logging.error("scanner %s: сессия недействительна", self.name)
            self.client = client
            self.cooldown_until = await db.get_cooldown(self.key)
            self.state = "подключён, подбирает ники…"
            logging.info("scanner %s: подключён", self.name)
        except Exception as e:
            self.state = f"не запустился ❌ ({type(e).__name__})"
            return logging.exception("scanner %s не запустился", self.name)
        last_clean = 0.0
        while True:
            try:
                left = self.cooling()
                if left:
                    self.state = f"FloodWait, спит до {time.strftime('%H:%M', time.gmtime(self.cooldown_until))} UTC 🧊"
                    await asyncio.sleep(min(left, 300))
                    continue
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
                    self.state = "нет подходящих ников (проверь words.txt и POOL_MIN_SCORE)"
                    await asyncio.sleep(5)
                    continue
                self.state = "ищет 🔎"
                async with self.lock:
                    ok = await client(functions.account.CheckUsernameRequest(username=u))
                self.last_used = time.time()
                self.checked += 1
                self.day_count += 1
                if ok:
                    self.found += 1
                    sc = scoring.score(u)[0]
                    if _alert and len(u) in ALERT_LENGTHS and sc >= ALERT_MIN:
                        # ценная находка: сразу шлём владельцу и НЕ кладём на склад,
                        # чтобы её не выдали случайному пользователю при обычном поиске
                        t = asyncio.create_task(_alert(u, sc, self.name))
                        _alert_tasks.add(t)
                        t.add_done_callback(_alert_tasks.discard)
                    else:
                        await db.run(
                            "INSERT INTO pool(username,length,score,found) VALUES(?,?,?,?) "
                            "ON CONFLICT(username) DO UPDATE SET found=excluded.found",
                            (u, len(u), sc, int(time.time())))
                await asyncio.sleep(random.uniform(0.85 * DELAY, 1.15 * DELAY))
            except (errors.UsernameInvalidError, errors.UsernameOccupiedError):
                self.checked += 1
                await asyncio.sleep(random.uniform(0.85 * DELAY, 1.15 * DELAY))
            except errors.FloodWaitError as e:
                logging.warning("scanner %s: FloodWait %s сек", self.name, e.seconds)
                await self._flood(e.seconds)
            except (errors.AuthKeyUnregisteredError, errors.UserDeactivatedError, errors.UserDeactivatedBanError):
                self.state = "аккаунт отключён ❌"
                self.client = None
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
        sc = Scanner(f"сканер {i}", sess, f"scan{i}")
        SCANNERS.append(sc)
        asyncio.create_task(sc.run(start_delay=(i - 1) * DELAY / 2))
    logging.info("scanner: слов в словаре %d, брендов %d, мин. оценка %d, длины %s",
                 len(scoring.WORDS), len(scoring.BRANDS), MIN_SCORE, LENGTHS)
    if not SCANNERS:
        logging.info("SCAN_SESSIONS пуст — фоновый поиск выключен")


def status_lines():
    return [f"• {s.name}: {s.state} · проверено {s.checked}, найдено {s.found}" for s in SCANNERS] or ["сканеров нет"]


def live_checkers():
    """Сканеры, которые подключены и могут помогать с живым поиском."""
    return [s for s in SCANNERS if s.client is not None]
