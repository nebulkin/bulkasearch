"""Оценка, рейтинг и генерация юзернеймов. Без сети — легко тестировать."""
import random
import re
from functools import lru_cache
from pathlib import Path

BASE = Path(__file__).parent


def _load(name):
    p = BASE / name
    if not p.exists():
        return set()
    return {w.lower() for w in re.split(r"[\s,]+", p.read_text("utf-8")) if len(w) >= 3}


BRANDS, WORDS = _load("brands.txt"), _load("words.txt")
LONG = [w for w in (BRANDS | WORDS) if len(w) >= 5]  # для бонуса «содержит слово»
VOW = set("aeiouy")
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{3,31}$")  # 4–32 символа


def _dels(x: str) -> set:
    return {x} | {x[:i] + x[i + 1:] for i in range(len(x))}


# индекс для быстрого поиска «почти-слов»: ник и слово делят вариант «с выкинутой буквой»
_DEL: dict[str, list] = {}
for _w in BRANDS | WORDS:
    if len(_w) >= 4:
        for _d in _dels(_w):
            _DEL.setdefault(_d, []).append(_w)


def _edit1(a: str, b: str) -> bool:
    """Ровно одна правка: замена, вставка или удаление одной буквы."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    s, l = (a, b) if len(a) < len(b) else (b, a)
    return any(l[:i] + l[i + 1:] == s for i in range(len(l)))


@lru_cache(maxsize=20000)
def is_near(u: str) -> bool:
    """«Почти-слово»: не слово и не бренд, но от одного из них отличается одной буквой (doutta ← outta, enuch ← eunuch)."""
    if u in WORDS or u in BRANDS:
        return False
    return any(_edit1(u, w) for d in _dels(u) for w in _DEL.get(d, ()))


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
        subs = [w for w in LONG if w in u] if len(u) > 5 else []
        if subs and not is_near(u):  # «почти-слову» (слово с одной правкой) бонус не положен: оно и так «содержит» слово
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
    elif core == core[::-1] and (u in WORDS or u in BRANDS):  # палиндром ценен только у настоящего слова/бренда, не у «каши»
        parts.append(("палиндром", 10))

    d = sum(c.isdigit() for c in u)
    if d:
        parts.append(("цифры", -min(15, 4 * d)))
    if "_" in u:
        parts.append(("подчёркивание", -8 * u.count("_")))

    return max(0, min(100, sum(p for _, p in parts))), parts


TON_BASE = {4: 4000, 5: 400, 6: 80, 7: 25, 8: 10, 9: 5}


def _is_pattern(core: str) -> bool:
    h = len(core) // 2
    return len(set(core)) == 1 or (len(core) % 2 == 0 and core[:h] * 2 == core)


# множители цены для ников, которые не слово и не бренд (числа — прикидка, не рыночные данные!)
NONWORD_K = 0.15   # выдуманный набор букв, «почти-слово»
COMPOUND_K = 0.4   # составной ник «слово + слово/буквы» (mystorm)


def estimate_ton(u: str, sc: int):
    """Грубый ориентир рыночной цены в TON (диапазон). НЕ котировка: по реальным продажам Fragment не калибровалась."""
    mid = TON_BASE.get(len(u), 2) * (0.1 + 1.4 * (sc / 100) ** 2)
    if u not in WORDS and u not in BRANDS and not _is_pattern(u.replace("_", "")):
        compound = any(label.startswith("содержит") for label, _ in score(u)[1])
        mid *= COMPOUND_K if compound else NONWORD_K
    return max(1, round(mid * 0.6)), max(2, round(mid * 1.5))


def tier(sc: int) -> str:
    return "💎 топ" if sc >= 75 else "🔥 сильный" if sc >= 55 else "👍 норм" if sc >= 35 else "🪨 слабый"


# ---------- генерация кандидатов ----------
MODES = ("pron", "brand", "word", "pattern")
CONS, VOWS = "bcdfghjklmnprstvwz", "aeiou"
PREFIX, SUFFIX = ("my", "the", "get", "go", "its"), ("app", "hq", "pro", "ru", "world", "club")


NICE_C = "bdfgklmnprstv"  # «приятные» согласные (без w, z, h, j, q, x — они дают уродливые сочетания)
NICE_V = "aaeeiioou"  # u реже остальных
END_C = "nrlsmxkt"  # красивые концовки для нечётных длин
CLUSTERS = ("tr", "st", "br", "pr", "cl", "fl", "gr", "pl", "sk", "dr", "kr", "sn", "sl", "sp")


def _pron(n):
    """Слоговые ники вида lumen, brava, kolari: согласная+гласная, иногда кластер в начале, красивая концовка."""
    s = ""
    if n >= 5 and random.random() < 0.25:
        s = random.choice(CLUSTERS) + random.choice(NICE_V)
    while len(s) < n:
        if n - len(s) == 1:
            s += random.choice(END_C) if s[-1] in VOWS else random.choice(NICE_V)
        else:
            s += random.choice(NICE_C) + random.choice(NICE_V)
    return s[:n]


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
