"""Checks the PostgreSQL schema and EVERY statement the Store sends in PostgreSQL mode, on a real server,
without a Python driver: statements are captured, rendered as SQL with literal values and run with psql.

    python platform/tools/check_postgres.py "host=/tmp port=55432 user=opcare dbname=postgres"

It creates a scratch database, applies sql/postgres.sql, replays a realistic sequence of store calls and
checks the results (latest value ordering, history, sequence gaps, events, audit, delete).
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from daq_platform.store import SQL_DIR, Store  # noqa: E402


class CaptureCursor:
    def __init__(self, log):
        self.log, self.rowcount = log, 0

    def execute(self, sql, params=()):
        self.log.append((sql, tuple(params)))

    def executemany(self, sql, seq):
        for params in seq:
            self.log.append((sql, tuple(params)))

    def fetchall(self):
        return []


class CaptureConn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return CaptureCursor(self.log)

    def commit(self):
        self.log.append(("COMMIT", ()))


def literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def render(sql: str, params: tuple) -> str:
    parts = sql.split("%s")
    assert len(parts) == len(params) + 1, (sql, params)
    out = parts[0]
    for p, rest in zip(params, parts[1:]):
        out += literal(p) + rest
    return out.strip() + ";"


def psql(dsn: str, sql: str, tuples_only: bool = True) -> str:
    args = ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-X"] + (["-tA"] if tuples_only else [])
    r = subprocess.run(args, input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"psql failed:\n{r.stderr}")
    return r.stdout.strip()


def main(admin_dsn: str) -> None:
    psql(admin_dsn, "DROP DATABASE IF EXISTS opcare_check; CREATE DATABASE opcare_check;")
    dsn = admin_dsn.replace("dbname=postgres", "dbname=opcare_check")
    psql(dsn, (SQL_DIR / "postgres.sql").read_text())
    psql(dsn, (SQL_DIR / "postgres.sql").read_text())          # idempotent: second run must also succeed
    psql(dsn, "INSERT INTO schema_version (version) VALUES (1);")

    st = Store.__new__(Store)
    st.dialect, st._conn = "postgres", CaptureConn()
    v = lambda t, val, ts: {"t": t, "v": val, "q": 192, "ts": ts, "rx": ts}  # noqa: E731
    cfg = {"spec": "1.0", "revision": 1, "site": "s1", "source_id": "src", "type": "modbus", "name": "O'Brien meters",
           "enabled": True, "connection": {"host": "h"},
           "points": [{"tag_id": "s1.a", "data_type": "float", "unit": "kW", "address": {}},
                      {"tag_id": "s1.b", "data_type": "string", "address": {}},
                      {"tag_id": "s1.c", "data_type": "bool", "address": {}}]}
    st.save_source(cfg, "khaled")
    st.save_source({**cfg, "revision": 2}, "khaled")                      # update path (ON CONFLICT DO UPDATE)
    st.write_values([v("s1.a", 1.5, "2026-11-03T18:00:01.000Z"), v("s1.b", "ABC", "2026-11-03T18:00:01.000Z"),
                     v("s1.c", True, "2026-11-03T18:00:01.000Z")])
    st.write_values([v("s1.a", 2.5, "2026-11-03T18:00:03.000Z")])
    st.write_values([v("s1.a", 9.9, "2026-11-03T18:00:02.000Z")])         # older: must not replace latest
    st.write_values([v("s1.a", 2.5, "2026-11-03T18:00:03.000Z")])         # duplicate: ignored
    st.save_status({"src": "src", "state": "running", "ts": "2026-11-03T18:00:04.000Z"})
    st.save_status({"src": "src", "state": "degraded", "ts": "2026-11-03T18:00:05.000Z"})
    st.check_seq("src", "boot-1", 1)
    st.check_seq("src", "boot-1", 4)          # statement syntax only: the gap arithmetic is tested on SQLite
    st.save_event({"id": "01JBX3Q7K2M8V5T9N4R6C1D0EF", "src": "src", "type": "conn.lost", "severity": "medium",
                   "ts": "2026-11-03T18:00:00.000Z", "message": "Device m2 unreachable"})
    st.audit("khaled", "create_source", "src", "revision 1")
    queries = len(st._conn.log)
    st.list_sources(); st.list_tags(); st.list_tags("src", "s1.", 10); st.history("s1.a", 0, 10 ** 14, 100)
    st.events(10, 0); st.seq_stats(); st.audit_log(10); st.get_source("src")

    statements = [render(s, p) for s, p in st._conn.log if s != "COMMIT"]
    psql(dsn, "BEGIN;\n" + "\n".join(statements[:queries]) + "\nCOMMIT;")
    for q in statements[queries:]:
        psql(dsn, q)                                                        # every query must run

    checks = {
        "latest s1.a is the newest value": ("SELECT v FROM latest WHERE tag_id = 's1.a'", "2.5"),
        "history keeps late value": ("SELECT string_agg(v_num::text, ',' ORDER BY ts_ms) FROM readings WHERE tag_id = 's1.a'",
                                     "1.5,9.9,2.5"),
        "string and bool stored": ("SELECT v_text || '/' || (SELECT v_num FROM readings WHERE tag_id='s1.c') FROM readings WHERE tag_id = 's1.b'",
                                   "ABC/1"),
        "source updated to revision 2": ("SELECT revision || ' ' || name FROM sources WHERE source_id = 'src'",
                                         "2 O'Brien meters"),
        "config stored as JSONB": ("SELECT config->'connection'->>'host' FROM sources", "h"),
        "status upserted": ("SELECT state FROM source_status WHERE source_id = 'src'", "degraded"),
        "tags replaced on update": ("SELECT count(*) FROM tags", "3"),
        "event stored": ("SELECT body->>'type' FROM events", "conn.lost"),
        "audit stored": ("SELECT action FROM audit_log", "create_source"),
    }
    failed = 0
    for name, (sql, want) in checks.items():
        got = psql(dsn, sql)
        ok = got == want
        failed += not ok
        print(("PASS " if ok else "FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (expected {want!r})"))
    st2 = Store.__new__(Store)
    st2.dialect, st2._conn = "postgres", CaptureConn()
    st2.delete_source("src")
    psql(dsn, "BEGIN;\n" + "\n".join(render(s, p) for s, p in st2._conn.log if s != "COMMIT") + "\nCOMMIT;")
    gone = psql(dsn, "SELECT (SELECT count(*) FROM sources) + (SELECT count(*) FROM tags) + (SELECT count(*) FROM source_status)")
    print(("PASS " if gone == "0" else "FAIL ") + f"delete removes source, tags and status: {gone}")
    failed += gone != "0"
    timescale = psql(dsn, "SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'")
    print(f"INFO {len(statements)} statements run on {psql(dsn, 'SHOW server_version')}; "
          f"TimescaleDB {'installed' if timescale == '1' else 'not installed here (hypertable block skipped)'}")
    psql(admin_dsn, "DROP DATABASE opcare_check;")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "host=/tmp port=55432 user=opcare dbname=postgres")
