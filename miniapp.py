"""BulkaSearch Mini App: сервер (маршруты aiohttp) + проверка подписи Telegram.
Сама страница лежит отдельно на Vercel (index.html). Всю «умную» работу (проверка ников, оценка, избранное, ловушки)
берём из bot.py, ничего не дублируя: B — это модуль bot.py, передаётся из start_web()."""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qsl

from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiohttp import web

import db
import lookup
import scanner
import scoring

WEBAPP_URL = os.getenv("WEBAPP_URL", "https://bulkasearch.vercel.app")
_last: dict = {}  # (user_id, вид) -> время последнего запроса
_busy: set = set()  # кто сейчас ищет


def _user(request, token):
    """Проверяет подпись initData от Telegram. Возвращает словарь пользователя или None."""
    try:
        pairs = dict(parse_qsl(request.headers.get("X-Init-Data", ""), keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        mine = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not got or not hmac.compare_digest(mine, got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def _wait(uid, kind, gap):
    """Сколько секунд ещё ждать до следующего запроса этого вида (0 — можно)."""
    left = gap - (time.time() - _last.get((uid, kind), 0))
    if left > 0:
        return int(left) + 1
    _last[(uid, kind)] = time.time()
    return 0


ORIGIN = os.getenv("WEBAPP_ORIGIN", "https://bulkasearch.vercel.app")  # откуда разрешены запросы (сайт на Vercel)


@web.middleware
async def cors(request, handler):
    """Разрешает странице с Vercel ходить в API на Render (защита всё равно в подписи Telegram)."""
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
    resp.headers["Access-Control-Allow-Origin"] = ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


async def setup(app, B, token):
    app.middlewares.append(cors)

    def api(fn):
        async def handler(request):
            u = _user(request, token)
            if not u:
                return web.json_response({"error": "auth"}, status=401)
            try:
                body = await request.json() if request.can_read_body else {}
            except Exception:
                body = {}
            try:
                return web.json_response(await fn(u, body or {}))
            except Exception:
                logging.exception("miniapp %s", fn.__name__)
                return web.json_response({"error": "server"})
        return handler

    async def exists(table, uid, nick):
        return bool(await db.run(f"SELECT 1 FROM {table} WHERE user_id=? AND username=?", (uid, nick), fetch=True))

    async def dossier(uid, nick, fresh=False):
        sc, parts = scoring.score(nick)
        pst, info = await lookup.profile(nick, B.all_checkers(), fresh=fresh)
        if pst == "found":
            st = "taken"
            B.checker._mark_taken(nick)
        elif pst in ("notfound", "unknown"):
            st = await B.smart_check(nick, fresh=True)
        else:
            st = pst
        if st in ("flood", "offline", "invalid"):
            return {"error": st, "left": B.cooling_left()}
        frag = await lookup.fragment(nick, fresh) if st in ("taken", "fragment") else None
        lo, hi = scoring.estimate_ton(nick, sc)
        info = dict(info) if info else None
        if info:
            photo = info.pop("photo", None)
            if photo and len(photo) < 300_000:
                info["photo"] = "data:image/jpeg;base64," + base64.b64encode(photo).decode()
        return {"nick": nick, "status": st, "score": sc, "tier": scoring.tier(sc), "lo": lo, "hi": hi,
                "parts": [[a, b] for a, b in parts if b], "info": info, "frag": frag,
                "fav": await exists("favs", uid, nick), "trap": await exists("traps", uid, nick)}

    @api
    async def h_lookup(u, body):
        nick = scoring.normalize(str(body.get("nick", "")))
        if not scoring.is_valid(nick):
            return {"error": "bad_nick"}
        if (w := _wait(u["id"], "lookup", B.LOOKUP_GAP)):
            return {"error": "wait", "left": w}
        return await dossier(u["id"], nick, bool(body.get("fresh")))

    @api
    async def h_me(u, body):
        uid = u["id"]
        await db.touch(uid, u.get("first_name", ""))
        favs = await db.run("SELECT username, score FROM favs WHERE user_id=? ORDER BY score DESC", (uid,), fetch=True)
        traps = await db.run("SELECT username, state FROM traps WHERE user_id=?", (uid,), fetch=True)
        return {"name": u.get("first_name", ""), "max_traps": B.MAX_TRAPS,
                "favs": [{"nick": a, "score": b} for a, b in favs],
                "traps": [{"nick": a, "state": b} for a, b in traps]}

    @api
    async def h_fav(u, body):
        uid, nick = u["id"], scoring.normalize(str(body.get("nick", "")))
        if not scoring.is_valid(nick):
            return {"error": "bad_nick"}
        if body.get("on"):
            msg = await B.add_fav(uid, nick)
        else:
            await db.run("DELETE FROM favs WHERE user_id=? AND username=?", (uid, nick))
            msg = "Убрано из избранного"
        return {"msg": msg, "on": await exists("favs", uid, nick)}

    @api
    async def h_trap(u, body):
        uid, nick = u["id"], scoring.normalize(str(body.get("nick", "")))
        if not scoring.is_valid(nick):
            return {"error": "bad_nick"}
        if body.get("on"):
            msg = await B.add_trap(uid, nick)
        else:
            await db.run("DELETE FROM traps WHERE user_id=? AND username=?", (uid, nick))
            msg = "Ловушка снята"
        return {"msg": msg, "on": await exists("traps", uid, nick)}

    @api
    async def h_find(u, body):
        uid = u["id"]
        quick = body.get("length") == "quick"  # быстрый поиск как в боте: 6–7 символов, звучные, склад сканеров
        mode = "pron" if quick else (body.get("mode") if body.get("mode") in scoring.MODES else "pron")
        lengths = B.parse_len(str(body.get("length", "5")))
        if not lengths:
            return {"error": "bad_len"}
        if uid in _busy:
            return {"error": "busy"}
        if (left := B.cooling_left()):
            return {"error": "flood", "left": left}
        if (w := _wait(uid, "find", B.FIND_GAP)):
            return {"error": "wait", "left": w}
        _busy.add(uid)
        try:
            await db.touch(uid, u.get("first_name", ""), searched=True)
            if mode == "pron":  # сначала готовые находки сканеров, это мгновенно
                got = await B.take_from_pool(scanner.POOL_LENGTHS if quick else lengths)
                if got:
                    return {"nick": got[1], "checked": 0}
            checked = 0
            for c in B.build_cands(lengths, mode, 30 if quick else 20, by_score=not quick):
                st = await B.smart_check(c)
                checked += 1
                if st in ("flood", "offline"):
                    return {"error": st, "left": B.cooling_left()}
                if st == "free":
                    return {"nick": c, "checked": checked}
            return {"nick": None, "checked": checked}
        finally:
            _busy.discard(uid)

    for path, h in (("lookup", h_lookup), ("me", h_me), ("fav", h_fav), ("trap", h_trap), ("find", h_find)):
        app.router.add_post(f"/api/{path}", h)
    try:  # кнопка-меню рядом с полем ввода открывает приложение
        await B.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🥐 Приложение", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception:
        logging.exception("miniapp: menu button")
