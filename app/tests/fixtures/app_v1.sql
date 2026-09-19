-- v1 schema (SCHEMA_VERSION = 1) with sample rows, used by the migration tests.
-- Mirrors the legacy DDL of speedtest_app.db.ensure_db before schema v2,
-- including speed_tests WITHOUT the columns added later by ALTER TABLE.

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connectivity_periods (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  ended_at TEXT NULL,
  is_up INTEGER NOT NULL CHECK (is_up IN (0,1))
);

CREATE TABLE IF NOT EXISTS connectivity_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  checked_at TEXT NOT NULL,
  is_up INTEGER NOT NULL CHECK (is_up IN (0,1)),
  latency_ms REAL NULL
);
CREATE INDEX IF NOT EXISTS idx_connectivity_checks_checked_at ON connectivity_checks(checked_at);

CREATE TABLE IF NOT EXISTS speed_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  duration_seconds REAL NOT NULL,
  bytes_downloaded INTEGER NOT NULL,
  mbps REAL NOT NULL,
  error TEXT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocked_periods (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  test_type TEXT NOT NULL CHECK (test_type IN ('ping', 'speed')),
  started_at TEXT NOT NULL,
  ended_at TEXT NULL,
  reason TEXT NOT NULL CHECK (reason IN ('disabled', 'schedule'))
);
CREATE INDEX IF NOT EXISTS idx_blocked_periods_started_at ON blocked_periods(started_at);
CREATE INDEX IF NOT EXISTS idx_blocked_periods_test_type ON blocked_periods(test_type);

INSERT INTO meta(key, value) VALUES ('schema_version', '1');

-- 3 connectivity periods: up, down, up (the last one still open)
INSERT INTO connectivity_periods(started_at, ended_at, is_up) VALUES
  ('2026-01-10T09:00:00.000Z', '2026-01-10T10:00:00.000Z', 1),
  ('2026-01-10T10:00:00.000Z', '2026-01-10T10:01:00.000Z', 0),
  ('2026-01-10T10:01:00.000Z', NULL, 1);

-- 20 legacy TCP checks
INSERT INTO connectivity_checks(checked_at, is_up, latency_ms) VALUES
  ('2026-01-10T10:00:00.000Z', 0, NULL),
  ('2026-01-10T10:00:05.000Z', 0, NULL),
  ('2026-01-10T10:00:10.000Z', 0, NULL),
  ('2026-01-10T10:00:15.000Z', 0, NULL),
  ('2026-01-10T10:00:20.000Z', 0, NULL),
  ('2026-01-10T10:00:25.000Z', 0, NULL),
  ('2026-01-10T10:00:30.000Z', 0, NULL),
  ('2026-01-10T10:00:35.000Z', 0, NULL),
  ('2026-01-10T10:00:40.000Z', 0, NULL),
  ('2026-01-10T10:00:45.000Z', 0, NULL),
  ('2026-01-10T10:00:50.000Z', 0, NULL),
  ('2026-01-10T10:00:55.000Z', 0, NULL),
  ('2026-01-10T10:01:00.000Z', 1, 24.5),
  ('2026-01-10T10:01:05.000Z', 1, 25.5),
  ('2026-01-10T10:01:10.000Z', 1, 26.5),
  ('2026-01-10T10:01:15.000Z', 1, 27.5),
  ('2026-01-10T10:01:20.000Z', 1, 28.5),
  ('2026-01-10T10:01:25.000Z', 1, 29.5),
  ('2026-01-10T10:01:30.000Z', 1, 30.5),
  ('2026-01-10T10:01:35.000Z', 1, 31.5);

-- 2 speed tests (v1 column set only)
INSERT INTO speed_tests(started_at, duration_seconds, bytes_downloaded, mbps, error) VALUES
  ('2026-01-10T09:15:00.000Z', 10.0, 125000000, 100.0, NULL),
  ('2026-01-10T09:30:00.000Z', 10.0, 0, 0.0, 'timeout');

-- 1 blocked period (monitor disabled by the user)
INSERT INTO blocked_periods(test_type, started_at, ended_at, reason) VALUES
  ('ping', '2026-01-10T11:00:00.000Z', '2026-01-10T11:30:00.000Z', 'disabled');

-- legacy settings the 1->2 migration derives the `legacy-tcp` target from
INSERT INTO settings(key, value, updated_at) VALUES
  ('connect_target', 'example.org', '2026-01-10T08:00:00.000Z'),
  ('connect_interval_seconds', '7', '2026-01-10T08:00:00.000Z'),
  ('ping_timeout_ms', '1500', '2026-01-10T08:00:00.000Z');
