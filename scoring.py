"""Оценка, рейтинг и генерация юзернеймов. Без сети — легко тестировать."""
import random
import re
from pathlib import Path

BASE = Path(__file__).parent


def _load(name):
    p = BASE / name
    if not p.exists():
        return set()
    return {w.lower() for w in re.split(r"[\s,]+", p.read_text("utf-8")) if len(w) >= 3}


BRANDS, WORDS = _load("brands.txt"), _load("words.txt")
VOW = set("aeiouy")
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{3,31}$")  # 4–32 символа


def normalize(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"^(https?://)?(t\.me/|telegram\.me/)", "", s)
    return s.lstrip("@")


def is_valid(u: str) -> bool:
    return bool(USERNAME_RE.match(u)) and "__" not in u and not u.endswith("_")


def score(u: str):
    """-> (0..100, [(причина, баллы)])"""
    parts = []
    n = len(u)
    parts.append((f"длина {n}", {4: 30, 5: 26, 6: 21, 7: 16, 8: 12, 9: 8, 10: 5}.get(n, 2 if n > 10 else 0)))
    core = u.replace("_", "")

    if u in BRANDS:
        parts.append(("бренд", 30))
    elif u in WORDS:
        parts.append(("словарное слово", 24))
    else:
        subs = [w for w in (BRANDS | WORDS) if len(w) >= 4 and w in u]
        if subs:
            parts.append((f"содержит «{max(subs, key=len)}»", 8))

    letters = [c for c in core if c.isalpha()]
    if letters:
        v = sum(c in VOW for c in letters) / len(letters)
        run = max((len(m.group()) for m in re.finditer(r"[^aeiouy0-9]+", core)), default=0)
        if 0.25 <= v <= 0.6 and run <= 3:
            parts.append(("легко читается", 14))
        elif run >= 4:
            parts.append(("тяжёлые согласные", -8))

    h = len(core) // 2
    if len(set(core)) == 1:
        parts.append(("один символ", 20))
    elif len(core) % 2 == 0 and core[:h] * 2 == core:
        parts.append(("повтор (abab)", 12))
    elif core == core[::-1]:
        parts.append(("палиндром", 10))

    d = sum(c.isdigit() for c in u)
    if d:
        parts.append(("цифры", -min(15, 4 * d)))
    if "_" in u:
        parts.append(("подчёркивание", -8 * u.count("_")))

    return max(0, min(100, sum(p for _, p in parts))), parts


TON_BASE = {4: 4000, 5: 400, 6: 80, 7: 25, 8: 10, 9: 5}


def estimate_ton(u: str, sc: int):
    """Грубый ориентир рыночной цены в TON (диапазон). Калибруй по реальным продажам Fragment."""
    mid = TON_BASE.get(len(u), 2) * (0.1 + 1.4 * (sc / 100) ** 2)
    return max(1, round(mid * 0.6)), max(2, round(mid * 1.5))


def tier(sc: int) -> str:
    return "💎 топ" if sc >= 75 else "🔥 сильный" if sc >= 55 else "👍 норм" if sc >= 35 else "🪨 слабый"


# ---------- генерация кандидатов ----------
MODES = ("pron", "brand", "word", "pattern")
CONS, VOWS = "bcdfghjklmnprstvwz", "aeiou"
PREFIX, SUFFIX = ("my", "the", "get", "go", "its"), ("app", "hq", "pro", "ru", "world", "club")


def _pron(n):
    start = random.choice([0, 1])
    return "".join(random.choice(CONS if (i + start) % 2 == 0 else VOWS) for i in range(n))


def _pattern(n):
    a, b = random.sample("abcdefghijklmnoprstuvwxz", 2)
    kind = random.choice(["abab", "aaab", "abba"])
    s = {"abab": (a + b) * n, "aaab": a * (n - 1) + b, "abba": (a + b + b + a) * n}[kind]
    return s[:n]


def candidates(length: int, mode: str, count: int = 40):
    if mode in ("brand", "word"):
        src = BRANDS if mode == "brand" else WORDS
        pool = {w for w in src if len(w) == length}
        pool |= {p + w for w in src for p in PREFIX if len(p + w) == length}
        pool |= {w + s for w in src for s in SUFFIX if len(w + s) == length}
        pool = list(pool)
        random.shuffle(pool)
        return pool[:count]
    gen = _pattern if mode == "pattern" else _pron
    out = set()
    for _ in range(count * 6):
        out.add(gen(length))
        if len(out) >= count:
            break
    return [u for u in out if is_valid(u)]
