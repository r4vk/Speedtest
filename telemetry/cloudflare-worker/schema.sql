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
