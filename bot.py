import asyncio
import html
import logging
import os
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

HOME = (
    "<b>✨ Охотник за юзернеймами</b>\n\n"
    "Ищу красивые свободные ники, оцениваю их и рисую карточку.\n"
    "Можно просто прислать ник сообщением — я его оценю."
)
HELP = (
    "<b>Команды</b>\n"
    "/find — поиск кнопками (или <code>/find 5 brand</code>)\n"
    "/rate name — оценка ника (или просто пришли ник)\n"
    "/trap name — ловушка: сообщу, когда ник освободится\n"
    "/cabinet — кабинет, избранное, ловушки\n"
    "/set name — поставить ник на аккаунт (userbot, только владелец)\n"
    "/unfav name · /untrap name — удалить\n\n"
    "Ники до 4 символов обычно только на Fragment."
)
MODE_NAMES = {"pron": "🗣 Читаемые", "brand": "🏷 Бренды", "word": "📖 Слова", "pattern": "🔁 Паттерны"}


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


HOME_BTN = [btn("🏠 Меню", "m:home")]


def home_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔎 Найти ник", "m:find"), btn("📊 Оценить ник", "m:rate")],
        [btn("👤 Кабинет", "m:cab"), btn("ℹ️ Помощь", "m:help")],
    ])


def mode_kb():
    ms = list(MODE_NAMES.items())
    rows = [[btn(n, f"fm:{k}") for k, n in ms[i:i + 2]] for i in range(0, len(ms), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows + [HOME_BTN])


def len_kb(mode):
    row1 = [btn(str(n), f"f:{mode}:{n}") for n in (4, 5, 6)]
    row2 = [btn(str(n), f"f:{mode}:{n}") for n in (7, 8, 9)]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, [btn("⬅️ Режим", "m:find")] + HOME_BTN])


def results_kb(found, mode, length):
    rows = [[btn(f"{'✅' if st == 'free' else '💎'} @{u} · {sc}", f"r:{u}")] for sc, u, st in found[:6]]
    rows.append([btn("🔄 Ещё", f"f:{mode}:{length}")] + HOME_BTN)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def nick_kb(u, uid):
    rows = [[btn("⭐ В избранное", f"fav:{u}"), btn("🪤 Ловушка", f"trap:{u}")]]
    if uid == OWNER_ID:
        rows.append([btn("✏️ Поставить ник (userbot)", f"set:{u}")])
    return InlineKeyboardMarkup(inline_keyboard=rows + [HOME_BTN])


async def show(c: CallbackQuery, text, markup):
    """Меняет сообщение; если это картинка — удаляет и шлёт новое."""
    if c.message.photo:
        await c.message.delete()
        await c.message.answer(text, reply_markup=markup)
    else:
        await c.message.edit_text(text, reply_markup=markup)
    await c.answer()


async def send_card(target: Message, u, sc, st, caption, markup):
    lo, hi = scoring.estimate_ton(u, sc)
    png = await asyncio.to_thread(card.render_card, u, sc, st, lo, hi)
    await target.answer_photo(BufferedInputFile(png, f"{u}.png"), caption=caption, reply_markup=markup)


async def add_fav(uid, u):
    sc = scoring.score(u)[0]
    await db.run("INSERT OR REPLACE INTO favs VALUES(?,?,?)", (uid, u, sc))
    return f"⭐ @{u} в избранном"


async def add_trap(uid, u):
    if not scoring.is_valid(u):
        return "⚠️ Недопустимый ник"
    n = (await db.run("SELECT COUNT(*) FROM traps WHERE user_id=?", (uid,), fetch=True))[0][0]
    if n >= MAX_TRAPS:
        return f"Лимит ловушек: {MAX_TRAPS}. Удали лишнюю: /untrap name"
    st = await checker.check(u)
    if st == "free":
        return f"✅ @{u} уже свободен — забирай!"
    if st == "invalid":
        return "⚠️ Недопустимый ник"
    await db.run("INSERT OR REPLACE INTO traps VALUES(?,?,?)", (uid, u, st))
    return f"🪤 Ловушка на @{u} поставлена (проверка каждые {TRAP_INTERVAL // 60} мин)"


