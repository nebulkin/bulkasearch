import asyncio
import html
import logging
import os
import re
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiohttp import web

import card
import db
import lookup
import scanner
import scoring
from checker import STATUS, Checker

logging.basicConfig(level=logging.INFO)
bot = Bot(os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
checker = Checker()
OWNER_ID = int(os.getenv("OWNER_ID", 0))  # твой Telegram ID: только ты можешь менять ник аккаунта
MAX_TRAPS = int(os.getenv("MAX_TRAPS", 10))
TRAP_INTERVAL = int(os.getenv("TRAP_INTERVAL", 600))  # сек между проверками ловушек
busy: set[int] = set()
last_find: dict[int, float] = {}
FIND_GAP = int(os.getenv("FIND_GAP", 20))  # сек между поисками одного человека
lk_busy: set[int] = set()  # кто сейчас ждёт досье
last_lookup: dict[int, float] = {}
LOOKUP_GAP = int(os.getenv("LOOKUP_GAP", 4))  # сек между досье одного человека
BTN_STYLE = os.getenv("BTN_STYLE", "1") != "0"  # цветные кнопки (Bot API 9.4+); BTN_STYLE=0 — выключить, если вдруг мешают

BOT_DESCRIPTION = (
    "🥐 BulkaSearch — охотник за красивыми Telegram-юзернеймами.\n\n"
    "🕵️ Досье на любой ник: свободен, кто владелец, что на Fragment\n"
    "🔎 Находит свободные ники нужной длины: звучные, бренды, слова, паттерны\n"
    "📊 Оценка 0–100 и цена в TON на красивой карточке\n"
    "🪤 Ловушка: напишу, как только ник освободится\n\n"
    "Нажми Start и пришли ник — я его изучу!"
)
BOT_SHORT = "🥐 Досье на любой юзернейм, поиск свободных и ловушки. Свежие ники — горячими!"

HOME = (
    "🥐 <b>BulkaSearch</b>\n"
    "<i>свежие юзернеймы — горячими, пока не остыли</i>\n\n"
    "<blockquote>🕵️ <b>Досье:</b> пришли ник или <code>/find news</code>\n"
    "🔎 <b>Поиск</b> свободных за пару секунд\n"
    "📊 <b>Оценка</b> и цена в TON\n"
    "🪤 <b>Ловушка</b> — скажу, когда освободится</blockquote>\n\n"
    "Попробуй прямо сейчас: <code>bulka</code> 👇"
)
HELP = (
    "🥐 <b>BulkaSearch · команды</b>\n\n"
    "<blockquote>🕵️ <code>/find news</code> — досье на ник: свободен ли, кто владелец, что на Fragment, оценка и цена\n"
    "🔎 <code>/find</code> — поиск свободного кнопками (или <code>/find 5-6 pron</code>)\n"
    "⚡ <code>/find quick</code> — быстрый поиск (6–7 символов)\n"
    "📊 <code>/rate name</code> — то же досье (или просто пришли ник)\n"
    "🪤 <code>/trap name</code> — ловушка: сообщу, когда ник освободится\n"
    "👤 <code>/cabinet</code> — кабинет, избранное, ловушки\n"
    "✏️ <code>/set name</code> — поставить ник на аккаунт (владелец)\n"
    "🗑 <code>/unfav name</code> · <code>/untrap name</code> — удалить</blockquote>\n\n"
    "💡 Ники до 4 символов обычно продаются только на Fragment.\n"
    "ℹ️ Если сам ник совпадает со стилем (<code>word</code>, <code>brand</code>, <code>pron</code>, <code>pattern</code>) — используй <code>/rate</code>."
)
MODE_NAMES = {"pron": "🗣 Звучные", "brand": "🏷 Бренды", "word": "📖 Словарные", "pattern": "🔁 Паттерны"}
BAKING = ("🥐 Достаю свежие ники из печи", "🔥 Греем юзернеймы", "🍞 Замешиваю варианты", "✨ Посыпаем сахарной пудрой")
KIND_TXT = {"user": ("👤", "пользователь"), "bot": ("🤖", "бот"), "channel": ("📢", "канал"),
            "group": ("👥", "группа"), "unknown": ("❔", "аккаунт")}
MEMBERS_TXT = {"channel": "подписчиков", "group": "участников", "bot": "пользователей в месяц"}


def esc(s) -> str:
    return html.escape(str(s))


def bar(sc: int, n: int = 10) -> str:
    k = max(0, min(n, round(sc / 100 * n)))
    return "▰" * k + "▱" * (n - k)


def btn(text, data=None, style=None, url=None):
    """Кнопка. style: primary / success / danger — цветные кнопки (если клиент не умеет, будет обычная)."""
    kw = {"style": style} if (style and BTN_STYLE) else {}
    if url:
        return InlineKeyboardButton(text=text, url=url, **kw)
    return InlineKeyboardButton(text=text, callback_data=data, **kw)


HOME_BTN = [btn("🥐 В меню", "m:home")]


def home_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🕵️ Досье на ник", "m:rate", "primary")],
        [btn("🔎 Найти свободный", "m:find", "primary"), btn("⚡ Быстрый поиск", "q:go", "success")],
        [btn("⭐ Мой кабинет", "m:cab"), btn("❓ Помощь", "m:help")],
    ])


