"""Storage for the central platform: PostgreSQL (+TimescaleDB) in production, SQLite for dev/tests/demo.

Both use the same tables (sql/postgres.sql, sql/sqlite.sql) and the same statements below. Statements
are written with '?' placeholders and '{J}' for JSON values; they are adapted per dialect.
All database work runs on ONE worker thread (SQLite needs it; it also keeps PostgreSQL writes ordered).
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

SQL_DIR = pathlib.Path(__file__).with_name("sql")
SCHEMA_VERSION = 2


def iso_to_ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, url: str):
        """url: 'sqlite:///path/to.db', 'sqlite://:memory:' or a PostgreSQL DSN 'postgresql://user@host/db'."""
        self.url = url
        self.dialect = "postgres" if url.startswith(("postgres://", "postgresql://")) else "sqlite"
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")
        self._conn = None
        self._pool.submit(self._open).result()

    # ------------------------------------------------------------ plumbing
    def _open(self) -> None:
        if self.dialect == "postgres":
            import psycopg  # psycopg 3 (LGPL-3.0), production only
            self._conn = psycopg.connect(self.url)
            with self._conn.cursor() as cur:
                cur.execute((SQL_DIR / "postgres.sql").read_text())
            self._conn.commit()
        else:
            path = self.url.removeprefix("sqlite://").removeprefix("/") if ":memory:" in self.url \
                else self.url.removeprefix("sqlite:///")
            self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False)
            self._conn.executescript((SQL_DIR / "sqlite.sql").read_text())
        rows = self._q("SELECT version FROM schema_version")
        if not rows:
            self._x("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

    def _sql(self, sql: str) -> str:
        if self.dialect == "postgres":
            return sql.replace("{J}", "%s::jsonb").replace("?", "%s")
        return sql.replace("{J}", "?")

    def _x(self, sql: str, params: tuple = (), many: list | None = None, commit: bool = True) -> int:
        cur = self._conn.cursor()
        if many is not None:
            cur.executemany(self._sql(sql), many)
        else:
            cur.execute(self._sql(sql), params)
        n = cur.rowcount
        if commit:
            self._conn.commit()
        return n

    def _q(self, sql: str, params: tuple = ()) -> list[tuple]:
        cur = self._conn.cursor()
        cur.execute(self._sql(sql), params)
        rows = cur.fetchall()
        if self.dialect == "postgres" and self._conn.info.transaction_status == 2:  # INTRANS: end the read
            self._conn.commit()
        return rows

    async def run(self, fn, *args):
        """Run a store method on the database thread from async code."""
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def close(self) -> None:
        self._pool.submit(self._conn.close).result()
        self._pool.shutdown()

    @staticmethod
    def _json(v: Any) -> Any:
        return v if isinstance(v, (dict, list)) or v is None else json.loads(v)

    # ------------------------------------------------------------ configuration
    def save_source(self, cfg: dict, user: str) -> None:
        self._x("INSERT INTO sites (site_id, name) VALUES (?, ?) ON CONFLICT (site_id) DO NOTHING",
                (cfg["site"], cfg["site"]), commit=False)
        self._x("""INSERT INTO sources (source_id, site_id, type, name, enabled, revision, config, updated_ms, updated_by)
                   VALUES (?, ?, ?, ?, ?, ?, {J}, ?, ?)
                   ON CONFLICT (source_id) DO UPDATE SET site_id = excluded.site_id, type = excluded.type,
                     name = excluded.name, enabled = excluded.enabled, revision = excluded.revision,
                     config = excluded.config, updated_ms = excluded.updated_ms, updated_by = excluded.updated_by""",
                (cfg["source_id"], cfg["site"], cfg["type"], cfg["name"], bool(cfg["enabled"]), cfg["revision"],
                 json.dumps(cfg), now_ms(), user), commit=False)
        self._x("DELETE FROM tags WHERE source_id = ?", (cfg["source_id"],), commit=False)
        self._x("""INSERT INTO tags (tag_id, source_id, site_id, data_type, unit, name) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (tag_id) DO UPDATE SET source_id = excluded.source_id, site_id = excluded.site_id,
                     data_type = excluded.data_type, unit = excluded.unit, name = excluded.name""",
                many=[(p["tag_id"], cfg["source_id"], cfg["site"], p["data_type"], p.get("unit"), p.get("name"))
                      for p in cfg["points"] if p.get("enabled", True)], commit=False)
        self._conn.commit()

    def delete_source(self, source_id: str) -> int:
        n = self._x("DELETE FROM sources WHERE source_id = ?", (source_id,), commit=False)
        self._x("DELETE FROM tags WHERE source_id = ?", (source_id,), commit=False)
        self._x("DELETE FROM source_status WHERE source_id = ?", (source_id,), commit=False)
        self._conn.commit()
        return n

    def get_source(self, source_id: str) -> dict | None:
        rows = self._q("SELECT config FROM sources WHERE source_id = ?", (source_id,))
        return self._json(rows[0][0]) if rows else None

    def list_sources(self) -> list[dict]:
        rows = self._q("""SELECT s.source_id, s.site_id, s.type, s.name, s.enabled, s.revision, s.updated_ms,
                                 s.updated_by, st.state, st.body
                          FROM sources s LEFT JOIN source_status st ON st.source_id = s.source_id
                          ORDER BY s.site_id, s.source_id""")
        out = []
        for sid, site, typ, name, enabled, rev, upd, by, state, body in rows:
            status = self._json(body) if body else None
            out.append({"source_id": sid, "site": site, "type": typ, "name": name, "enabled": bool(enabled),
                        "revision": rev, "updated": ms_to_iso(upd), "updated_by": by,
                        "state": state or "unknown", "status": status})
        return out

    # ------------------------------------------------------------ live data
    def save_status(self, status: dict) -> None:
        ts = iso_to_ms(status["ts"]) if status.get("ts") else now_ms()
        self._x("""INSERT INTO source_status (source_id, state, ts_ms, body) VALUES (?, ?, ?, {J})
                   ON CONFLICT (source_id) DO UPDATE SET state = excluded.state, ts_ms = excluded.ts_ms,
                     body = excluded.body""", (status["src"], status["state"], ts, json.dumps(status)))

    def write_values(self, values: list[dict]) -> None:
        rows, latest = [], []
        for v in values:
            ts, rx, val = iso_to_ms(v["ts"]), iso_to_ms(v["rx"]), v["v"]
            num = float(val) if isinstance(val, (int, float)) else None   # bool is an int: 1.0 / 0.0
            text = val if isinstance(val, str) else None
            rows.append((v["t"], ts, num, text, v["q"]))
            latest.append((v["t"], json.dumps(val), v["q"], ts, rx, json.dumps(v["f"]) if v.get("f") else None))
        self._x("""INSERT INTO readings (tag_id, ts_ms, v_num, v_text, q) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (tag_id, ts_ms) DO NOTHING""", many=rows, commit=False)
        self._x("""INSERT INTO latest (tag_id, v, q, ts_ms, rx_ms, flags) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (tag_id) DO UPDATE SET v = excluded.v, q = excluded.q, ts_ms = excluded.ts_ms,
                     rx_ms = excluded.rx_ms, flags = excluded.flags
                   WHERE excluded.ts_ms >= latest.ts_ms""", many=latest, commit=False)
        self._conn.commit()

    def check_seq(self, src: str, boot: str, seq: int) -> int:
        """Record (boot, seq) for a source; return the number of batches missing before this one."""
        rows = self._q("SELECT boot, seq FROM ingest_seq WHERE src = ?", (src,))
        missing = 0
        if rows and rows[0][0] == boot and seq > rows[0][1] + 1:
            missing = seq - rows[0][1] - 1
        elif not rows and seq > 1:
            missing = 0          # first batch seen after a platform restart: nothing to compare with
        self._x("""INSERT INTO ingest_seq (src, boot, seq, gaps) VALUES (?, ?, ?, ?)
                   ON CONFLICT (src) DO UPDATE SET boot = excluded.boot, seq = excluded.seq,
                     gaps = ingest_seq.gaps + excluded.gaps""", (src, boot, seq, missing))
        return missing

    def save_event(self, ev: dict) -> None:
        self._x("""INSERT INTO events (event_id, src, type, severity, ts_ms, message, body)
                   VALUES (?, ?, ?, ?, ?, ?, {J}) ON CONFLICT (event_id) DO NOTHING""",
                (ev["id"], ev["src"], ev["type"], ev["severity"], iso_to_ms(ev["ts"]), ev["message"], json.dumps(ev)))

    # ------------------------------------------------------------ queries
    def list_tags(self, source_id: str | None = None, search: str | None = None, limit: int = 5000) -> list[dict]:
        sql = """SELECT t.tag_id, t.source_id, t.site_id, t.data_type, t.unit, l.v, l.q, l.ts_ms, l.rx_ms, l.flags
                 FROM tags t LEFT JOIN latest l ON l.tag_id = t.tag_id WHERE 1 = 1"""
        params: list = []
        if source_id:
            sql += " AND t.source_id = ?"
            params.append(source_id)
        if search:
            sql += " AND t.tag_id LIKE ?"
            params.append(f"%{search}%")
        sql += " ORDER BY t.tag_id LIMIT ?"
        params.append(limit)
        out = []
        for tag, src, site, dt, unit, v, q, ts, rx, flags in self._q(sql, tuple(params)):
            out.append({"tag_id": tag, "source_id": src, "site": site, "data_type": dt, "unit": unit,
                        "v": json.loads(v) if v is not None else None, "q": q,
                        "ts": ms_to_iso(ts) if ts else None, "rx": ms_to_iso(rx) if rx else None,
                        "f": json.loads(flags) if flags else None})
        return out

    def history(self, tag_id: str, from_ms: int, to_ms: int, limit: int = 2000) -> list[dict]:
        rows = self._q("""SELECT ts_ms, v_num, v_text, q FROM readings WHERE tag_id = ? AND ts_ms >= ? AND ts_ms <= ?
                          ORDER BY ts_ms DESC LIMIT ?""", (tag_id, from_ms, to_ms, limit))
        return [{"ts": ms_to_iso(ts), "v": text if text is not None else num, "q": q}
                for ts, num, text, q in reversed(rows)]

    def events(self, limit: int = 100, since_ms: int = 0) -> list[dict]:
        rows = self._q("SELECT body FROM events WHERE ts_ms >= ? ORDER BY ts_ms DESC LIMIT ?", (since_ms, limit))
        return [self._json(b) for (b,) in rows]

    def seq_stats(self) -> dict:
        return {src: {"boot": boot, "seq": seq, "missing_batches": gaps}
                for src, boot, seq, gaps in self._q("SELECT src, boot, seq, gaps FROM ingest_seq")}

    def audit(self, user: str, action: str, target: str, detail: str = "") -> None:
        self._x("INSERT INTO audit_log (ts_ms, username, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                (now_ms(), user, action, target, detail[:2000]))

    def audit_log(self, limit: int = 200) -> list[dict]:
        rows = self._q("SELECT ts_ms, username, action, target, detail FROM audit_log ORDER BY id DESC LIMIT ?",
                       (limit,))
        return [{"ts": ms_to_iso(t), "user": u, "action": a, "target": tg, "detail": d} for t, u, a, tg, d in rows]

    # ------------------------------------------------------------ users and sessions
    USER_COLS = "username, name, email, role, active, must_change, created_ms, updated_ms, last_login_ms"

    @staticmethod
    def _user(row) -> dict:
        u, n, e, r, a, m, c, up, ll = row
        return {"username": u, "name": n, "email": e, "role": r, "active": bool(a), "must_change_password": bool(m),
                "created": ms_to_iso(c), "updated": ms_to_iso(up), "last_login": ms_to_iso(ll) if ll else None}

    def list_users(self) -> list[dict]:
        return [self._user(r) for r in self._q(f"SELECT {self.USER_COLS} FROM users ORDER BY username")]

    def get_user(self, username: str) -> dict | None:
        rows = self._q(f"SELECT {self.USER_COLS}, password_hash FROM users WHERE username = ?", (username,))
        if not rows:
            return None
        return {**self._user(rows[0][:-1]), "password_hash": rows[0][-1]}

    def create_user(self, username: str, name: str, email: str | None, role: str, password_hash: str,
                    must_change: bool) -> None:
        t = now_ms()
        self._x("""INSERT INTO users (username, name, email, role, password_hash, active, must_change, created_ms, updated_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (username, name, email, role, password_hash, True, must_change, t, t))

    def update_user(self, username: str, name: str, email: str | None, role: str, active: bool) -> None:
        self._x("UPDATE users SET name = ?, email = ?, role = ?, active = ?, updated_ms = ? WHERE username = ?",
                (name, email, role, active, now_ms(), username))

    def set_password(self, username: str, password_hash: str, must_change: bool) -> None:
        self._x("UPDATE users SET password_hash = ?, must_change = ?, updated_ms = ? WHERE username = ?",
                (password_hash, must_change, now_ms(), username))

    def delete_user(self, username: str) -> None:
        self._x("DELETE FROM sessions WHERE username = ?", (username,), commit=False)
        self._x("DELETE FROM users WHERE username = ?", (username,), commit=False)
        self._conn.commit()

    def count_admins(self) -> int:
        rows = self._q("SELECT count(*) FROM users WHERE role = ? AND active = ?", ("admin", True))
        return rows[0][0] if rows else 0

    def count_users(self) -> int:
        rows = self._q("SELECT count(*) FROM users")
        return rows[0][0] if rows else 0

    def touch_login(self, username: str) -> None:
        self._x("UPDATE users SET last_login_ms = ? WHERE username = ?", (now_ms(), username))

    def create_session(self, token_hash: str, username: str, expires_ms: int) -> None:
        t = now_ms()
        self._x("DELETE FROM sessions WHERE expires_ms < ?", (t,), commit=False)
        self._x("INSERT INTO sessions (token_hash, username, created_ms, expires_ms) VALUES (?, ?, ?, ?)",
                (token_hash, username, t, expires_ms), commit=False)
        self._conn.commit()

    def get_session(self, token_hash: str) -> tuple | None:
        """(user dict, expires_ms) for a live session of an active user, else None."""
        rows = self._q(f"""SELECT {', '.join('u.' + c.strip() for c in self.USER_COLS.split(','))}, s.expires_ms
                           FROM sessions s JOIN users u ON u.username = s.username
                           WHERE s.token_hash = ? AND s.expires_ms > ? AND u.active = ?""",
                       (token_hash, now_ms(), True))
        return (self._user(rows[0][:-1]), rows[0][-1]) if rows else None

    def extend_session(self, token_hash: str, expires_ms: int) -> None:
        self._x("UPDATE sessions SET expires_ms = ? WHERE token_hash = ?", (expires_ms, token_hash))

    def delete_session(self, token_hash: str) -> None:
        self._x("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_user_sessions(self, username: str, keep: str | None = None) -> None:
        self._x("DELETE FROM sessions WHERE username = ? AND token_hash <> ?", (username, keep or ""))
