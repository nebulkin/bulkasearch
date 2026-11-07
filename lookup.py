"""Досье на ник (/find news): что за аккаунт/канал/бот сидит на @нике и что с ним на Fragment.
Только публичные данные — то же, что любой человек видит на t.me/ник. Бот-токен это не умеет,
поэтому спрашиваем через userbot-аккаунты (основной и сканеры), как и проверку свободы."""
import asyncio
import logging
import random
import re
import time

import aiohttp
from telethon import errors, functions, types

import db
from checker import LINK_ERRORS, REQ_TIMEOUT, ensure_connected

CACHE_TTL = 600  # сек: одно и то же досье не гоняем в Telegram чаще раза в 10 минут (кнопка «Обновить» обходит кеш)
_cache: dict[str, tuple[float, tuple]] = {}
_frag_cache: dict[str, tuple[float, dict | None]] = {}
FRAG_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _pick(checkers, tried):
    """Самый отдохнувший аккаунт, у которого есть связь и нет паузы."""
    ok = [c for c in checkers
          if id(c) not in tried and c.client is not None and c.client.is_connected()
          and not c.cooling() and time.time() >= getattr(c, "offline_until", 0)]
    return min(ok, key=lambda c: c.last_used, default=None)


async def _flood(c, seconds: int):
    """FloodWait: аккаунт отдыхает с запасом (так же, как в checker/scanner), пауза сохраняется в БД."""
    c.cooldown_until = time.time() + seconds + 300
    await db.set_cooldown(c.key, c.cooldown_until)


async def _extra(c, coro):
    """Дополнительный запрос (полный профиль, фото): не вышло — не страшно, остаёмся с тем, что уже есть."""
    try:
        return await asyncio.wait_for(coro, REQ_TIMEOUT)
    except errors.FloodWaitError as e:
        await _flood(c, e.seconds)
    except (errors.RPCError, *LINK_ERRORS):
        pass
    except Exception:
        logging.exception("lookup extra")
    return None


async def _resolve(c, u: str):
    """None — такого ника нет в Telegram; иначе словарь с данными владельца."""
    cl = c.client
    if not await ensure_connected(cl, 25):
        raise ConnectionError("offline")
    try:
        r = await asyncio.wait_for(cl(functions.contacts.ResolveUsernameRequest(username=u)), REQ_TIMEOUT)
    except errors.UsernameNotOccupiedError:
        return None
    ent, peer = None, r.peer
    if isinstance(peer, types.PeerUser):
        ent = next((x for x in r.users if x.id == peer.user_id), None)
    elif isinstance(peer, types.PeerChannel):
        ent = next((x for x in r.chats if x.id == peer.channel_id), None)
    elif isinstance(peer, types.PeerChat):
        ent = next((x for x in r.chats if x.id == peer.chat_id), None)
    info = {"kind": "unknown", "name": "", "bio": "", "members": None, "photo": None,
            "verified": bool(getattr(ent, "verified", False)), "scam": bool(getattr(ent, "scam", False)),
            "fake": bool(getattr(ent, "fake", False)), "premium": bool(getattr(ent, "premium", False)),
            "restricted": bool(getattr(ent, "restricted", False))}
    if ent is None:  # ник занят, но сам объект Telegram не отдал
        return info
    if isinstance(ent, types.User):
        info["kind"] = "bot" if ent.bot else "user"
        info["name"] = " ".join(x for x in (ent.first_name, ent.last_name) if x) or "Без имени"
        if ent.bot:
            info["members"] = getattr(ent, "bot_active_users", None)
        full = await _extra(c, cl(functions.users.GetFullUserRequest(ent)))
        if full is not None:
            fu = full.full_user
            info["bio"] = (getattr(fu, "about", "") or "").strip()
            bi = getattr(fu, "bot_info", None)
            if not info["bio"] and bi is not None:
                info["bio"] = (getattr(bi, "description", "") or "").strip()
    else:
        info["kind"] = "channel" if getattr(ent, "broadcast", False) else "group"
        info["name"] = getattr(ent, "title", "") or "Без названия"
        info["members"] = getattr(ent, "participants_count", None)
        if isinstance(ent, types.Channel):
            full = await _extra(c, cl(functions.channels.GetFullChannelRequest(ent)))
            if full is not None:
                fc = full.full_chat
                info["bio"] = (getattr(fc, "about", "") or "").strip()
                info["members"] = getattr(fc, "participants_count", None) or info["members"]
    info["photo"] = await _extra(c, cl.download_profile_photo(ent, file=bytes)) or None
    return info


async def profile(u: str, checkers, fresh: bool = False):
    """-> (статус, данные): found (данные владельца) · notfound · flood · offline · invalid · unknown."""
    if not fresh:
        hit = _cache.get(u)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    tried, link_lost, flooded = set(), False, False
    while True:
        c = _pick(checkers, tried)
        if c is None:
            if link_lost or not any(x.client is not None and x.client.is_connected() for x in checkers):
                return "offline", None
            return ("flood" if flooded or any(x.cooling() for x in checkers) else "unknown"), None
        tried.add(id(c))
        try:
            async with c.lock:  # один запрос за раз на аккаунт — как у проверок свободы
                info = await _resolve(c, u)
                c.last_used = time.time()
                await asyncio.sleep(random.uniform(0.8, 1.4))
        except errors.FloodWaitError as e:
            logging.warning("lookup %s: FloodWait %s сек на «%s»", u, e.seconds, c.name)
            await _flood(c, e.seconds)
            flooded = True
            continue
        except errors.UsernameInvalidError:
            return "invalid", None
        except LINK_ERRORS:
            link_lost = True
            continue
        except errors.RPCError as e:
            logging.warning("lookup %s: %s", u, type(e).__name__)
            continue
        out = ("notfound", None) if info is None else ("found", info)
        if len(_cache) > 2000:
            _cache.clear()
        _cache[u] = (time.time(), out)
        return out


async def fragment(u: str, fresh: bool = False):
    """Статус ника на Fragment: {'state': avail|auction|sold|none, 'price': '1,234'|None} или None, если не вышло.
    Разбор страницы fragment.com — «по возможности»: если Fragment сменит вёрстку или не ответит, досье просто придёт без этой строки."""
    hit = _frag_cache.get(u)
    if hit and not fresh and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    res = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=7),
                                         headers={"User-Agent": FRAG_UA, "Accept-Language": "en"}) as s:
            async with s.get(f"https://fragment.com/username/{u}") as r:
                if r.status == 200:
                    res = _parse_fragment(await r.text())
    except Exception as e:
        logging.info("fragment %s: %s", u, type(e).__name__)
    if len(_frag_cache) > 2000:
        _frag_cache.clear()
    _frag_cache[u] = (time.time(), res)
    return res


def _parse_fragment(page: str):
    m = re.search(r'tm-section-header-status[^"]*"[^>]*>\s*([^<]+?)\s*<', page)
    if not m:
        return None
    t = m.group(1).lower()
    state = ("none" if "unavailable" in t or "taken" in t else "avail" if "available" in t
             else "auction" if "auction" in t or "bid" in t else "sold" if "sold" in t else "none")
    pm = re.search(r'icon-ton[^>]*>\s*([\d][\d,\.]*)\s*<', page)
    return {"state": state, "price": pm.group(1) if pm else None}