@dp.message(Command("start", "help"))
async def start(m: Message):
    await db.touch(m.from_user.id, m.from_user.full_name)
    await m.answer(HOME, reply_markup=home_kb())


async def run_find(target: Message, user, length, mode):
    uid = user.id
    if uid in busy:
        return await target.answer("Предыдущий поиск ещё идёт, подожди немного.")
    cands = scoring.candidates(length, mode, 40)
    if not cands:
        return await target.answer("Для такой длины и режима нет кандидатов — попробуй другую.", reply_markup=len_kb(mode))
    busy.add(uid)
    try:
        await db.touch(uid, user.full_name, searched=True)
        msg = await target.answer(f"🔎 Проверяю {len(cands)} вариантов, ~{len(cands) // 2} сек…")
        found = []
        for u in cands:
            st = await checker.check(u)
            if st in ("free", "fragment"):
                found.append((scoring.score(u)[0], u, st))
        found.sort(reverse=True)
        await msg.delete()
        if not found:
            return await target.answer("Все проверенные варианты заняты 😕", reply_markup=results_kb([], mode, length))
        sc, u, st = found[0]
        lines = "\n".join(f"{'✅' if s == 'free' else '💎'} @{n} — {c}/100" for c, n, s in found[:10])
        cap = (f"<b>Лучший: @{u}</b>\nНашёл {len(found)} из {len(cands)} · {MODE_NAMES[mode]} · {length} симв.\n\n{lines}\n\n"
               "Нажми на ник ниже — карточка и действия.")
        await send_card(target, u, sc, st, cap, results_kb(found, mode, length))
    finally:
        busy.discard(uid)


@dp.message(Command("find"))
async def find(m: Message, command: CommandObject):
    args = (command.args or "").lower().split()
    if not args:
        return await m.answer("Что ищем? Выбери режим:", reply_markup=mode_kb())
    length = next((int(a) for a in args if a.isdigit()), 5)
    mode = next((a for a in args if a in scoring.MODES), "pron")
    if not 4 <= length <= 12:
        return await m.answer("Длина — от 4 до 12.")
    await run_find(m, m.from_user, length, mode)


@dp.callback_query(F.data.startswith("f:"))
async def cb_find(c: CallbackQuery):
    _, mode, length = c.data.split(":")
    await c.answer()
    await run_find(c.message, c.from_user, int(length), mode)


@dp.callback_query(F.data.startswith("fm:"))
async def cb_mode(c: CallbackQuery):
    mode = c.data[3:]
    await show(c, f"{MODE_NAMES[mode]}\nКакой длины ник нужен?", len_kb(mode))


async def do_rate(target: Message, user, raw: str):
    u = scoring.normalize(raw)
    if not scoring.is_valid(u):
        return await target.answer("Ник: 4–32 символа, a–z, 0–9, _, начинается с буквы, без «__» и «_» в конце.")
    await db.touch(user.id, user.full_name, searched=True)
    sc, parts = scoring.score(u)
    st = await checker.check(u)
    detail = "\n".join(f"{'+' if p >= 0 else '−'}{abs(p)} {label}" for label, p in parts if p)
    cap = f"<b>@{u}</b> — {sc}/100 {scoring.tier(sc)}\n{STATUS[st]}\n\n{detail}"
    await send_card(target, u, sc, st, cap, nick_kb(u, user.id))


@dp.callback_query(F.data.startswith("r:"))
async def cb_rate(c: CallbackQuery):
    await c.answer()
    await do_rate(c.message, c.from_user, c.data[2:])


