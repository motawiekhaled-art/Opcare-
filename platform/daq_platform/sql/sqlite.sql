-- Opcare DAQ central database, SQLite 3.35+ (development, tests, single-site demo). Same tables as postgres.sql.
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sites (site_id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY, site_id TEXT NOT NULL REFERENCES sites(site_id), type TEXT NOT NULL,
    name TEXT NOT NULL, enabled INTEGER NOT NULL, revision INTEGER NOT NULL, config TEXT NOT NULL,
    updated_ms INTEGER NOT NULL, updated_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_status (
    source_id TEXT PRIMARY KEY, state TEXT NOT NULL, ts_ms INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tags (
    tag_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, site_id TEXT NOT NULL, data_type TEXT NOT NULL,
    unit TEXT, name TEXT);
CREATE INDEX IF NOT EXISTS tags_source ON tags (source_id);
CREATE TABLE IF NOT EXISTS latest (
    tag_id TEXT PRIMARY KEY, v TEXT, q INTEGER NOT NULL, ts_ms INTEGER NOT NULL, rx_ms INTEGER NOT NULL, flags TEXT);
CREATE TABLE IF NOT EXISTS readings (
    tag_id TEXT NOT NULL, ts_ms INTEGER NOT NULL, v_num REAL, v_text TEXT, q INTEGER NOT NULL,
    PRIMARY KEY (tag_id, ts_ms)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, src TEXT NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL,
    ts_ms INTEGER NOT NULL, message TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts_ms DESC);
CREATE TABLE IF NOT EXISTS ingest_seq (
    src TEXT PRIMARY KEY, boot TEXT NOT NULL, seq INTEGER NOT NULL, gaps INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, username TEXT NOT NULL,
    action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, role TEXT NOT NULL, password_hash TEXT NOT NULL,
    active INTEGER NOT NULL, must_change INTEGER NOT NULL, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
    last_login_ms INTEGER);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, created_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (username);