def mode_kb():
    ms = list(MODE_NAMES.items())
    rows = [[btn(n, f"fm:{k}") for k, n in ms[i:i + 2]] for i in range(0, len(ms), 2)]
    return InlineKeyboardMarkup(inline_keyboard=[[btn("⚡ Быстрый поиск", "q:go", "success")]] + rows + [HOME_BTN])


def len_kb(mode):
    singles = [btn(str(n), f"f:{mode}:{n}") for n in (4, 5, 6, 7)]
    ranges = [btn(f"{n}–{n + 1}", f"f:{mode}:{n}-{n + 1}") for n in (4, 5, 6, 7)]
    return InlineKeyboardMarkup(inline_keyboard=[singles, ranges, [btn("⬅️ Стиль", "m:find")] + HOME_BTN])


def results_kb(found, mode, length):
    rows = [[btn(f"{'✅' if st == 'free' else '💎'} @{u} · {sc}", f"r:{u}")] for sc, u, st in found[:6]]
    rows.append([btn("🔄 Ещё свежий", "q:go" if length == "quick" else f"f:{mode}:{length}", "primary")] + HOME_BTN)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def nick_kb(u, uid, free=True):
    """Кнопки под карточкой свободного ника (поиск, оповещения)."""
    rows = [[btn("⭐ В избранное", f"fav:{u}", "success"), btn("🪤 Ловушка", f"trap:{u}")]]
    if uid == OWNER_ID and free:
        rows.append([btn("✏️ Поставить ник (userbot)", f"set:{u}", "primary")])
    rows.append([btn("🕵️ Досье", f"r:{u}")] + HOME_BTN)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lookup_kb(u, uid, st):
    """Кнопки под досье: свободный — забирать, занятый — ловушка; всегда ссылки и «Обновить»."""
    if st == "free":
        first = [btn("⭐ В избранное", f"fav:{u}", "success"), btn("🪤 Ловушка", f"trap:{u}")]
    else:
        first = [btn("🪤 Ловушка: сообщу, когда освободится", f"trap:{u}", "primary")]
    rows = [first]
    if st == "free" and uid == OWNER_ID:
        rows.append([btn("✏️ Поставить ник (userbot)", f"set:{u}", "primary")])
    if st != "free":
        rows.append([btn("⭐ В избранное", f"fav:{u}")])
    rows.append([btn("🔗 В Telegram", url=f"https://t.me/{u}"), btn("💎 Fragment", url=f"https://fragment.com/username/{u}")])
    rows.append([btn("🔄 Обновить", f"lk:{u}")] + HOME_BTN)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show(c: CallbackQuery, text, markup):
    """Меняет сообщение; если это картинка — удаляет и шлёт новое."""
    if c.message.photo:
        await c.message.delete()
        await c.message.answer(text, reply_markup=markup)
    else:
        await c.message.edit_text(text, reply_markup=markup)
    await c.answer()


def tags_for(u: str) -> list[str]:
    """Короткие метки для карточки: чем ценен ник."""
    out = []
    for label, p in scoring.score(u)[1]:
        if label == "бренд":
            out.append("бренд")
        elif label == "словарное слово":
            out.append("слово")
        elif label.startswith("содержит"):
            out.append("составной")
        elif label == "палиндром":
            out.append("палиндром")
        elif label.startswith("повтор") or label == "один символ":
            out.append("паттерн")
    if scoring.is_near(u):
        out.append("почти-слово")
    return out[:3]


async def send_card(target: Message, u, sc, st, caption, markup, profile=None):
    lo, hi = scoring.estimate_ton(u, sc)
    png = await asyncio.to_thread(card.render_card, u, sc, st, lo, hi, profile, tags_for(u))
    await target.answer_photo(BufferedInputFile(png, f"{u}.jpg"), caption=caption, reply_markup=markup)


async def add_fav(uid, u):
    sc = scoring.score(u)[0]
    await db.run("INSERT INTO favs(user_id,username,score) VALUES(?,?,?) ON CONFLICT(user_id,username) DO UPDATE SET score=excluded.score", (uid, u, sc))
    return f"⭐ @{u} в избранном"