@dp.callback_query(F.data.startswith("m:"))
async def cb_menu(c: CallbackQuery):
    act = c.data[2:]
    if act == "home":
        await show(c, HOME, home_kb())
    elif act == "find":
        await show(c, "Что ищем? Выбери режим:", mode_kb())
    elif act == "rate":
        await show(c, "Пришли ник сообщением, например <code>example</code> — оценю и нарисую карточку.",
                   InlineKeyboardMarkup(inline_keyboard=[HOME_BTN]))
    elif act == "help":
        await show(c, HELP, InlineKeyboardMarkup(inline_keyboard=[HOME_BTN]))
    elif act == "cab":
        text, kbd = await cab_view(c.from_user)
        await show(c, text, kbd)


@dp.message(Command("rate"))
async def rate(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("Использование: /rate name")
    await do_rate(m, m.from_user, command.args.split()[0])


@dp.message(Command("trap"))
async def trap(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("Использование: /trap name")
    await m.answer(await add_trap(m.from_user.id, scoring.normalize(command.args.split()[0])))


@dp.message(Command("set"))
async def set_cmd(m: Message, command: CommandObject):
    if m.from_user.id != OWNER_ID:
        return await m.answer("Команда только для владельца бота.")
    u = scoring.normalize((command.args or "").split()[0] if command.args else "")
    if not scoring.is_valid(u):
        return await m.answer("Использование: /set name")
    await m.answer(f"Поставить <b>@{u}</b> на твой аккаунт?\nСтарый ник освободится — его могут занять.", reply_markup=confirm_kb(u))


def confirm_kb(u):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Да, @{u}", callback_data=f"setok:{u}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="setno"),
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


@dp.message(Command("untrap"))
async def untrap(m: Message, command: CommandObject):
    u = scoring.normalize(command.args or "")
    await db.run("DELETE FROM traps WHERE user_id=? AND username=?", (m.from_user.id, u))
    await m.answer(f"Ловушка на @{u} снята.")


@dp.message(Command("unfav"))
async def unfav(m: Message, command: CommandObject):
    u = scoring.normalize(command.args or "")
    await db.run("DELETE FROM favs WHERE user_id=? AND username=?", (m.from_user.id, u))
    await m.answer(f"@{u} убран из избранного.")


async def cab_view(user):
    uid = user.id
    await db.touch(uid, user.full_name)
    _, name, joined, searches = (await db.run("SELECT * FROM users WHERE id=?", (uid,), fetch=True))[0]
    favs = await db.run("SELECT username, score FROM favs WHERE user_id=? ORDER BY score DESC", (uid,), fetch=True)
    traps = await db.run("SELECT username, state FROM traps WHERE user_id=?", (uid,), fetch=True)
    text = (
        f"<b>👤 {html.escape(name)}</b>\nС нами с {time.strftime('%d.%m.%Y', time.gmtime(joined))} · поисков: {searches}\n\n"
        f"<b>⭐ Избранное ({len(favs)})</b>\n" + ("\n".join(f"@{u} — {s}/100" for u, s in favs) or "пусто") +
        f"\n\n<b>🪤 Ловушки ({len(traps)}/{MAX_TRAPS})</b>\n" + ("\n".join(f"@{u} — {STATUS.get(s, s)}" for u, s in traps) or "пусто")
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


@dp.message(F.text.regexp(r"^@?[A-Za-z0-9_]{4,32}$"))
async def plain_username(m: Message):
    await do_rate(m, m.from_user, m.text)


async def trap_loop():
    while True:
        await asyncio.sleep(TRAP_INTERVAL)
        try:
            for (u,) in await db.run("SELECT DISTINCT username FROM traps", fetch=True):
                st = await checker.check(u)
                if st not in ("free", "fragment"):
                    continue
                for uid, old in await db.run("SELECT user_id, state FROM traps WHERE username=?", (u,), fetch=True):
                    if old == st:
                        continue
                    where = "свободен — успей занять!" if st == "free" else "появился на Fragment"
                    try:
                        await bot.send_message(uid, f"🪤 <b>@{u}</b> {where}")
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
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="find", description="Найти красивый ник"),
        BotCommand(command="cabinet", description="Кабинет"),
        BotCommand(command="help", description="Помощь"),
    ])
    await checker.start()
    await start_web()
    asyncio.create_task(trap_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
