"""Configuration changes are logged once per real change (design spec §3)."""
from __future__ import annotations

from speedtest_app import quality_db
from speedtest_app.db import ensure_default_setting, get_settings, set_setting


def _changes(db_path: str, key: str, utc_iso) -> list[dict]:
    rows = quality_db.query_config_changes(db_path, utc_iso(-3600), utc_iso(3600))
    return [r for r in rows if r["key"] == key]


def test_set_setting_logs_old_and_new_value(db_path, utc_iso):
    set_setting(db_path, "connect_target", "example.org", now_iso=utc_iso(0))
    set_setting(db_path, "connect_target", "example.net", now_iso=utc_iso(60))

    rows = _changes(db_path, "connect_target", utc_iso)
    assert len(rows) == 2
    assert (rows[0]["old_value"], rows[0]["new_value"], rows[0]["source"]) == (None, "example.org", "ui")
    assert (rows[1]["old_value"], rows[1]["new_value"], rows[1]["source"]) == (
        "example.org",
        "example.net",
        "ui",
    )
    assert rows[1]["changed_at"] == utc_iso(60)
    assert get_settings(db_path, ["connect_target"]) == {"connect_target": "example.net"}


def test_unchanged_value_logs_nothing(db_path, utc_iso):
    set_setting(db_path, "ping_timeout_ms", "1500", now_iso=utc_iso(0))
    set_setting(db_path, "ping_timeout_ms", "1500", now_iso=utc_iso(60))

    rows = _changes(db_path, "ping_timeout_ms", utc_iso)
    assert len(rows) == 1
    assert rows[0]["new_value"] == "1500"


def test_ensure_default_setting_logs_source_env(db_path, utc_iso):
    ensure_default_setting(db_path, "ping_enabled", "true")
    ensure_default_setting(db_path, "ping_enabled", "false")

    rows = _changes(db_path, "ping_enabled", utc_iso)
    assert len(rows) == 1
    assert (rows[0]["old_value"], rows[0]["new_value"], rows[0]["source"]) == (None, "true", "env")
    assert get_settings(db_path, ["ping_enabled"]) == {"ping_enabled": "true"}


def test_migration_logs_every_schema_version_change(db_path, utc_iso):
    """One audit row per migration step, chained — a fresh database walks them all.

    `ensure_db` creates the legacy schema and then migrates forward, so a brand
    new file logs the whole chain instead of landing on the current version out
    of nowhere. A new migration adds a link here; having to update this list is
    the point.
    """
    rows = quality_db.query_config_changes(db_path, "1970-01-01T00:00:00.000Z", utc_iso(3600))
    schema_rows = [r for r in rows if r["key"] == "schema_version"]

    assert [(r["old_value"], r["new_value"]) for r in schema_rows] == [("1", "2"), ("2", "3")]
    assert {r["source"] for r in schema_rows} == {"migration"}