async def add_trap(uid, u):
    if not scoring.is_valid(u):
        return "⚠️ Недопустимый ник"
    n = (await db.run("SELECT COUNT(*) FROM traps WHERE user_id=?", (uid,), fetch=True))[0][0]
    if n >= MAX_TRAPS:
        return f"Лимит ловушек: {MAX_TRAPS}. Удали лишнюю: /untrap name"
    st = await smart_check(u, fresh=True)
    if st == "free":
        return f"✅ @{u} уже свободен — забирай!"
    if st == "invalid":
        return "⚠️ Недопустимый ник"
    await db.run("INSERT INTO traps(user_id,username,state) VALUES(?,?,?) ON CONFLICT(user_id,username) DO UPDATE SET state=excluded.state", (uid, u, st))
    return f"🪤 Ловушка на @{u} поставлена (проверка каждые {TRAP_INTERVAL // 60} мин)"


@dp.message(Command("start"))
async def start(m: Message):
    await db.touch(m.from_user.id, m.from_user.full_name)
    await m.answer(HOME, reply_markup=home_kb())


@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(HELP, reply_markup=InlineKeyboardMarkup(inline_keyboard=[HOME_BTN]))


def all_checkers():
    """Основной аккаунт + подключённые сканеры: любой из них может проверять ники."""
    return [checker] + scanner.live_checkers()


def any_ready() -> bool:
    return any(c.ready() for c in all_checkers())


def any_link() -> bool:
    """Есть ли хоть один аккаунт с живой связью с Telegram."""
    return any(c.client is not None and c.client.is_connected() for c in all_checkers())


def cooling_left() -> int:
    """0 — хотя бы один аккаунт может работать; иначе сколько секунд до ближайшего."""
    cs = all_checkers()
    if any(c.ready() for c in cs):
        return 0
    return min((c.cooling() for c in cs if c.cooling()), default=0)


def acc_lines():
    out = []
    for c in all_checkers():
        left = c.cooling()
        if c.client is not None and not c.client.is_connected():
            out.append(f"• {c.name}: нет связи с Telegram 📡 (переподключаюсь)")
            continue
        out.append(f"• {c.name}: " + (f"отдыхает ещё ~{left // 60 + 1} мин 🧊" if left else "готов ✅"))
    return out


async def smart_check(u: str, fresh: bool = False) -> str:
    """Проверка ника любым свободным аккаунтом (по кругу, самым отдохнувшим). Уснул или потерял связь один — берём другой."""
    if not fresh and checker.is_taken(u):
        return "taken"
    tried = set()
    offline = False
    while True:
        c = min((x for x in all_checkers() if x.ready() and id(x) not in tried),
                key=lambda x: x.last_used, default=None)
        if c is None:
            return "offline" if (offline or not any_link()) else "flood"
        tried.add(id(c))
        st = await c.check(u, True)
        if st == "offline":
            offline = True
            continue  # у этого аккаунта пропала связь — пробуем следующий
        if st == "flood":
            continue  # этот аккаунт отдыхает — пробуем следующий
        if st == "taken":
            checker._mark_taken(u)
        return st


async def take_from_pool(lengths):
    """Берёт лучший ник из пула, перепроверяет одним запросом. Не больше 3 проверок основного аккаунта."""
    ph = ",".join("?" * len(lengths))
    rows = await db.run(f"SELECT username, score FROM pool WHERE length IN ({ph}) ORDER BY score DESC, found DESC LIMIT 3",
                        tuple(lengths), fetch=True)
    for u, sc in rows:
        await db.run("DELETE FROM pool WHERE username=?", (u,))  # забираем, чтобы не отдать двоим
        st = await smart_check(u, fresh=True)
        if st == "free":
            return sc, u, st
        if st in ("flood", "offline"):
            break
    return None


def parse_len(spec) -> list[int] | None:
    """5 -> [5]; '5-6' -> [5, 6]; 'quick' -> [7, 6] (длиннее — свободнее)."""
    if spec == "quick":
        return [7, 6]
    m = re.fullmatch(r"(\d+)(?:[-–](\d+))?", str(spec))
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2) or lo)
    if not (4 <= lo <= hi <= 12) or hi - lo > 3:
        return None
    return list(range(lo, hi + 1))


