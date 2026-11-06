"""База: Postgres (Supabase) если задан DATABASE_URL, иначе SQLite-файл."""
import logging
import os
import re
import time

import aiosqlite

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB = os.getenv("DB_PATH", "data.db")
PG = bool(DATABASE_URL)
BACKEND = "Supabase (Postgres)" if PG else "SQLite (файл)"
_pool = None

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users(id BIGINT PRIMARY KEY, name TEXT, joined BIGINT, searches INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS favs(user_id BIGINT, username TEXT, score INTEGER, PRIMARY KEY(user_id, username))",
    "CREATE TABLE IF NOT EXISTS traps(user_id BIGINT, username TEXT, state TEXT, PRIMARY KEY(user_id, username))",
    # пул свободных ников, который наполняют фоновые сканеры
    "CREATE TABLE IF NOT EXISTS pool(username TEXT PRIMARY KEY, length INTEGER, score INTEGER, found BIGINT)",
    "CREATE INDEX IF NOT EXISTS pool_len ON pool(length, score)",
    # паузы аккаунтов после FloodWait — переживают перезапуск сервиса
    "CREATE TABLE IF NOT EXISTS cooldowns(name TEXT PRIMARY KEY, until BIGINT)",
    # ники, которые сканеры уже проверяли: после перезапуска не начинаем проход заново
    "CREATE TABLE IF NOT EXISTS checked(username TEXT PRIMARY KEY, ts BIGINT)",
    "CREATE INDEX IF NOT EXISTS checked_ts ON checked(ts)",
]


def _pg(sql: str) -> str:
    n = 0

    def repl(_):
        nonlocal n
        n += 1
        return f"${n}"

    return re.sub(r"\?", repl, sql)


async def _get_pool():
    global _pool
    if _pool is None:
        import asyncpg

        _pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=5, ssl="require",
            statement_cache_size=0, command_timeout=30,  # statement_cache_size=0 — совместимость с пулерами Supabase
        )
    return _pool


async def init():
    global PG, BACKEND
    if PG:
        try:
            pool = await _get_pool()
            async with pool.acquire() as con:
                for stmt in SCHEMA:
                    await con.execute(stmt)
            return
        except Exception:
            logging.exception("Supabase недоступен — перехожу на локальный SQLite (данные не будут сохраняться)")
            PG, BACKEND = False, "SQLite (запасной: Supabase не подключился)"
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    async with aiosqlite.connect(DB) as db:
        for stmt in SCHEMA:
            await db.execute(stmt)
        await db.commit()


async def run(sql, args=(), fetch=False):
    if PG:
        pool = await _get_pool()
        async with pool.acquire() as con:
            if fetch:
                return [tuple(r) for r in await con.fetch(_pg(sql), *args)]
            await con.execute(_pg(sql), *args)
            return None
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(sql, args)
        rows = await cur.fetchall() if fetch else None
        await db.commit()
        return rows


async def touch(uid, name, searched=False):
    await run("INSERT INTO users(id,name,joined) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING",
              (uid, name, int(time.time())))
    if searched:
        await run("UPDATE users SET searches=searches+1 WHERE id=?", (uid,))


async def get_cooldown(name: str) -> float:
    try:
        rows = await run("SELECT until FROM cooldowns WHERE name=?", (name,), fetch=True)
        return float(rows[0][0]) if rows else 0.0
    except Exception:
        logging.exception("get_cooldown")
        return 0.0


async def set_cooldown(name: str, until: float):
    try:
        await run("INSERT INTO cooldowns(name,until) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET until=excluded.until",
                  (name, int(until)))
    except Exception:
        logging.exception("set_cooldown")


async def load_checked(ttl: int) -> dict:
    """Ники, проверенные сканерами за последние ttl секунд. Старые записи заодно удаляются."""
    try:
        await run("DELETE FROM checked WHERE ts < ?", (int(time.time()) - ttl,))
        rows = await run("SELECT username, ts FROM checked", fetch=True)
        return {u: float(t) for u, t in rows}
    except Exception:
        logging.exception("load_checked")
        return {}


async def mark_checked(u: str):
    try:
        await run("INSERT INTO checked(username,ts) VALUES(?,?) ON CONFLICT(username) DO UPDATE SET ts=excluded.ts",
                  (u, int(time.time())))
    except Exception:
        logging.exception("mark_checked")
