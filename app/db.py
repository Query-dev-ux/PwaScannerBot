from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,
  user_id INTEGER,
  chat_id INTEGER,
  proxy TEXT,
  site_url TEXT,
  pwa_name TEXT,
  start_url TEXT,
  scope TEXT,
  deep_link TEXT,
  stage TEXT,
  push_subscribed INTEGER DEFAULT 0,
  push_endpoint TEXT,
  profile_dir TEXT,
  status TEXT,
  created_at REAL,
  expires_at REAL,
  delivered_at REAL
);
CREATE TABLE IF NOT EXISTS pushes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  ts REAL,
  stage TEXT,
  service TEXT,
  event TEXT,
  title TEXT,
  body TEXT,
  icon TEXT,
  url TEXT,
  raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_pushes_session ON pushes(session_id);
CREATE TABLE IF NOT EXISTS authorized(
  user_id INTEGER PRIMARY KEY,
  granted_at REAL
);
"""

ACTIVE_STATUSES = ("inspected", "collecting")


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            # lightweight migrations for pre-existing DBs
            cur = await db.execute("PRAGMA table_info(sessions)")
            scols = {r[1] for r in await cur.fetchall()}
            if "deep_link" not in scols:
                await db.execute("ALTER TABLE sessions ADD COLUMN deep_link TEXT")
            if "stage" not in scols:
                await db.execute("ALTER TABLE sessions ADD COLUMN stage TEXT")
            if "push_subscribed" not in scols:
                await db.execute(
                    "ALTER TABLE sessions ADD COLUMN push_subscribed INTEGER DEFAULT 0"
                )
            if "push_endpoint" not in scols:
                await db.execute("ALTER TABLE sessions ADD COLUMN push_endpoint TEXT")
            cur = await db.execute("PRAGMA table_info(pushes)")
            pcols = {r[1] for r in await cur.fetchall()}
            if "stage" not in pcols:
                await db.execute("ALTER TABLE pushes ADD COLUMN stage TEXT")
            # one-off cleanup: earlier builds stored every push-lifecycle event
            # as its own row (received / dispatched / displayed / completed)
            await db.execute(
                "DELETE FROM pushes WHERE event IN "
                "('Push event dispatched','Push event completed')"
            )
            await db.execute(
                "DELETE FROM pushes WHERE event='Notification displayed' "
                "AND EXISTS (SELECT 1 FROM pushes p2 "
                "WHERE p2.session_id=pushes.session_id "
                "AND p2.event='Push message received' "
                "AND ABS(p2.ts - pushes.ts) < 12)"
            )
            await db.commit()

    @asynccontextmanager
    async def _conn(self):
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()

    # ---------- sessions ----------
    async def create_session(self, d: dict[str, Any]) -> None:
        async with self._conn() as db:
            await db.execute(
                """INSERT INTO sessions
                (id,user_id,chat_id,proxy,site_url,pwa_name,start_url,scope,deep_link,stage,
                 push_subscribed,push_endpoint,
                 profile_dir,status,created_at,expires_at,delivered_at)
                VALUES (:id,:user_id,:chat_id,:proxy,:site_url,:pwa_name,:start_url,:scope,:deep_link,:stage,
                        :push_subscribed,:push_endpoint,
                        :profile_dir,:status,:created_at,:expires_at,:delivered_at)""",
                {"deep_link": None, "stage": None, "push_subscribed": 0,
                 "push_endpoint": None, **d},
            )
            await db.commit()

    async def get_session(self, session_id: str):
        async with self._conn() as db:
            cur = await db.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
            return await cur.fetchone()

    async def set_session_fields(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        async with self._conn() as db:
            await db.execute(
                f"UPDATE sessions SET {cols} WHERE id=?",
                (*fields.values(), session_id),
            )
            await db.commit()

    async def list_collecting(self):
        async with self._conn() as db:
            cur = await db.execute("SELECT * FROM sessions WHERE status='collecting'")
            return await cur.fetchall()

    async def list_due(self, now: float):
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT * FROM sessions WHERE status='collecting' "
                "AND expires_at IS NOT NULL AND expires_at<=?",
                (now,),
            )
            return await cur.fetchall()

    async def count_active(self) -> int:
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        async with self._conn() as db:
            cur = await db.execute(
                f"SELECT COUNT(*) FROM sessions WHERE status IN ({placeholders})",
                ACTIVE_STATUSES,
            )
            (n,) = await cur.fetchone()
            return int(n)

    async def sessions_for_user(self, user_id: int):
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT * FROM sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
                (user_id,),
            )
            return await cur.fetchall()

    # ---------- access ----------
    async def is_authorized(self, user_id: int) -> bool:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT 1 FROM authorized WHERE user_id=?", (user_id,)
            )
            return await cur.fetchone() is not None

    async def authorize(self, user_id: int) -> None:
        import time

        async with self._conn() as db:
            await db.execute(
                "INSERT OR REPLACE INTO authorized(user_id,granted_at) VALUES(?,?)",
                (user_id, time.time()),
            )
            await db.commit()

    async def deauthorize(self, user_id: int) -> None:
        async with self._conn() as db:
            await db.execute("DELETE FROM authorized WHERE user_id=?", (user_id,))
            await db.commit()

    # ---------- pushes ----------
    async def add_push(self, session_id: str, rec: dict[str, Any]) -> bool:
        """Insert a push. Collapses the low-level `pushMessaging` event and the
        `notifications` event for the same push (±1s) into one row, keeping the
        one that actually has a title/body. Returns True if a row was written."""
        title, body = rec.get("title"), rec.get("body")
        has_content = bool((title or "").strip() or (body or "").strip())
        ts = float(rec["ts"])
        async with self._conn() as db:
            cur = await db.execute(
                """SELECT id, title, body FROM pushes
                   WHERE session_id=? AND ABS(ts - ?) <= 1
                   AND IFNULL(service,'') <> 'stage' ORDER BY id""",
                (session_id, ts),
            )
            for r in await cur.fetchall():
                r_has = bool((r["title"] or "").strip() or (r["body"] or "").strip())
                same = (r["title"] or "") == (title or "") and (r["body"] or "") == (body or "")
                if same:
                    return False
                if r_has and not has_content:
                    return False  # keep the richer existing row
                if has_content and not r_has:
                    await db.execute(
                        """UPDATE pushes SET ts=?,stage=?,service=?,event=?,
                           title=?,body=?,icon=?,url=?,raw=? WHERE id=?""",
                        (rec["ts"], rec.get("stage"), rec.get("service"),
                         rec.get("event"), title, body, rec.get("icon"),
                         rec.get("url"), rec.get("raw"), r["id"]),
                    )
                    await db.commit()
                    return True
            await db.execute(
                """INSERT INTO pushes(session_id,ts,stage,service,event,title,body,icon,url,raw)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id,
                    rec["ts"],
                    rec.get("stage"),
                    rec.get("service"),
                    rec.get("event"),
                    rec.get("title"),
                    rec.get("body"),
                    rec.get("icon"),
                    rec.get("url"),
                    rec.get("raw"),
                ),
            )
            await db.commit()
            return True

    async def list_pushes(self, session_id: str):
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT * FROM pushes WHERE session_id=? ORDER BY ts ASC", (session_id,)
            )
            return await cur.fetchall()

    async def count_pushes(self, session_id: str) -> int:
        async with self._conn() as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM pushes WHERE session_id=? "
                "AND IFNULL(service,'')<>'stage'",
                (session_id,),
            )
            (n,) = await cur.fetchone()
            return int(n)