def build_cands(lengths, mode, total, by_score=True):
    """Кандидаты по всем длинам вперемешку (по кругу); занятые из кеша пропускаем."""
    per = max(10, total // len(lengths))
    lists = []
    for n in lengths:
        cs = [u for u in scoring.candidates(n, mode, per) if not checker.is_taken(u)]
        if by_score:
            cs.sort(key=lambda x: scoring.score(x)[0], reverse=True)
        lists.append(cs)
    out = []
    for i in range(max(map(len, lists), default=0)):
        out += [cs[i] for cs in lists if i < len(cs)]
    return out


def found_caption(head, u, sc, foot):
    return f"{head}\n\n<b>@{u}</b>\n📊 <b>{sc}</b>/100 · {scoring.tier(sc)}\n<code>{bar(sc)}</code>\n<i>{foot}</i>"


async def run_find(target: Message, user, length, mode):
    uid = user.id
    quick = length == "quick"
    lengths = parse_len(length)
    if not lengths:
        return await target.answer("📏 Длина — от 4 до 12, диапазон до 4 цифр, например <code>/find 5-6</code>")
    if quick:
        mode = "pron"
    label = f"{lengths[0]}–{lengths[-1]}" if len(lengths) > 1 else str(lengths[0])
    if uid in busy:
        return await target.answer("⏳ Предыдущий поиск ещё печётся, подожди немного 🥐")
    if (left := cooling_left()):
        return await target.answer(f"🧊 Печь остывает: Telegram просил притормозить. Вернусь через ~{left // 60 + 1} мин, "
                                   "так аккаунт останется целым.")
    wait = FIND_GAP - (time.time() - last_find.get(uid, 0))
    if wait > 0:
        return await target.answer(f"🥐 Булки ещё не остыли, подожди {int(wait) + 1} сек.")
    last_find[uid] = time.time()
    if mode == "pron":  # сначала пробуем готовый свободный ник из пула — это мгновенно
        got = await take_from_pool(scanner.POOL_LENGTHS if quick else lengths)
        if got:
            sc, u, st = got
            await db.touch(uid, user.full_name, searched=True)
            cap = found_caption("🎉 <b>Свободный ник найден!</b>", u, sc, "⚡ из свежей выпечки · проверено только что")
            await send_card(target, u, sc, st, cap, nick_kb(u, user.id))
            return await target.answer("Ещё один? 👇", reply_markup=results_kb([], mode, length if quick else label))
    cands = build_cands(lengths, mode, 40 if quick else 60, by_score=not quick)
    if not cands:
        return await target.answer("🤷 Для такой длины и стиля нет новых вариантов. Попробуй другую длину.", reply_markup=len_kb(mode))
    busy.add(uid)
    try:
        await db.touch(uid, user.full_name, searched=True)
        msg = await target.answer(f"{BAKING[0]}…")
        hit, fallback, checked, down = None, None, 0, None
        for u in cands:
            st = await smart_check(u)
            checked += 1
            if st in ("flood", "offline"):
                down = st
                break
            if st == "free":
                hit = (scoring.score(u)[0], u, st)
                break
            if st == "fragment" and fallback is None:
                fallback = (scoring.score(u)[0], u, st)
            if checked % 8 == 0:  # живой прогресс, но не чаще — лимиты Telegram
                try:
                    await msg.edit_text(f"{BAKING[(checked // 8) % len(BAKING)]}… <i>проверено {checked}</i>")
                except Exception:
                    pass
        await msg.delete()
        best = hit or fallback
        if not best and down == "offline":
            return await target.answer("📡 Связь с Telegram пропала, аккаунты переподключаются. Попробуй через минуту 🥐")
        if not best and (left := cooling_left()):
            return await target.answer("🧊 Telegram просит притормозить, я ставлю поиск на паузу, чтобы не рисковать аккаунтом. "
                                       f"Загляни через ~{left // 60 + 1} мин.")
        if not best and checked and not any_ready():
            return await target.answer("🧊 Дневной лимит проверок исчерпан, продолжим завтра. Так аккаунт в безопасности.")
        if not best:
            return await target.answer(f"🥲 Проверил {checked}, и всё занято. Булки закончились, жми «Ещё свежий»!",
                                       reply_markup=results_kb([], mode, length if quick else label))
        sc, u, st = best
        head = "🎉 <b>Свободный ник найден!</b>" if st == "free" else "💎 <b>Нашёл на Fragment</b>"
        cap = found_caption(head, u, sc, f"{'⚡ Быстрый поиск' if quick else MODE_NAMES[mode]} · {label} симв. · проверено {checked}")
        await send_card(target, u, sc, st, cap, nick_kb(u, user.id, st == "free"))
        await target.answer("Ещё один? 👇", reply_markup=results_kb([], mode, length if quick else label))
    finally:
        busy.discard(uid)


@dp.message(Command("find", "whois"))
async def find(m: Message, command: CommandObject):
    args = (command.args or "").lower().split()
    if not args:
        return await m.answer("🔎 <b>Что ищем?</b>\nВыбери стиль ника 👇\n\n"
                              "<i>А чтобы узнать всё про конкретный ник — <code>/find news</code></i>", reply_markup=mode_kb())
    if "quick" in args or "fast" in args:
        return await run_find(m, m.from_user, "quick", "pron")
    search = any(a in scoring.MODES or re.fullmatch(r"\d+(?:[-–]\d+)?", a) for a in args)
    if not search:  # /find news — досье на ник
        if scoring.is_valid(scoring.normalize(args[0])):
            return await do_lookup(m, m.from_user, args[0])
        return await m.answer("⚠️ Ник: 4–32 символа, a–z, 0–9, _, начинается с буквы. Например: <code>/find news</code>")
    length = next((a for a in args if re.fullmatch(r"\d+(?:[-–]\d+)?", a)), "5")
    mode = next((a for a in args if a in scoring.MODES), "pron")
    await run_find(m, m.from_user, length, mode)


@dp.callback_query(F.data.startswith("f:"))
async def cb_find(c: CallbackQuery):
    _, mode, length = c.data.split(":")
    await c.answer()
    await run_find(c.message, c.from_user, length, mode)


@dp.callback_query(F.data == "q:go")
async def cb_quick(c: CallbackQuery):
    await c.answer()
    await run_find(c.message, c.from_user, "quick", "pron")


@dp.callback_query(F.data.startswith("fm:"))
async def cb_mode(c: CallbackQuery):
    mode = c.data[3:]
    await show(c, f"{MODE_NAMES[mode]}\n\n📏 <b>Какой длины ник?</b>\nОдна цифра — точная длина, «5–6» — ищу сразу в двух 👇", len_kb(mode))


# ---------- досье на ник ----------
def lookup_caption(u, sc, parts, st, info, frag) -> str:
    lo, hi = scoring.estimate_ton(u, sc)
    L = []
    if st == "free":
        L += [f"✅ <b>@{u}</b> свободен!", "<i>Забирай, пока не заняли 🏃</i>"]
    elif st == "fragment":
        L += [f"💎 <b>@{u}</b> продаётся на Fragment", "<i>Обычным аккаунтом ник не занять — только покупка</i>"]
    elif info:
        emoji, kind = KIND_TXT.get(info["kind"], KIND_TXT["unknown"])
        name = esc(info["name"] or "без имени")
        flags = (" ✅" if info.get("verified") else "") + (" ⚠️ <b>SCAM</b>" if info.get("scam") else "") + \
                (" 🚫 <b>FAKE</b>" if info.get("fake") else "") + (" ⭐" if info.get("premium") else "")
        L += [f"🕵️ <b>Досье · @{u}</b>", "", f"❌ Занят · {emoji} {kind}", f"<b>{name}</b>{flags}"]
        bio = (info.get("bio") or "").strip()
        if bio:
            L.append(f"<blockquote>{esc(bio[:260])}{'…' if len(bio) > 260 else ''}</blockquote>")
        if info.get("members") is not None and info["kind"] in MEMBERS_TXT:
            L.append(f"👥 {card.fmt_num(info['members'])} {MEMBERS_TXT[info['kind']]}")
    elif st == "taken":
        L += [f"🕵️ <b>Досье · @{u}</b>", "", "❌ Занят",
              "<i>Владельца не видно: аккаунт скрыт, удалён или ник зарезервирован</i>"]
    else:
        L += [f"🕵️ <b>@{u}</b>", "", f"{STATUS.get(st, STATUS['unknown'])}"]
    if frag and frag["state"] != "none":
        price = f" · {frag['price']} TON" if frag.get("price") else ""
        L += ["", {"avail": "🛒 Fragment: продаётся", "auction": "🔨 Fragment: аукцион", "sold": "🏷 Fragment: продан"}[frag["state"]] + price]
    L += ["", f"📊 <b>{sc}</b>/100 · {scoring.tier(sc)}", f"<code>{bar(sc)}</code>", f"💰 ≈ {lo}–{hi} TON"]
    if st == "free":
        detail = "\n".join(f"{'+' if p >= 0 else '−'}{abs(p)} {label}" for label, p in parts if p)
        if detail:
            L += ["", f"<blockquote expandable>{esc(detail)}</blockquote>"]
    return "\n".join(L)


async def do_lookup(target: Message, user, raw: str, fresh: bool = False):
    """Досье на ник: свободен / занят (и кем) / на Fragment + оценка и цена. Любой ввод ника приходит сюда."""
    u = scoring.normalize(raw)
    if not scoring.is_valid(u):
        return await target.answer("⚠️ Ник: 4–32 символа, a–z, 0–9, _, начинается с буквы, без «__» и «_» в конце.")
    uid = user.id
    if uid in lk_busy:
        return await target.answer("⏳ Предыдущее досье ещё собирается, секунду 🥐")
    wait = LOOKUP_GAP - (time.time() - last_lookup.get(uid, 0))
    if wait > 0:
        return await target.answer(f"🥐 Не так быстро, подожди {int(wait) + 1} сек.")
    last_lookup[uid] = time.time()
    lk_busy.add(uid)
    try:
        await db.touch(uid, user.full_name, searched=True)
        msg = await target.answer(f"🕵️ Изучаю <b>@{u}</b>…")
        sc, parts = scoring.score(u)
        pst, info = await lookup.profile(u, all_checkers(), fresh=fresh)
        if pst == "found":
            st = "taken"
            checker._mark_taken(u)
        elif pst in ("notfound", "unknown"):  # владельца нет — спросим, свободен ник или продаётся
            st = await smart_check(u, fresh=True)
        else:  # invalid / flood / offline
            st = pst
        frag = await lookup.fragment(u, fresh) if st in ("taken", "fragment") else None
        try:
            await msg.delete()
        except Exception:
            pass
        if st == "invalid":
            return await target.answer("⚠️ Telegram говорит, что такой ник недопустим.")
        if st == "offline":
            return await target.answer("📡 Связь с Telegram пропала, аккаунты переподключаются. Попробуй через минуту 🥐")
        if st == "flood":
            left = cooling_left()
            return await target.answer("🧊 Telegram просит притормозить, поэтому досье пока не собрать. "
                                       + (f"Загляни через ~{left // 60 + 1} мин." if left else "Попробуй чуть позже."))
        cap = lookup_caption(u, sc, parts, st, info, frag)
        await send_card(target, u, sc, st, cap, lookup_kb(u, uid, st), profile=info)
        return True
    finally:
        lk_busy.discard(uid)


async def do_rate(target: Message, user, raw: str):
    await do_lookup(target, user, raw)


@dp.callback_query(F.data.startswith("r:"))
async def cb_rate(c: CallbackQuery):
    await c.answer()
    await do_lookup(c.message, c.from_user, c.data[2:])


@dp.callback_query(F.data.startswith("lk:"))
async def cb_refresh(c: CallbackQuery):
    await c.answer("Обновляю…")
    if await do_lookup(c.message, c.from_user, c.data[3:], fresh=True):
        try:
            await c.message.delete()  # старую карточку убираем, только если новая уже пришла
        except Exception:
            pass


@dp.callback_query(F.data.startswith("m:"))
async def cb_menu(c: CallbackQuery):
    act = c.data[2:]
    if act == "home":
        await show(c, HOME, home_kb())
    elif act == "find":
        await show(c, "🔎 <b>Что ищем?</b>\nВыбери стиль ника 👇", mode_kb())
    elif act == "rate":
        await show(c, "🕵️ <b>Досье на ник</b>\n\nПришли ник сообщением, например <code>news</code> или <code>@bulka</code>, "
                      "либо командой <code>/find news</code>.\n\n"
                      "<blockquote>✅ свободен или занят\n👤 кто владелец: имя, описание, подписчики\n"
                      "💎 что на Fragment\n📊 оценка и цена в TON</blockquote>",
                   InlineKeyboardMarkup(inline_keyboard=[HOME_BTN]))
    elif act == "help":
        await show(c, HELP, InlineKeyboardMarkup(inline_keyboard=[HOME_BTN]))
    elif act == "cab":
        text, kbd = await cab_view(c.from_user)
        await show(c, text, kbd)


@dp.message(Command("rate"))
async def rate(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("Использование: <code>/rate name</code>")
    await do_lookup(m, m.from_user, command.args.split()[0])


@dp.message(Command("trap"))
async def trap(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("Использование: <code>/trap name</code>")
    await m.answer(await add_trap(m.from_user.id, scoring.normalize(command.args.split()[0])))


@dp.message(Command("set"))
async def set_cmd(m: Message, command: CommandObject):
    if m.from_user.id != OWNER_ID:
        return await m.answer("Команда только для владельца бота.")
    u = scoring.normalize((command.args or "").split()[0] if command.args else "")
    if not scoring.is_valid(u):
        return await m.answer("Использование: <code>/set name</code>")
    await m.answer(f"Поставить <b>@{u}</b> на твой аккаунт?\nСтарый ник освободится — его могут занять.", reply_markup=confirm_kb(u))


def confirm_kb(u):
    return InlineKeyboardMarkup(inline_keyboard=[[
        btn(f"✅ Да, @{u}", f"setok:{u}", "success"),
        btn("✖️ Отмена", "setno", "danger"),
    ]])


@dp.callback_query(F.data.startswith("set:"))
async def cb_set(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        return await c.answer("Только для владельца", show_alert=True)
    u = c.data[4:]
    await c.message.answer(f"Поставить <b>@{u}</b> на твой аккаунт?\nСтарый ник освободится — его могут занять.", reply_markup=confirm_kb(u))
    await c.answer()


@dp.callback_query(F.data == "setno")
async def cb_setno(c: CallbackQuery):
    await c.message.edit_text("Отменено.")
    await c.answer()


@dp.callback_query(F.data.startswith("setok:"))
async def cb_setok(c: CallbackQuery):
    if c.from_user.id != OWNER_ID:
        return await c.answer("Только для владельца", show_alert=True)
    u = c.data[6:]
    st = await checker.set_username(u)
    await c.message.edit_text(f"<b>@{u}</b>: {STATUS[st]}")
    await c.answer()


@dp.message(Command("pool"))
async def pool_cmd(m: Message):
    if m.from_user.id != OWNER_ID:
        return await m.answer("Команда только для владельца бота.")
    rows = await db.run("SELECT length, COUNT(*) FROM pool GROUP BY length ORDER BY length", fetch=True)
    stock = ", ".join(f"{n} симв.: {c}" for n, c in rows) or "пусто"
    await m.answer(f"📦 <b>Склад свободных ников</b>\n<blockquote>{stock}</blockquote>\n\n"
                   f"🤖 <b>Сканеры</b>\n" + "\n".join(scanner.status_lines()) + "\n\n" + scanner.alert_line() + "\n" + scanner.seen_line() +
                   "\n\n🔑 <b>Аккаунты для поиска</b>\n" + "\n".join(acc_lines()) + f"\n\n🗄 База: {db.BACKEND}")


@dp.message(Command("clearpool"))
async def clearpool_cmd(m: Message):
    if m.from_user.id != OWNER_ID:
        return await m.answer("Команда только для владельца бота.")
    await db.run("DELETE FROM pool")
    await m.answer("🧹 Склад очищен, сканеры наполнят его заново.")


@dp.message(Command("rest"))
async def rest_cmd(m: Message, command: CommandObject):
    if m.from_user.id != OWNER_ID:
        return await m.answer("Команда только для владельца бота.")
    arg = (command.args or "").split()
    if not arg or not arg[0].isdigit():
        return await m.answer("Использование: <code>/rest 540</code> — основной аккаунт отдыхает 540 минут (0 — разбудить).")
    await checker.rest(int(arg[0]))
    await m.answer(f"🧊 Основной аккаунт отдыхает {arg[0]} мин. Остальные продолжают работать." if int(arg[0]) else "☀️ Основной аккаунт снова в строю.")


@dp.message(Command("untrap"))
async def untrap(m: Message, command: CommandObject):
    u = scoring.normalize(command.args or "")
    await db.run("DELETE FROM traps WHERE user_id=? AND username=?", (m.from_user.id, u))
    await m.answer(f"🪤 Ловушка на @{u} снята.")


@dp.message(Command("unfav"))
async def unfav(m: Message, command: CommandObject):
    u = scoring.normalize(command.args or "")
    await db.run("DELETE FROM favs WHERE user_id=? AND username=?", (m.from_user.id, u))
    await m.answer(f"🗑 @{u} убран из избранного.")


async def cab_view(user):
    uid = user.id
    await db.touch(uid, user.full_name)
    _, name, joined, searches = (await db.run("SELECT * FROM users WHERE id=?", (uid,), fetch=True))[0]
    favs = await db.run("SELECT username, score FROM favs WHERE user_id=? ORDER BY score DESC", (uid,), fetch=True)
    traps = await db.run("SELECT username, state FROM traps WHERE user_id=?", (uid,), fetch=True)
    fav_txt = "\n".join(f"@{u} · {s}/100 <code>{bar(s, 5)}</code>" for u, s in favs) or "пока пусто — жми ⭐ под карточкой"
    trap_txt = "\n".join(f"@{u} · {STATUS.get(s, s)}" for u, s in traps) or "пока пусто — жми 🪤 под карточкой"
    text = (
        f"👤 <b>{esc(name)}</b>\n<i>с нами с {time.strftime('%d.%m.%Y', time.gmtime(joined))} · поисков: {searches}</i>\n\n"
        f"⭐ <b>Избранное</b> · {len(favs)}\n<blockquote>{fav_txt}</blockquote>\n\n"
        f"🪤 <b>Ловушки</b> · {len(traps)}/{MAX_TRAPS}\n<blockquote>{trap_txt}</blockquote>"
    )
    rows = [[btn(f"⭐ @{u} · {s}", f"r:{u}")] for u, s in favs[:8]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows + [HOME_BTN])


@dp.message(Command("cabinet", "profile"))
async def cabinet(m: Message):
    text, kbd = await cab_view(m.from_user)
    await m.answer(text, reply_markup=kbd)


@dp.callback_query(F.data.startswith("fav:"))
async def cb_fav(c: CallbackQuery):
    await c.answer(await add_fav(c.from_user.id, c.data[4:]))


@dp.callback_query(F.data.startswith("trap:"))
async def cb_trap(c: CallbackQuery):
    await c.answer(await add_trap(c.from_user.id, c.data[5:]), show_alert=True)


@dp.message(F.text.regexp(r"^(?:https?://)?(?:t\.me/|telegram\.me/)?@?[A-Za-z0-9_]{4,32}$"))
async def plain_username(m: Message):
    await do_lookup(m, m.from_user, m.text)


async def notify_valuable(u: str, sc: int, who: str = "сканер", test: bool = False):
    """Ценная находка сканера: сразу шлём владельцу карточку с кнопкой «Поставить ник (userbot)»."""
    try:
        lo, hi = scoring.estimate_ton(u, sc)
        png = await asyncio.to_thread(card.render_card, u, sc, "free", lo, hi, None, tags_for(u))
        head = "🧪 <b>Тест оповещения</b>" if test else "🚨💎 <b>Ценная находка!</b>"
        cap = found_caption(head, u, sc, f"🤖 {who} · свободен прямо сейчас, забирай, пока не заняли!")
        kb = InlineKeyboardMarkup(inline_keyboard=[HOME_BTN]) if test else nick_kb(u, OWNER_ID)
        await bot.send_photo(OWNER_ID, BufferedInputFile(png, f"{u}.jpg"), caption=cap, reply_markup=kb)
    except Exception:
        logging.exception("notify_valuable")
        if not test:  # не вышло отправить — хотя бы не теряем ник, кладём на склад
            try:
                await db.run(
                    "INSERT INTO pool(username,length,score,found) VALUES(?,?,?,?) "
                    "ON CONFLICT(username) DO UPDATE SET found=excluded.found",
                    (u, len(u), sc, int(time.time())))
            except Exception:
                logging.exception("notify_valuable fallback")


@dp.message(Command("testalert"))
async def testalert_cmd(m: Message):
    if m.from_user.id != OWNER_ID:
        return await m.answer("Команда только для владельца бота.")
    await notify_valuable("bulka", scoring.score("bulka")[0], "тестовый сканер", test=True)


async def trap_loop():
    while True:
        await asyncio.sleep(TRAP_INTERVAL)
        try:
            for (u,) in await db.run("SELECT DISTINCT username FROM traps", fetch=True):
                st = await smart_check(u, fresh=True)
                if st not in ("free", "fragment"):
                    continue
                for uid, old in await db.run("SELECT user_id, state FROM traps WHERE username=?", (u,), fetch=True):
                    if old == st:
                        continue
                    where = "свободен, беги занимать! 🏃" if st == "free" else "появился на Fragment"
                    try:
                        await bot.send_message(uid, f"🪤🎉 <b>@{u}</b> {where}",
                                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("🕵️ Открыть досье", f"r:{u}", "primary")]]))
                    except Exception:
                        logging.exception("notify failed")
                    if st == "free":
                        await db.run("DELETE FROM traps WHERE user_id=? AND username=?", (uid, u))
                    else:
                        await db.run("UPDATE traps SET state=? WHERE user_id=? AND username=?", (st, uid, u))
        except Exception:
            logging.exception("trap loop")


async def start_web():
    async def ok(_):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000))).start()


async def main():
    await db.init()
    await bot.set_my_commands([
        BotCommand(command="start", description="🥐 Главное меню"),
        BotCommand(command="find", description="🕵️ Досье на ник: /find news · 🔎 поиск свободных"),
        BotCommand(command="rate", description="📊 Оценка и цена ника"),
        BotCommand(command="cabinet", description="⭐ Мой кабинет"),
        BotCommand(command="help", description="❓ Помощь"),
    ])
    try:  # описание, которое видно в пустом чате, и «О боте» в профиле
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_short_description(BOT_SHORT)
    except Exception:
        logging.exception("set description")
    await checker.start()
    if OWNER_ID:
        scanner.set_alert(notify_valuable)  # ценные находки сканеров — сразу владельцу
    scanner.start_all(os.getenv("TG_SESSION", ""))
    await start_web()
    asyncio.create_task(trap_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
