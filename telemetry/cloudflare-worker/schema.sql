CREATE TABLE IF NOT EXISTS daily_events (
  day TEXT NOT NULL,
  install_id TEXT NOT NULL,
  event TEXT NOT NULL,
  version TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  first_seen_epoch INTEGER NOT NULL,
  last_seen_epoch INTEGER NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY (day, install_id, event)
);

CREATE INDEX IF NOT EXISTS idx_daily_events_last_seen_epoch
  ON daily_events(last_seen_epoch);

CREATE INDEX IF NOT EXISTS idx_daily_events_event
  ON daily_events(event);

CREATE INDEX IF NOT EXISTS idx_daily_events_version
  ON daily_events(version);

-- Legacy table kept for backward compatibility (not used by current Worker code).
CREATE TABLE IF NOT EXISTS startup_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  install_id TEXT NOT NULL,
  version TEXT NOT NULL,
  event TEXT NOT NULL,
  started_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  received_at_epoch INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_startup_events_received_at_epoch
  ON startup_events(received_at_epoch);

CREATE INDEX IF NOT EXISTS idx_startup_events_install_id
  ON startup_events(install_id);

CREATE INDEX IF NOT EXISTS idx_startup_events_version
  ON startup_events(version);
