-- Opcare DAQ central database, PostgreSQL 15+ (TimescaleDB used when installed). Idempotent.
-- Times are milliseconds since the Unix epoch (UTC) in BIGINT columns, identical to the SQLite schema,
-- so the same queries run on both. TimescaleDB supports integer time columns.

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS sites (
    site_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id   TEXT PRIMARY KEY,
    site_id     TEXT NOT NULL REFERENCES sites(site_id),
    type        TEXT NOT NULL,
    name        TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL,
    revision    INTEGER NOT NULL,
    config      JSONB NOT NULL,
    updated_ms  BIGINT NOT NULL,
    updated_by  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_status (
    source_id   TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    ts_ms       BIGINT NOT NULL,
    body        JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    tag_id      TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL,
    site_id     TEXT NOT NULL,
    data_type   TEXT NOT NULL,
    unit        TEXT,
    name        TEXT
);
CREATE INDEX IF NOT EXISTS tags_source ON tags (source_id);

CREATE TABLE IF NOT EXISTS latest (
    tag_id      TEXT PRIMARY KEY,
    v           TEXT,              -- JSON-encoded value
    q           SMALLINT NOT NULL,
    ts_ms       BIGINT NOT NULL,
    rx_ms       BIGINT NOT NULL,
    flags       TEXT
);

CREATE TABLE IF NOT EXISTS readings (
    tag_id      TEXT NOT NULL,
    ts_ms       BIGINT NOT NULL,
    v_num       DOUBLE PRECISION,  -- numbers and booleans (1/0)
    v_text      TEXT,              -- strings
    q           SMALLINT NOT NULL,
    PRIMARY KEY (tag_id, ts_ms)
);

CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    src         TEXT NOT NULL,
    type        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    ts_ms       BIGINT NOT NULL,
    message     TEXT NOT NULL,
    body        JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts_ms DESC);

CREATE TABLE IF NOT EXISTS ingest_seq (
    src         TEXT PRIMARY KEY,
    boot        TEXT NOT NULL,
    seq         BIGINT NOT NULL,
    gaps        BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    ts_ms       BIGINT NOT NULL,
    username    TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    detail      TEXT
);

-- TimescaleDB: hypertable with 1-day chunks, compression after 7 days, 2-year retention (MVP defaults).
-- Set up once only: compression settings cannot be changed again after chunks have been compressed.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = 'readings') THEN
      PERFORM create_hypertable('readings', 'ts_ms', chunk_time_interval => 86400000);
      CREATE OR REPLACE FUNCTION unix_now_ms() RETURNS BIGINT LANGUAGE SQL STABLE AS
        $f$ SELECT (extract(epoch FROM now()) * 1000)::BIGINT $f$;
      PERFORM set_integer_now_func('readings', 'unix_now_ms');
      ALTER TABLE readings SET (timescaledb.compress, timescaledb.compress_segmentby = 'tag_id');
      PERFORM add_compression_policy('readings', 7::BIGINT * 86400000);
      PERFORM add_retention_policy('readings', 730::BIGINT * 86400000);
    END IF;
  END IF;
END $$;
