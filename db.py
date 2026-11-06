import os
import time

import aiosqlite

DB = os.getenv("DB_PATH", "data.db")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, joined INTEGER, searches INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS favs(user_id INTEGER, username TEXT, score INTEGER, PRIMARY KEY(user_id, username));
CREATE TABLE IF NOT EXISTS traps(user_id INTEGER, username TEXT, state TEXT, PRIMARY KEY(user_id, username));
"""


async def init():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    async with aiosqlite.connect(DB) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def run(sql, args=(), fetch=False):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(sql, args)
        rows = await cur.fetchall() if fetch else None
        await db.commit()
        return rows


async def touch(uid, name, searched=False):
    await run("INSERT OR IGNORE INTO users(id,name,joined) VALUES(?,?,?)", (uid, name, int(time.time())))
    if searched:
        await run("UPDATE users SET searches=searches+1 WHERE id=?", (uid,))
