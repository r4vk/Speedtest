# Expected Outages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a person mark an outage as expected — by hand for one row, or once via a recurring window — so a nightly router reboot stops counting against the downtime figure and stops mailing.

**Architecture:** A new `expected_windows` table holds the rules. A pure matcher (`expected_windows.py`) decides whether a *closed* outage fits entirely inside a rule's local-time window. Two writers call it at close time — `quality_engine._persist_event` for `incidents`, `AvailabilityTracker._on_up` for `connectivity_periods` — and store the verdict in three new columns on each table. Nothing is ever recomputed afterwards.

**Tech Stack:** Python 3.11+, FastAPI, SQLite (WAL), pytest, vanilla JS front end.

**Spec:** `docs/superpowers/specs/2026-09-23-expected-outages-design.md`

## Global Constraints

- Timestamps stored as UTC ISO-8601 with `Z` and millisecond precision (`to_iso_z`); display and exports convert to local and name the zone.
- `days` uses 0=Monday … 6=Sunday, the convention `_is_blocked_by_schedule` already uses.
- Rule times (`time_from`, `time_to`) are **local wall clock**, stored as `'HH:MM'`.
- A match requires **full containment**: `window_start <= started_at` and `ended_at <= window_end`.
- An open outage (`ended_at IS NULL`) never matches and cannot be marked by hand (`409`).
- `expected_source='manual'` always wins; nothing recomputes a closed row.
- No retroactive rewrite: creating or editing a rule never updates existing rows.
- A broken rule (bad `days`, bad `HH:MM`, `time_from == time_to`) must never raise inside a writer — it is skipped.
- New API text visible to users is Polish, matching the existing handlers (`"nie ma takiego incydentu"`).
- Every rule mutation writes a `config_changes` row.

## Review Focus

Five things the spec implies, that no obvious happy-path test would catch. Each has a test pinned to the task that owns the code.

1. **A rule read failure must not swallow the outage mail** — if `expected_windows` cannot be read, the mail goes out and the row stays unflagged (Task 6).
2. **`query_connectivity_periods` currently selects three columns only** — every consumer (report, CSV, `/api/outages`) breaks or silently loses the flag unless the projection grows with it (Task 4).
3. **Spring-forward** — a window instance starting in the skipped local hour must be dropped, not resolved to a different wall-clock time (Task 2).
4. **`time_from == time_to`** — read as a 24-hour window it would mark every outage; rejected at the API and skipped by the matcher (Tasks 2, 7).
5. **A manual `expected = false`** must survive: it sets `expected_source='manual'` so a later reader can tell "nobody looked" from "a person said it was real" (Task 8).

---

## File Structure

| File | Responsibility |
|---|---|
| `app/speedtest_app/expected_windows.py` | **new** — `ExpectedWindow`, `parse_windows`, `match`. Pure. |
| `app/speedtest_app/db.py` | migration 2→3; connectivity-period accessors gain the new columns |
| `app/speedtest_app/quality_db.py` | `expected_windows` CRUD; incident column plumbing and filter |
| `app/speedtest_app/quality_engine.py` | flag written with `INCIDENT_CLOSE_FIELDS` |
| `app/speedtest_app/availability.py` | flag on the closed period + mail suppression |
| `app/speedtest_app/api_quality.py` | rules CRUD, incident `PATCH`, `expected` filter |
| `app/speedtest_app/main.py` | `/api/outages` filter + `PATCH`, outages CSV |
| `app/speedtest_app/api_quality_exports.py` | incidents CSV columns |
| `app/speedtest_app/report.py` | report columns and summary line |
| `app/static/index.html`, `app.js`, `quality.js` | settings panel, badges, filter checkbox, drawer toggle |

Task dependency: **1** → **2, 3, 4** → **5, 6, 7** → **8, 9** → **10, 11, 12**.
Tasks 7, 8 and 9 all touch `api_quality.py` / `main.py`; run them in one lane, not in parallel with each other.

---

### Task 1: Schema migration 2 → 3

**Files:**
- Modify: `app/speedtest_app/db.py` (`SCHEMA_VERSION`, `_migrate`, new `_migrate_2_to_3`)
- Test: `app/tests/test_migration.py`

**Interfaces:**
- Consumes: nothing.
- Produces: table `expected_windows`; columns `expected`, `expected_source`, `expected_rule_id` on `incidents` and `connectivity_periods`; `SCHEMA_VERSION == 3`.

- [ ] **Step 1: Write the failing test**

```python
def test_migration_2_to_3_adds_expected_columns(tmp_path):
    path = str(tmp_path / "app.db")
    ensure_db(path)
    with db_conn(path) as conn:
        for table in ("incidents", "connectivity_periods"):
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert {"expected", "expected_source", "expected_rule_id"} <= cols
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "expected_windows" in tables
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert int(version["value"]) == 3


def test_migration_2_to_3_leaves_existing_rows_unexpected(tmp_path):
    path = str(tmp_path / "app.db")
    ensure_db(path)
    record_connectivity(path, is_up=False, now_iso="2026-09-01T00:00:00.000Z")
    with db_conn(path) as conn:
        row = conn.execute("SELECT expected, expected_source FROM connectivity_periods").fetchone()
    assert row["expected"] == 0
    assert row["expected_source"] is None


def test_ensure_db_is_idempotent_on_v3(tmp_path):
    path = str(tmp_path / "app.db")
    ensure_db(path)
    ensure_db(path)
    with db_conn(path) as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(incidents)")]
    assert cols.count("expected") == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest app/tests/test_migration.py -v`
Expected: FAIL — `expected_windows` is not in `sqlite_master`.

- [ ] **Step 3: Implement the migration**

In `db.py`, set `SCHEMA_VERSION = 3` and extend `_migrate`:

```python
def _migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations; each one runs in a single transaction."""
    version = _read_schema_version(conn)
    if version >= SCHEMA_VERSION:
        return
    if version < 2:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate_1_to_2(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    if _read_schema_version(conn) < 3:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate_2_to_3(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
```

```python
SCHEMA_V3_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS expected_windows (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      name       TEXT NOT NULL,
      time_from  TEXT NOT NULL,
      time_to    TEXT NOT NULL,
      days       TEXT NOT NULL,
      target_id  INTEGER NULL REFERENCES probe_targets(id) ON DELETE CASCADE,
      enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
      note       TEXT NULL,
      created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_expected_windows_enabled ON expected_windows(enabled)",
)

#: `incidents` and `connectivity_periods` carry the same verdict (spec §2).
EXPECTED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("expected", "INTEGER NOT NULL DEFAULT 0 CHECK (expected IN (0,1))"),
    ("expected_source", "TEXT NULL CHECK (expected_source IN ('rule','manual'))"),
    ("expected_rule_id", "INTEGER NULL REFERENCES expected_windows(id) ON DELETE SET NULL"),
)


def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    now_iso = _utc_now_iso()
    for statement in SCHEMA_V3_DDL:
        conn.execute(statement)
    for table in ("incidents", "connectivity_periods"):
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in EXPECTED_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_incidents_expected_time ON incidents(expected, started_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_connectivity_periods_expected "
        "ON connectivity_periods(expected, started_at)"
    )
    conn.execute(
        "INSERT INTO config_changes(changed_at, key, old_value, new_value, source) VALUES (?,?,?,?,?)",
        (now_iso, "schema_version", "2", "3", "migration"),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version','3') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest app/tests/test_migration.py app/tests/test_db_robustness.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/speedtest_app/db.py app/tests/test_migration.py
git commit -m "feat(db): add expected_windows and the expected flag (schema 3)"
```

---

### Task 2: The matcher

**Files:**
- Create: `app/speedtest_app/expected_windows.py`
- Test: `app/tests/test_expected_windows.py`

**Interfaces:**
- Consumes: `time_utils.local_tz()`.
- Produces:
  - `ExpectedWindow(id: int, name: str, time_from: str, time_to: str, days: frozenset[int], target_id: int | None, note: str | None)`
  - `parse_hhmm(value: Any) -> time | None`, `parse_days(value: Any) -> frozenset[int]`
  - `parse_windows(rows: Iterable[Mapping[str, Any]]) -> list[ExpectedWindow]`
  - `match(windows: Sequence[ExpectedWindow], started_at: datetime | None, ended_at: datetime | None, target_id: int | None = None) -> ExpectedWindow | None`

- [ ] **Step 1: Write the failing tests**

```python
"""Matching of expected maintenance windows (spec §4)."""
from datetime import datetime, timezone

import pytest

from speedtest_app.expected_windows import ExpectedWindow, match, parse_windows


def window(**over):
    base = dict(
        id=1, name="restart routera", time_from="02:55", time_to="03:15",
        days=frozenset({0, 1, 2, 3, 4, 5, 6}), target_id=None, note=None,
    )
    base.update(over)
    return ExpectedWindow(**base)


def utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# Europe/Warsaw in September is UTC+2, so local 03:00 is 01:00Z.

def test_outage_inside_the_window_matches():
    assert match([window()], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4)) is not None


def test_outage_that_overruns_the_window_does_not_match():
    assert match([window()], utc(2026, 9, 21, 0, 58), utc(2026, 9, 21, 4, 30)) is None


def test_outage_starting_before_the_window_does_not_match():
    assert match([window()], utc(2026, 9, 21, 0, 50), utc(2026, 9, 21, 1, 4)) is None


def test_bounds_are_inclusive():
    assert match([window()], utc(2026, 9, 21, 0, 55), utc(2026, 9, 21, 1, 15)) is not None


def test_open_outage_never_matches():
    assert match([window()], utc(2026, 9, 21, 1, 0), None) is None


def test_weekday_scoping():
    monday_only = window(days=frozenset({0}))
    # 2026-09-21 is a Monday, 2026-09-20 a Sunday (local time decides).
    assert match([monday_only], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4)) is not None
    assert match([monday_only], utc(2026, 9, 20, 1, 0), utc(2026, 9, 20, 1, 4)) is None


def test_target_scoping():
    scoped = window(target_id=3)
    assert match([scoped], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4), target_id=3) is not None
    assert match([scoped], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4), target_id=4) is None
    assert match([scoped], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4), target_id=None) is None
    assert match([window()], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4), target_id=7) is not None


def test_overnight_window_spanning_midnight():
    # 23:50 Monday -> 00:10 Tuesday local; the outage is at 00:00 local Tuesday.
    w = window(time_from="23:50", time_to="00:10", days=frozenset({0}))
    assert match([w], utc(2026, 9, 21, 22, 0), utc(2026, 9, 21, 22, 5)) is not None
    # The same clock time on Tuesday belongs to a window that starts Tuesday,
    # which this Monday-only rule does not cover.
    assert match([w], utc(2026, 9, 22, 22, 0), utc(2026, 9, 22, 22, 5)) is None


def test_spring_forward_skipped_hour_produces_no_window():
    # 2026-03-29, Europe/Warsaw: 02:00 -> 03:00, so local 02:30 does not exist.
    w = window(time_from="02:30", time_to="02:45")
    assert match([w], utc(2026, 3, 29, 1, 30), utc(2026, 3, 29, 1, 40)) is None


def test_autumn_fold_matches_either_instance():
    # 2026-10-25, Europe/Warsaw: 03:00 -> 02:00, so local 02:30 happens twice
    # (00:30Z and 01:30Z).
    w = window(time_from="02:20", time_to="02:40")
    assert match([w], utc(2026, 10, 25, 0, 25), utc(2026, 10, 25, 0, 35)) is not None
    assert match([w], utc(2026, 10, 25, 1, 25), utc(2026, 10, 25, 1, 35)) is not None


@pytest.mark.parametrize(
    "broken",
    [
        {"days": frozenset()},
        {"time_from": "krowa"},
        {"time_to": "25:00"},
        {"time_from": "03:00", "time_to": "03:00"},
    ],
)
def test_broken_rule_is_skipped_not_raised(broken):
    assert match([window(**broken)], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4)) is None


def test_reversed_timestamps_do_not_match():
    assert match([window()], utc(2026, 9, 21, 1, 10), utc(2026, 9, 21, 1, 0)) is None


def test_first_match_wins_by_id():
    first = window(id=1, name="pierwsze")
    second = window(id=2, name="drugie")
    assert match([first, second], utc(2026, 9, 21, 1, 0), utc(2026, 9, 21, 1, 4)).name == "pierwsze"


def test_parse_windows_reads_db_rows():
    rows = [
        {"id": 5, "name": "noc", "time_from": "02:55", "time_to": "03:15",
         "days": "[0,1,2,3,4,5,6]", "target_id": None, "note": "router", "enabled": 1},
        {"id": 6, "name": "wyłączone", "time_from": "01:00", "time_to": "02:00",
         "days": "[0]", "target_id": 2, "note": None, "enabled": 0},
    ]
    parsed = parse_windows(rows)
    assert [w.id for w in parsed] == [5]           # disabled rules are dropped
    assert parsed[0].days == frozenset({0, 1, 2, 3, 4, 5, 6})


def test_parse_windows_survives_broken_days_json():
    rows = [{"id": 1, "name": "z", "time_from": "01:00", "time_to": "02:00",
             "days": "not json", "target_id": None, "note": None, "enabled": 1}]
    assert parse_windows(rows)[0].days == frozenset()
```

Pin the zone for the module so the local-time assertions are stable:

```python
@pytest.fixture(autouse=True)
def warsaw(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    import speedtest_app.time_utils as tu
    importlib.reload(tu)
    yield
    importlib.reload(tu)
```

Check `app/tests/test_time_utils.py` first — if it already has a zone-pinning helper, reuse that one instead of writing a second.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest app/tests/test_expected_windows.py -v`
Expected: FAIL — `ModuleNotFoundError: speedtest_app.expected_windows`.

- [ ] **Step 3: Implement the module**

```python
"""Expected maintenance windows (design spec 2026-09-23 §4).

Pure logic: the matcher is handed the rules and both timestamps and never reads
a clock, a database or a setting, so tests are deterministic. Persistence is the
caller's job — `quality_engine` for `incidents`, `availability` for
`connectivity_periods`.

A rule is a local wall-clock window. `_is_blocked_by_schedule` in `scheduler.py`
answers a similar question by comparing "HH:MM" strings against `now()`, which
has no notion of a window instance on a given day and none of DST; this module
builds concrete local intervals instead, which is why the two do not share code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from .time_utils import local_tz


@dataclass(frozen=True)
class ExpectedWindow:
    """One recurring window, in local wall-clock terms."""

    id: int
    name: str
    time_from: str
    time_to: str
    days: frozenset[int]          # 0=Monday .. 6=Sunday
    target_id: int | None
    note: str | None = None


def parse_hhmm(value: Any) -> time | None:
    """`'HH:MM'` → `time`, or `None` for anything else. Never raises."""
    text = str(value or "").strip()
    if len(text) != 5 or text[2] != ":":
        return None
    try:
        hh, mm = int(text[0:2]), int(text[3:5])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return time(hh, mm)


def parse_days(value: Any) -> frozenset[int]:
    """JSON array (or any iterable) of 0–6 → frozenset. Junk yields an empty set."""
    raw: Any = value
    if not isinstance(raw, (list, tuple, set, frozenset)):
        try:
            raw = json.loads(str(value))
        except (TypeError, ValueError):
            return frozenset()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    days: set[int] = set()
    for item in raw:
        try:
            day = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6:
            days.add(day)
    return frozenset(days)


def parse_windows(rows: Iterable[Mapping[str, Any]]) -> list[ExpectedWindow]:
    """DB rows → rules, dropping the disabled ones.

    A broken row is kept but will never match (`match` skips it), so one bad
    rule cannot hide the rest.
    """
    windows: list[ExpectedWindow] = []
    for row in rows:
        if not bool(row.get("enabled", 1)):
            continue
        target_id = row.get("target_id")
        windows.append(
            ExpectedWindow(
                id=int(row["id"]),
                name=str(row.get("name") or ""),
                time_from=str(row.get("time_from") or ""),
                time_to=str(row.get("time_to") or ""),
                days=parse_days(row.get("days")),
                target_id=int(target_id) if target_id is not None else None,
                note=row.get("note") or None,
            )
        )
    windows.sort(key=lambda w: w.id)
    return windows


def _local_instants(day: date, at: time, tz) -> list[datetime]:
    """Every instant at which the clock on the wall reads `at` on `day`.

    Normally one. None at all in the hour a spring-forward skips — that window
    instance simply does not happen. Two on the autumn day, when the hour runs
    twice; both are returned, oldest first.
    """
    naive = datetime.combine(day, at)
    out: list[datetime] = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        # A local time the clock skips round-trips to a different wall clock.
        if aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None, fold=0) != naive:
            continue
        if not any(aware == seen for seen in out):
            out.append(aware)
    return out


def match(
    windows: Sequence[ExpectedWindow],
    started_at: datetime | None,
    ended_at: datetime | None,
    target_id: int | None = None,
) -> ExpectedWindow | None:
    """The first rule that fully contains `[started_at, ended_at]`, or `None`.

    Containment, not overlap: an outage that outlasts the window is a real one
    that happened to begin during a reboot, and must keep counting.
    """
    if started_at is None or ended_at is None or ended_at < started_at:
        return None
    tz = local_tz()
    start_local = started_at.astimezone(tz)
    for window in windows:
        if window.target_id is not None and window.target_id != target_id:
            continue
        begin_at = parse_hhmm(window.time_from)
        end_at = parse_hhmm(window.time_to)
        if begin_at is None or end_at is None or begin_at == end_at or not window.days:
            continue
        overnight = end_at < begin_at
        # The instance covering this outage starts either on the outage's own
        # local day or, for an overnight window, on the day before.
        for offset in (0, -1) if overnight else (0,):
            day = (start_local + timedelta(days=offset)).date()
            if day.weekday() not in window.days:
                continue
            end_day = day + timedelta(days=1) if overnight else day
            for begin in _local_instants(day, begin_at, tz):
                for end in _local_instants(end_day, end_at, tz):
                    if end > begin and begin <= started_at and ended_at <= end:
                        return window
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest app/tests/test_expected_windows.py -v`
Expected: PASS, all cases including both DST ones.

- [ ] **Step 5: Commit**

```bash
git add app/speedtest_app/expected_windows.py app/tests/test_expected_windows.py
git commit -m "feat(quality): match outages against expected maintenance windows"
```

---

### Task 3: `expected_windows` persistence and the incident filter

**Files:**
- Modify: `app/speedtest_app/quality_db.py`
- Test: `app/tests/test_quality_db.py`

**Interfaces:**
- Consumes: Task 1's schema; `_insert`, `_update`, `_dicts`, `db_conn` already in the module.
- Produces:
  - `EXPECTED_WINDOW_COLUMNS: frozenset[str]`, `EXPECTED_FILTERS: set[str]`, `expected_clause(expected: str) -> str | None`, `expected_mode(value: str | None) -> str` (added in Task 9, imported by both API modules)
  - `list_expected_windows(db_path, enabled_only=False) -> list[dict]`
  - `get_expected_window(db_path, window_id) -> dict | None`
  - `insert_expected_window(db_path, now_iso=None, **fields) -> int`
  - `update_expected_window(db_path, window_id, **fields) -> int`
  - `delete_expected_window(db_path, window_id) -> bool`
  - `query_incidents(..., expected: str = "all")` accepting `"all" | "exclude" | "only"`
  - `INCIDENT_COLUMNS` extended with `expected`, `expected_source`, `expected_rule_id`

- [ ] **Step 1: Write the failing tests**

```python
def test_expected_window_crud(db_path):
    window_id = quality_db.insert_expected_window(
        db_path, name="restart routera", time_from="02:55", time_to="03:15",
        days="[0,1,2,3,4,5,6]", note="codzienny",
    )
    rows = quality_db.list_expected_windows(db_path)
    assert [r["name"] for r in rows] == ["restart routera"]
    assert rows[0]["enabled"] == 1 and rows[0]["created_at"]

    assert quality_db.update_expected_window(db_path, window_id, enabled=0) == 1
    assert quality_db.list_expected_windows(db_path, enabled_only=True) == []

    assert quality_db.delete_expected_window(db_path, window_id) is True
    assert quality_db.delete_expected_window(db_path, window_id) is False


def test_expected_window_rejects_unknown_column(db_path):
    with pytest.raises(ValueError):
        quality_db.insert_expected_window(db_path, name="x", nonsense=1)


def test_incident_expected_columns_round_trip(db_path):
    target = quality_db.list_targets(db_path)[0]
    incident_id = quality_db.insert_incident(
        db_path, target_id=target.id, protocol="icmp", kind="outage",
        started_at="2026-09-21T01:00:00.000Z", window_seconds=10, probe_interval_seconds=1.0,
    )
    quality_db.update_incident(
        db_path, incident_id, expected=1, expected_source="rule", expected_rule_id=None
    )
    row = quality_db.get_incident(db_path, incident_id)
    assert (row["expected"], row["expected_source"]) == (1, "rule")


def test_query_incidents_expected_filter(db_path):
    target = quality_db.list_targets(db_path)[0]
    common = dict(target_id=target.id, protocol="icmp", kind="outage",
                  window_seconds=10, probe_interval_seconds=1.0)
    plain = quality_db.insert_incident(db_path, started_at="2026-09-21T01:00:00.000Z", **common)
    planned = quality_db.insert_incident(db_path, started_at="2026-09-21T02:00:00.000Z", **common)
    quality_db.update_incident(db_path, planned, expected=1, expected_source="rule")

    start, end = "2026-09-21T00:00:00.000Z", "2026-09-21T23:00:00.000Z"

    def ids(mode):
        return [r["id"] for r in quality_db.query_incidents(db_path, start, end, expected=mode)]

    assert ids("all") == [plain, planned]
    assert ids("exclude") == [plain]
    assert ids("only") == [planned]
    assert ids("nonsense") == [plain, planned]      # unknown value falls back to "all"


def test_retention_never_deletes_expected_windows(db_path):
    """Rules are configuration, not measurements (spec §11)."""
    quality_db.insert_expected_window(
        db_path, name="restart routera", time_from="02:55", time_to="03:15", days="[0]"
    )
    set_setting(db_path, "retention_incident_days", "1")
    run_retention(db_path)                 # whatever `retention.py` exposes as its entry point
    assert len(quality_db.list_expected_windows(db_path)) == 1
```

Read `app/speedtest_app/retention.py` for the real entry-point name and call that; the assertion is the point, not the helper's name.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest app/tests/test_quality_db.py -v -k expected`
Expected: FAIL — `module 'quality_db' has no attribute 'insert_expected_window'`.

- [ ] **Step 3: Implement**

```python
EXPECTED_WINDOW_COLUMNS = frozenset(
    {"name", "time_from", "time_to", "days", "target_id", "enabled", "note", "created_at"}
)

#: `expected` query modes shared by the incident and connectivity queries.
EXPECTED_FILTERS = {"all", "exclude", "only"}


def expected_clause(expected: str) -> str | None:
    """SQL fragment for an `expected` filter, or `None` for "no filter"."""
    if expected == "exclude":
        return "expected = 0"
    if expected == "only":
        return "expected = 1"
    return None
```

Add `"expected"`, `"expected_source"`, `"expected_rule_id"` to `INCIDENT_COLUMNS`, then:

```python
# ---------------------------------------------------------------------------
# expected_windows
# ---------------------------------------------------------------------------

def list_expected_windows(db_path: str, enabled_only: bool = False) -> list[dict[str, Any]]:
    where = " WHERE enabled = 1" if enabled_only else ""
    with db_conn(db_path) as conn:
        rows = conn.execute(f"SELECT * FROM expected_windows{where} ORDER BY id").fetchall()
    return _dicts(rows)


def get_expected_window(db_path: str, window_id: int) -> dict[str, Any] | None:
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM expected_windows WHERE id = ?", (window_id,)).fetchone()
    return dict(row) if row is not None else None


def insert_expected_window(db_path: str, now_iso: str | None = None, **fields: Any) -> int:
    fields.setdefault("created_at", now_iso or _now_iso())
    fields.setdefault("enabled", 1)
    with db_conn(db_path) as conn:
        return _insert(conn, "expected_windows", fields, EXPECTED_WINDOW_COLUMNS)


def update_expected_window(db_path: str, window_id: int, **fields: Any) -> int:
    with db_conn(db_path) as conn:
        return _update(conn, "expected_windows", window_id, fields, EXPECTED_WINDOW_COLUMNS)


def delete_expected_window(db_path: str, window_id: int) -> bool:
    with db_conn(db_path) as conn:
        cur = conn.execute("DELETE FROM expected_windows WHERE id = ?", (window_id,))
    return cur.rowcount > 0
```

And the `query_incidents` change:

```python
def query_incidents(
    db_path: str,
    start_iso: str,
    end_iso: str,
    target_id: int | None = None,
    open_only: bool = False,
    expected: str = "all",
) -> list[dict[str, Any]]:
    """Incidents overlapping the range (an incident is open until `closed_at`).

    `expected` is `all` (default), `exclude` or `only`; an unknown value is
    read as `all`, so a typo in a query string can never hide an outage.
    """
    where = ["started_at < ?", "(ended_at IS NULL OR ended_at > ?)"]
    params: list[Any] = [end_iso, start_iso]
    if target_id is not None:
        where.append("target_id = ?")
        params.append(target_id)
    if open_only:
        where.append("closed_at IS NULL")
    clause = expected_clause(expected)
    if clause is not None:
        where.append(clause)
    # ... unchanged SELECT below
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest app/tests/test_quality_db.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/speedtest_app/quality_db.py app/tests/test_quality_db.py
git commit -m "feat(db): expected-window CRUD and an expected filter for incidents"
```

---

### Task 4: Connectivity-period accessors

**Files:**
- Modify: `app/speedtest_app/db.py` (`query_connectivity_periods`, two new mark functions)
- Test: `app/tests/test_availability.py`

**Interfaces:**
- Consumes: Task 1's columns.
- Produces:
  - `query_connectivity_periods(db_path, tr, is_up=None, expected="all")` now selecting `id, started_at, ended_at, is_up, expected, expected_source, expected_rule_id`
  - `mark_connectivity_period_expected(db_path, *, started_at_iso, expected, source, rule_id=None) -> int`
  - `mark_connectivity_period_expected_by_id(db_path, period_id, *, expected, source, rule_id=None) -> int`

**Review Focus #2 lives here:** the projection must grow, or every consumer silently loses the flag.

- [ ] **Step 1: Write the failing tests**

```python
def test_query_connectivity_periods_exposes_id_and_flag(db_path):
    record_connectivity(db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    tr = TimeRange(start_iso="2026-09-21T00:00:00.000Z", end_iso="2026-09-21T02:00:00.000Z")
    down = query_connectivity_periods(db_path, tr=tr, is_up=False)
    assert set(down[0]) >= {"id", "started_at", "ended_at", "is_up",
                            "expected", "expected_source", "expected_rule_id"}
    assert down[0]["expected"] == 0


def test_mark_connectivity_period_expected_targets_the_down_period(db_path):
    record_connectivity(db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    updated = mark_connectivity_period_expected(
        db_path, started_at_iso="2026-09-21T01:00:00.000Z",
        expected=True, source="rule", rule_id=None,
    )
    assert updated == 1
    tr = TimeRange(start_iso="2026-09-21T00:00:00.000Z", end_iso="2026-09-21T02:00:00.000Z")
    assert query_connectivity_periods(db_path, tr=tr, is_up=False)[0]["expected"] == 1
    # The "up" period that starts at the same instant must not be touched.
    assert query_connectivity_periods(db_path, tr=tr, is_up=True)[0]["expected"] == 0


def test_query_connectivity_periods_expected_filter(db_path):
    record_connectivity(db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    record_connectivity(db_path, is_up=False, now_iso="2026-09-21T03:00:00.000Z")
    record_connectivity(db_path, is_up=True, now_iso="2026-09-21T03:05:00.000Z")
    mark_connectivity_period_expected(
        db_path, started_at_iso="2026-09-21T03:00:00.000Z",
        expected=True, source="rule", rule_id=None,
    )
    tr = TimeRange(start_iso="2026-09-21T00:00:00.000Z", end_iso="2026-09-21T04:00:00.000Z")

    def starts(mode):
        return [r["started_at"] for r in
                query_connectivity_periods(db_path, tr=tr, is_up=False, expected=mode)]

    assert starts("exclude") == ["2026-09-21T01:00:00.000Z"]
    assert starts("only") == ["2026-09-21T03:00:00.000Z"]
    assert len(starts("all")) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest app/tests/test_availability.py -v -k connectivity_period`
Expected: FAIL — `KeyError: 'id'`.

- [ ] **Step 3: Implement**

```python
def query_connectivity_periods(db_path: str, tr: TimeRange, is_up: bool | None = None, expected: str = "all"):
    where = ["started_at < ?", "(ended_at IS NULL OR ended_at > ?)"]
    params: list[Any] = [tr.end_iso, tr.start_iso]
    if is_up is not None:
        where.append("is_up = ?")
        params.append(1 if is_up else 0)
    if expected == "exclude":
        where.append("expected = 0")
    elif expected == "only":
        where.append("expected = 1")
    with db_conn(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT id, started_at, ended_at, is_up, expected, expected_source, expected_rule_id
            FROM connectivity_periods
            WHERE {' AND '.join(where)}
            ORDER BY started_at ASC
            """,
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_connectivity_period_expected(
    db_path: str,
    *,
    started_at_iso: str,
    expected: bool,
    source: str,
    rule_id: int | None = None,
) -> int:
    """Flag the *down* period that starts at `started_at_iso`.

    Addressed by start time rather than id because the caller
    (`AvailabilityTracker`) knows when the outage began but never held its row
    id — `record_connectivity` closed the period on the way past. `is_up = 0`
    keeps it off the "up" period that starts at the same instant.
    """
    with db_conn(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE connectivity_periods
            SET expected = ?, expected_source = ?, expected_rule_id = ?
            WHERE started_at = ? AND is_up = 0
            """,
            (1 if expected else 0, source, rule_id, started_at_iso),
        )
        return cur.rowcount


def mark_connectivity_period_expected_by_id(
    db_path: str, period_id: int, *, expected: bool, source: str, rule_id: int | None = None
) -> int:
    with db_conn(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE connectivity_periods
            SET expected = ?, expected_source = ?, expected_rule_id = ?
            WHERE id = ? AND is_up = 0
            """,
            (1 if expected else 0, source, rule_id, period_id),
        )
        return cur.rowcount
```

- [ ] **Step 4: Run the tests and the consumers**

Run: `.venv/bin/python -m pytest app/tests/test_availability.py app/tests/test_report.py app/tests/test_exports.py app/tests/test_main_wiring.py -v`
Expected: PASS. The wider projection is additive, so existing consumers keep working; if one asserts an exact key set, widen that assertion.

- [ ] **Step 5: Commit**

```bash
git add app/speedtest_app/db.py app/tests/test_availability.py
git commit -m "feat(db): expose and set the expected flag on connectivity periods"
```

---

### Task 5: The quality engine writes the flag

**Files:**
- Modify: `app/speedtest_app/quality_engine.py` (`_persist_event`, new `_expected_fields`)
- Modify: `docs/superpowers/specs/2026-09-23-expected-outages-design.md` (§5 caching note)
- Test: `app/tests/test_quality_engine_wiring.py`

**Interfaces:**
- Consumes: `expected_windows.parse_windows`, `expected_windows.match`, `quality_db.list_expected_windows`.
- Produces: a closed incident row carrying `expected`, `expected_source='rule'`, `expected_rule_id`.

**Spec amendment, done in this task:** §5 says the rules are cached with the other settings and warns about `SETTINGS_REFRESH_SECONDS` staleness. Incidents close rarely, so the rules are read at close time instead — one small query per closed incident. Delete the caching paragraph and its caveat from §5; a rule now applies from the moment it is saved.

- [ ] **Step 1: Write the failing test**

```python
def test_closing_incident_inside_a_window_marks_it_expected(db_path):
    quality_db.insert_expected_window(
        db_path, name="restart routera", time_from="02:55", time_to="03:15",
        days="[0,1,2,3,4,5,6]",
    )
    engine = _engine(db_path)                     # existing helper in this module
    incident_id = _close_incident(
        engine, started_at="2026-09-21T01:00:00.000Z", ended_at="2026-09-21T01:04:00.000Z"
    )
    row = quality_db.get_incident(db_path, incident_id)
    assert row["expected"] == 1
    assert row["expected_source"] == "rule"
    assert row["expected_rule_id"] is not None


def test_closing_incident_that_overruns_the_window_stays_unexpected(db_path):
    quality_db.insert_expected_window(
        db_path, name="restart routera", time_from="02:55", time_to="03:15",
        days="[0,1,2,3,4,5,6]",
    )
    engine = _engine(db_path)
    incident_id = _close_incident(
        engine, started_at="2026-09-21T01:00:00.000Z", ended_at="2026-09-21T04:30:00.000Z"
    )
    row = quality_db.get_incident(db_path, incident_id)
    assert row["expected"] == 0
    assert row["expected_source"] is None


def test_unreadable_rules_do_not_stop_the_incident_from_closing(db_path, monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(quality_db, "list_expected_windows", boom)
    engine = _engine(db_path)
    incident_id = _close_incident(
        engine, started_at="2026-09-21T01:00:00.000Z", ended_at="2026-09-21T01:04:00.000Z"
    )
    row = quality_db.get_incident(db_path, incident_id)
    assert row["closed_at"] is not None
    assert row["expected"] == 0
```

Write `_close_incident` as a helper in the test module if the file has none: drive the engine's `_persist_event` with a `closed` `IncidentEvent` built the way the existing wiring tests build one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest app/tests/test_quality_engine_wiring.py -v -k expected`
Expected: FAIL — `row["expected"] == 0`.

- [ ] **Step 3: Implement**

In `quality_engine.py`:

```python
from .expected_windows import match as match_expected_window, parse_windows
```

```python
    def _expected_fields(self, row: Mapping[str, Any], target_id: int) -> dict[str, Any]:
        """Verdict of the expected-window rules for a closing incident.

        Read at close time rather than cached: incidents close rarely, and a
        rule saved a minute ago should already hold. A failure here is logged
        and treated as "no rules" — an unflagged incident is recoverable by
        hand, a lost one is not.
        """
        try:
            windows = parse_windows(
                quality_db.list_expected_windows(self._db_path, enabled_only=True)
            )
            if not windows:
                return {}
            started, ended = row.get("started_at"), row.get("ended_at")
            window = match_expected_window(
                windows,
                parse_dt(str(started)) if started else None,
                parse_dt(str(ended)) if ended else None,
                target_id=target_id,
            )
        except Exception:
            log.warning("Could not evaluate the expected windows", exc_info=True)
            return {}
        if window is None:
            return {}
        return {"expected": 1, "expected_source": "rule", "expected_rule_id": window.id}
```

and in `_persist_event`, on the close branch:

```python
            fields = INCIDENT_CLOSE_FIELDS if event.type == "closed" else INCIDENT_UPDATE_FIELDS
            values = {name: row[name] for name in fields}
            if event.type == "closed":
                values.update(self._expected_fields(row, event.target_id))
            quality_db.update_incident(self._db_path, incident_id, **values)
```

Extend the existing close log line so an operator can see the verdict:

```python
                log.info(
                    "incident %s closed on target %s (%s), reason=%s%s",
                    incident_id, event.target_id, event.protocol, event.state.close_reason,
                    f", expected by rule {values['expected_rule_id']}" if values.get("expected") else "",
                )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest app/tests/test_quality_engine_wiring.py app/tests/test_quality_engine.py -v`
Expected: PASS.

- [ ] **Step 5: Amend the spec and commit**

Edit §5 of the spec: drop the caching paragraph and the `SETTINGS_REFRESH_SECONDS` caveat, and say the rules are read at close time.

```bash
git add app/speedtest_app/quality_engine.py app/tests/test_quality_engine_wiring.py \
        docs/superpowers/specs/2026-09-23-expected-outages-design.md
git commit -m "feat(quality): flag a closing incident that fits an expected window"
```

---

### Task 6: Mail suppression and the flagged period

**Files:**
- Modify: `app/speedtest_app/availability.py` (`_on_up`, new `_expected_window`)
- Test: `app/tests/test_availability.py`

**Interfaces:**
- Consumes: Task 2's matcher, Task 4's `mark_connectivity_period_expected`.
- Produces: no notifier call for a contained outage; the down period carries the flag.

**Review Focus #1 lives here.**

- [ ] **Step 1: Write the failing tests**

```python
def test_outage_inside_an_expected_window_sends_no_mail(db_path, cfg_with_smtp):
    quality_db.insert_expected_window(
        db_path, name="restart routera", time_from="02:55", time_to="03:15",
        days="[0,1,2,3,4,5,6]",
    )
    sent: list[tuple] = []
    tracker = AvailabilityTracker(db_path, cfg_with_smtp, notifier=lambda *a: sent.append(a))
    tracker.apply("down", now=parse_dt("2026-09-21T01:00:00.000Z"))
    tracker.apply("up", now=parse_dt("2026-09-21T01:04:00.000Z"))

    assert sent == []
    tr = TimeRange(start_iso="2026-09-21T00:00:00.000Z", end_iso="2026-09-21T02:00:00.000Z")
    down = query_connectivity_periods(db_path, tr=tr, is_up=False)[0]
    assert (down["expected"], down["expected_source"]) == (1, "rule")
    assert down["expected_rule_id"] is not None


def test_outage_that_overruns_the_window_still_mails(db_path, cfg_with_smtp):
    quality_db.insert_expected_window(
        db_path, name="restart routera", time_from="02:55", time_to="03:15",
        days="[0,1,2,3,4,5,6]",
    )
    sent: list[tuple] = []
    tracker = AvailabilityTracker(db_path, cfg_with_smtp, notifier=lambda *a: sent.append(a))
    tracker.apply("down", now=parse_dt("2026-09-21T01:00:00.000Z"))
    tracker.apply("up", now=parse_dt("2026-09-21T04:30:00.000Z"))

    assert len(sent) == 1
    tr = TimeRange(start_iso="2026-09-21T00:00:00.000Z", end_iso="2026-09-21T05:00:00.000Z")
    assert query_connectivity_periods(db_path, tr=tr, is_up=False)[0]["expected"] == 0


def test_unreadable_rules_still_send_the_mail(db_path, cfg_with_smtp, monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(quality_db, "list_expected_windows", boom)
    sent: list[tuple] = []
    tracker = AvailabilityTracker(db_path, cfg_with_smtp, notifier=lambda *a: sent.append(a))
    tracker.apply("down", now=parse_dt("2026-09-21T01:00:00.000Z"))
    tracker.apply("up", now=parse_dt("2026-09-21T01:04:00.000Z"))
    assert len(sent) == 1


def test_short_outage_keeps_its_existing_min_seconds_behaviour(db_path, cfg_with_smtp):
    """The expected check must not disturb the smtp_min_outage_seconds gate."""
    sent: list[tuple] = []
    tracker = AvailabilityTracker(db_path, cfg_with_smtp, notifier=lambda *a: sent.append(a))
    tracker.apply("down", now=parse_dt("2026-09-21T01:00:00.000Z"))
    tracker.apply("up", now=parse_dt("2026-09-21T01:00:10.000Z"))
    assert sent == []
```

Reuse whatever SMTP-enabled config fixture `test_availability.py` already has; add `cfg_with_smtp` only if there is none.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest app/tests/test_availability.py -v -k expected`
Expected: FAIL — a mail is sent for the contained outage.

- [ ] **Step 3: Implement**

```python
from . import quality_db
from .db import mark_connectivity_period_expected
from .expected_windows import match as match_expected_window, parse_windows
```

```python
    def _expected_window(self, started_at: str, ended_at: str):
        """The rule covering this outage, or `None`.

        A failure to read the rules is "no rules": the mail goes out and the
        row stays unflagged. Losing a mail is worse than sending one too many.
        """
        try:
            windows = parse_windows(
                quality_db.list_expected_windows(self._db_path, enabled_only=True)
            )
            if not windows:
                return None
            return match_expected_window(
                windows, parse_dt(started_at), parse_dt(ended_at), target_id=None
            )
        except Exception:
            log.warning("Could not evaluate the expected windows", exc_info=True)
            return None
```

In `_on_up`, after `started_local` / `ended_local` are computed and **before** the `smtp_enabled` guard, so the row is flagged even when SMTP is off:

```python
        window = self._expected_window(started_at, now_iso)
        if window is not None:
            try:
                mark_connectivity_period_expected(
                    self._db_path, started_at_iso=started_at,
                    expected=True, source="rule", rule_id=window.id,
                )
            except Exception:
                log.warning("Could not flag the expected outage period", exc_info=True)
            log.info(
                "Outage %s–%s fits the expected window %r, no e-mail sent",
                started_local, ended_local, window.name,
            )
            return
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest app/tests/test_availability.py -v`
Expected: PASS, existing cases included.

- [ ] **Step 5: Commit**

```bash
git add app/speedtest_app/availability.py app/tests/test_availability.py
git commit -m "feat(alerts): suppress the recovery mail for an expected outage"
```

---

### Task 7: Rules API

**Files:**
- Modify: `app/speedtest_app/api_quality.py`
- Test: `app/tests/test_api_quality.py`

**Interfaces:**
- Consumes: Task 3's CRUD, `expected_windows.parse_hhmm`, `expected_windows.parse_days`.
- Produces: `GET/POST/PUT/DELETE /api/quality/expected-windows[/{id}]`, payload keys `window` / `windows`; helper `expected_window_payload(row) -> dict`.

**Review Focus #4 lives here** (`time_from == time_to` rejected).

- [ ] **Step 1: Write the failing tests**

```python
def test_expected_window_crud_over_http(client):
    created = client.post("/api/quality/expected-windows", json={
        "name": "restart routera", "time_from": "02:55", "time_to": "03:15",
        "days": [0, 1, 2, 3, 4, 5, 6], "note": "codzienny",
    })
    assert created.status_code == 201
    window = created.json()["window"]
    assert window["days"] == [0, 1, 2, 3, 4, 5, 6] and window["enabled"] is True

    listed = client.get("/api/quality/expected-windows").json()["windows"]
    assert [w["name"] for w in listed] == ["restart routera"]

    updated = client.put(f"/api/quality/expected-windows/{window['id']}", json={
        "name": "restart routera", "time_from": "02:50", "time_to": "03:20",
        "days": [0], "enabled": False,
    })
    assert updated.json()["window"]["time_from"] == "02:50"
    assert updated.json()["window"]["enabled"] is False

    assert client.delete(f"/api/quality/expected-windows/{window['id']}").status_code == 200
    assert client.delete(f"/api/quality/expected-windows/{window['id']}").status_code == 404


@pytest.mark.parametrize("body", [
    {"name": "", "time_from": "02:55", "time_to": "03:15", "days": [0]},
    {"name": "x", "time_from": "krowa", "time_to": "03:15", "days": [0]},
    {"name": "x", "time_from": "02:55", "time_to": "25:00", "days": [0]},
    {"name": "x", "time_from": "03:00", "time_to": "03:00", "days": [0]},
    {"name": "x", "time_from": "02:55", "time_to": "03:15", "days": []},
    {"name": "x", "time_from": "02:55", "time_to": "03:15", "days": [9]},
    {"name": "x", "time_from": "02:55", "time_to": "03:15", "days": [0], "target_id": 9999},
])
def test_expected_window_validation(client, body):
    assert client.post("/api/quality/expected-windows", json=body).status_code == 422


def test_expected_window_mutation_is_audited(client):
    client.post("/api/quality/expected-windows", json={
        "name": "restart routera", "time_from": "02:55", "time_to": "03:15", "days": [0],
    })
    changes = query_config_changes(client.app_db_path, "2000-01-01T00:00:00.000Z",
                                   "2100-01-01T00:00:00.000Z")
    assert any(c["key"].startswith("expected_window") for c in changes)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest app/tests/test_api_quality.py -v -k expected_window`
Expected: FAIL — 404 on `/api/quality/expected-windows`.

- [ ] **Step 3: Implement**

```python
class ExpectedWindowBody(BaseModel):
    name: str = Field(max_length=200)
    time_from: str = Field(max_length=5)
    time_to: str = Field(max_length=5)
    days: list[int]
    target_id: int | None = None
    enabled: bool = True
    note: str | None = Field(default=None, max_length=2000)


def _validated_window_fields(db_path: str, body: ExpectedWindowBody) -> dict[str, Any]:
    """Reject a rule that cannot mean what its author intended (spec §7)."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="nazwa nie może być pusta")
    if parse_hhmm(body.time_from) is None or parse_hhmm(body.time_to) is None:
        raise HTTPException(status_code=422, detail="godziny muszą mieć format HH:MM")
    if body.time_from == body.time_to:
        # Read as a 24-hour window it would quietly mark every outage expected.
        raise HTTPException(status_code=422, detail="okno o zerowej długości")
    days = sorted({int(d) for d in body.days})
    if not days or any(d < 0 or d > 6 for d in days):
        raise HTTPException(status_code=422, detail="dni muszą być liczbami 0-6")
    if body.target_id is not None and quality_db.get_target(db_path, body.target_id) is None:
        raise HTTPException(status_code=422, detail="nie ma takiego celu")
    return {
        "name": name,
        "time_from": body.time_from,
        "time_to": body.time_to,
        "days": json.dumps(days),
        "target_id": body.target_id,
        "enabled": 1 if body.enabled else 0,
        "note": (body.note or "").strip() or None,
    }


def expected_window_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "time_from": row["time_from"],
        "time_to": row["time_to"],
        "days": sorted(parse_days(row["days"])),
        "target_id": row["target_id"],
        "enabled": bool(row["enabled"]),
        "note": row["note"],
        "created_at": local_iso(parse_dt(str(row["created_at"]))),
    }
```

The four handlers follow the shape of the `/targets` ones already in the file: `GET` returns `{"windows": [...]}`; `POST` returns `{"window": ...}` with `status_code=201`; `PUT` raises `404` (`"nie ma takiego okna"`) when the id is unknown; `DELETE` returns `{"ok": True}`. Each mutation records the change through the same `config_changes` path the `/targets` handlers use — read one of them and copy the call exactly, with key `f"expected_window:{window_id}"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest app/tests/test_api_quality.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/speedtest_app/api_quality.py app/tests/test_api_quality.py
git commit -m "feat(api): CRUD for expected maintenance windows"
```

---

### Task 8: Manual marking

**Files:**
- Modify: `app/speedtest_app/api_quality.py` (incident `PATCH`), `app/speedtest_app/main.py` (outage `PATCH`, `/api/outages` payload)
- Test: `app/tests/test_api_quality.py`, `app/tests/test_main_wiring.py`

**Interfaces:**
- Consumes: Task 3 `update_incident`, Task 4 `mark_connectivity_period_expected_by_id`, `quality_db.insert_annotation`.
- Produces: `PATCH /api/quality/incidents/{id}/expected`, `PATCH /api/outages/{id}/expected`, body `{"expected": bool, "note": str | None}`; `/api/outages` items gain `id`, `expected`, `expected_source`, `expected_rule_id`.

**Review Focus #5 lives here** (a manual `false` sets `expected_source='manual'`).

- [ ] **Step 1: Write the failing tests**

```python
def test_mark_incident_expected_by_hand(client):
    incident_id = _closed_incident(client)               # helper: insert + close a row
    response = client.patch(f"/api/quality/incidents/{incident_id}/expected",
                            json={"expected": True, "note": "restart routera"})
    assert response.status_code == 200
    assert response.json()["incident"]["expected"] == 1
    assert response.json()["incident"]["expected_source"] == "manual"

    detail = client.get(f"/api/quality/incidents/{incident_id}").json()
    assert any(a["label"] == "expected" for a in detail["annotations"])


def test_manual_unmark_records_that_a_person_looked(client):
    incident_id = _closed_incident(client, expected=1, expected_source="rule")
    response = client.patch(f"/api/quality/incidents/{incident_id}/expected",
                            json={"expected": False})
    assert response.json()["incident"]["expected"] == 0
    assert response.json()["incident"]["expected_source"] == "manual"


def test_open_incident_cannot_be_marked(client):
    incident_id = _open_incident(client)
    assert client.patch(f"/api/quality/incidents/{incident_id}/expected",
                        json={"expected": True}).status_code == 409


def test_marking_an_unknown_incident_is_404(client):
    assert client.patch("/api/quality/incidents/424242/expected",
                        json={"expected": True}).status_code == 404


def test_mark_outage_expected_by_hand(client):
    record_connectivity(client.app_db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(client.app_db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    url = "/api/outages?from=2026-09-21T00:00&to=2026-09-21T02:00"
    period_id = client.get(url).json()["items"][0]["id"]
    assert client.patch(f"/api/outages/{period_id}/expected", json={"expected": True}).status_code == 200
    items = client.get(url).json()["items"]
    assert items[0]["expected"] == 1 and items[0]["expected_source"] == "manual"


def test_open_outage_cannot_be_marked(client):
    record_connectivity(client.app_db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    url = "/api/outages?from=2026-09-21T00:00&to=2026-09-21T02:00"
    period_id = client.get(url).json()["items"][0]["id"]
    assert client.patch(f"/api/outages/{period_id}/expected", json={"expected": True}).status_code == 409
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest app/tests/test_api_quality.py app/tests/test_main_wiring.py -v -k expected`
Expected: FAIL — 405 on the `PATCH`.

- [ ] **Step 3: Implement**

```python
class ExpectedMark(BaseModel):
    expected: bool
    note: str | None = Field(default=None, max_length=2000)


@router.patch("/quality/incidents/{incident_id}/expected")
def api_mark_incident_expected(request: Request, incident_id: int, body: ExpectedMark) -> dict[str, Any]:
    """A person's verdict on one closed incident; it outranks any rule (spec §6)."""
    db_path = db_path_of(request)
    row = quality_db.get_incident(db_path, incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="nie ma takiego incydentu")
    if not row.get("ended_at"):
        raise HTTPException(status_code=409, detail="incydent jeszcze trwa")

    quality_db.update_incident(
        db_path, incident_id, expected=1 if body.expected else 0, expected_source="manual"
    )
    note = (body.note or "").strip()
    if note:
        quality_db.insert_annotation(
            db_path, str(row["started_at"]), "expected", note=note, incident_id=incident_id
        )
    names = target_names(db_path)
    updated = quality_db.get_incident(db_path, incident_id)
    return {"tz": tz_name(), "incident": incident_payload(updated, names)}
```

`main.py` gets the mirror for a period: find the row through `query_connectivity_periods` over a wide range (or a small `get_connectivity_period(db_path, period_id)` helper beside the others in `db.py`), `404` when absent, `409` when `ended_at` is `NULL`, `mark_connectivity_period_expected_by_id` to write, and `insert_annotation` (no `incident_id`) for the note. Extend the dict built in `api_outages` with `id`, `expected`, `expected_source` and `expected_rule_id`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest app/tests/test_api_quality.py app/tests/test_main_wiring.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/speedtest_app/api_quality.py app/speedtest_app/main.py \
        app/tests/test_api_quality.py app/tests/test_main_wiring.py
git commit -m "feat(api): mark a single closed outage expected by hand"
```

---

### Task 9: The filter across reads

**Files:**
- Modify: `app/speedtest_app/api_quality.py`, `main.py`, `api_quality_exports.py`, `report.py`
- Test: `app/tests/test_api_quality.py`, `test_main_wiring.py`, `test_exports.py`, `test_report.py`

**Interfaces:**
- Consumes: Tasks 3 and 4 filters.
- Produces: `?expected=all|exclude|only` on `/api/quality/incidents`, `/api/outages`, both CSV exports and the report; responses carry `expected_filter` and `expected_hidden`; shared helper `expected_mode(value) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
def test_incidents_endpoint_filters_expected(client):
    plain = _closed_incident(client, started_at="2026-09-21T01:00:00.000Z")
    planned = _closed_incident(client, started_at="2026-09-21T02:00:00.000Z",
                               expected=1, expected_source="rule")
    url = "/api/quality/incidents?from=2026-09-21T00:00&to=2026-09-21T23:00"

    body = client.get(url).json()
    assert [i["id"] for i in body["items"]] == [plain, planned]
    assert body["expected_filter"] == "all" and body["expected_hidden"] == 0

    body = client.get(url + "&expected=exclude").json()
    assert [i["id"] for i in body["items"]] == [plain]
    assert body["expected_hidden"] == 1

    assert [i["id"] for i in client.get(url + "&expected=only").json()["items"]] == [planned]
    assert [i["id"] for i in client.get(url + "&expected=krowa").json()["items"]] == [plain, planned]


def test_outages_downtime_follows_the_filter(client):
    # a 5-minute plain outage and a 4-minute expected one
    db = client.app_db_path
    record_connectivity(db, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(db, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    record_connectivity(db, is_up=False, now_iso="2026-09-21T03:00:00.000Z")
    record_connectivity(db, is_up=True, now_iso="2026-09-21T03:04:00.000Z")
    mark_connectivity_period_expected(db, started_at_iso="2026-09-21T03:00:00.000Z",
                                      expected=True, source="rule", rule_id=None)

    url = "/api/outages?from=2026-09-21T00:00&to=2026-09-21T05:00"
    assert client.get(url).json()["downtime_seconds"] == 540
    filtered = client.get(url + "&expected=exclude").json()
    assert filtered["downtime_seconds"] == 300
    assert filtered["expected_hidden"] == 1


def test_incident_csv_carries_the_flag(client):
    _closed_incident(client, expected=1, expected_source="rule")
    text = client.get("/api/quality/export/incidents.csv?from=2026-09-21T00:00&to=2026-09-21T23:00").text
    header = [line for line in text.splitlines() if not line.startswith("#")][0]
    assert "expected" in header and "expected_source" in header


def test_outage_csv_carries_the_flag(client):
    record_connectivity(client.app_db_path, is_up=False, now_iso="2026-09-21T01:00:00.000Z")
    record_connectivity(client.app_db_path, is_up=True, now_iso="2026-09-21T01:05:00.000Z")
    text = client.get("/api/export/outages.csv?from=2026-09-21T00:00&to=2026-09-21T23:00").text
    assert "expected" in text.splitlines()[0]


def test_report_separates_expected_downtime(client):
    _closed_incident(client, expected=1, expected_source="rule")
    html = client.get("/api/quality/report.html?from=2026-09-21T00:00&to=2026-09-21T23:00").text
    assert "spodziewane" in html
```

If `/api/outages` does not currently return `downtime_seconds`, read where `app.js` fills `q-downtime` and put the two figures on whichever endpoint feeds it, keeping the assertions above pointed at that endpoint.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest app/tests/test_api_quality.py app/tests/test_exports.py app/tests/test_report.py app/tests/test_main_wiring.py -v -k expected`
Expected: FAIL — `KeyError: 'expected_filter'`.

- [ ] **Step 3: Implement**

A shared reader so the three modes are parsed in exactly one place. It lives in
`quality_db.py`, beside `EXPECTED_FILTERS` and `expected_clause`, and both
`api_quality.py` and `main.py` import it from there — not two copies:

```python
def expected_mode(value: str | None) -> str:
    """`all` unless the caller clearly asked for something else.

    A typo must never hide an outage, so anything unrecognised reads as `all`.
    """
    mode = (value or "all").strip().lower()
    return mode if mode in {"all", "exclude", "only"} else "all"
```

`/api/quality/incidents` queries twice — once with the mode, once with `all` — and reports `expected_hidden = len(all) - len(filtered)`. `/api/outages` does the same and computes its downtime figures from the filtered rows only. Both CSVs add `expected` and `expected_source` columns after the existing ones, keeping the timezone comment line. The report adds an `expected` column to its incident table and one summary line (`"w tym spodziewane: …"`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest app/tests -v`
Expected: PASS — the whole suite, since this task touches every read path.

- [ ] **Step 5: Commit**

```bash
git add app/speedtest_app/api_quality.py app/speedtest_app/main.py \
        app/speedtest_app/api_quality_exports.py app/speedtest_app/report.py app/tests
git commit -m "feat(api): filter outages, exports and the report by the expected flag"
```

---

### Task 10: Settings panel for the rules

**Files:**
- Modify: `app/static/index.html`, `app/static/quality.js`
- Test: `app/tests/test_static_quality.py`

**Interfaces:**
- Consumes: Task 7's endpoints.
- Produces: element ids `q-expected-windows-tbody`, `q-expected-window-form`, `q-expected-window-name`, `q-expected-window-from`, `q-expected-window-to`, `q-expected-window-days`, `q-expected-window-target`, `q-expected-window-note`, `q-expected-window-add`.

- [ ] **Step 1: Write the failing test**

```python
def test_expected_window_settings_markup_exists():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for element_id in (
        "q-expected-windows-tbody", "q-expected-window-form", "q-expected-window-name",
        "q-expected-window-from", "q-expected-window-to", "q-expected-window-days",
        "q-expected-window-target", "q-expected-window-note", "q-expected-window-add",
    ):
        assert f'id="{element_id}"' in html


def test_expected_window_settings_are_wired():
    js = (STATIC / "quality.js").read_text(encoding="utf-8")
    assert "/api/quality/expected-windows" in js
    for element_id in ("q-expected-windows-tbody", "q-expected-window-add"):
        assert element_id in js


def test_expected_window_help_text_explains_containment():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "w całości" in html        # the containment rule, stated to the user
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest app/tests/test_static_quality.py -v -k expected`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add an "Okna serwisowe" section beside the existing schedule settings: a table of rules (name, hours, days, target, note, delete button) and a row of inputs plus an add button. Weekday checkboxes reuse the labels the schedule editor already uses. Include one line of help text: *"Awaria liczy się jako spodziewana tylko wtedy, gdy zmieści się w oknie w całości."*

In `quality.js`, follow the module's existing fetch/render style: `loadExpectedWindows()` on settings open, render rows, POST on add, DELETE on the row button, re-render after each call, and surface a failed request the way the other handlers in the file do.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest app/tests/test_static_quality.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/static/index.html app/static/quality.js app/tests/test_static_quality.py
git commit -m "feat(ui): manage expected maintenance windows in settings"
```

---

### Task 11: Dashboard badge and filter

**Files:**
- Modify: `app/static/index.html`, `app/static/app.js`
- Test: `app/tests/test_static_app_js.py`

**Interfaces:**
- Consumes: Task 8's `PATCH /api/outages/{id}/expected`, Task 9's `expected` parameter.
- Produces: element ids `q-hide-expected`, `q-expected-hidden-note`; outage rows carrying a badge and a mark/unmark control.

- [ ] **Step 1: Write the failing test**

```python
def test_dashboard_has_the_expected_filter():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="q-hide-expected"' in html
    assert 'id="q-expected-hidden-note"' in html


def test_dashboard_js_sends_the_filter_and_can_mark():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "expected=exclude" in js
    assert "/expected" in js and "PATCH" in js
    assert "expected_hidden" in js
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest app/tests/test_static_app_js.py -v -k expected`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add the checkbox above the outage list; when checked, append `&expected=exclude` to the outages request. `q-downtime` and `q-percent` read from that response, so they follow once the server filters. Render a badge (rule name, or "oznaczone ręcznie" when `expected_source === "manual"`) on each flagged row, a per-row button that `PATCH`es the opposite value, and the hidden-count line in `q-expected-hidden-note` when `expected_hidden > 0`.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest app/tests/test_static_app_js.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/static/index.html app/static/app.js app/tests/test_static_app_js.py
git commit -m "feat(ui): badge and filter expected outages on the dashboard"
```

---

### Task 12: Quality tab badge, toggle and timeline shading

**Files:**
- Modify: `app/static/index.html`, `app/static/quality.js`
- Test: `app/tests/test_static_quality.py`

**Interfaces:**
- Consumes: Task 8's `PATCH /api/quality/incidents/{id}/expected`, Task 9's filter.
- Produces: element ids `q-incidents-hide-expected`, `q-incident-expected-toggle`.

- [ ] **Step 1: Write the failing test**

```python
def test_incident_table_and_drawer_expose_the_flag():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="q-incidents-hide-expected"' in html
    assert 'id="q-incident-expected-toggle"' in html


def test_quality_js_marks_and_filters_incidents():
    js = (STATIC / "quality.js").read_text(encoding="utf-8")
    assert "q-incident-expected-toggle" in js
    assert "expected=exclude" in js
    assert "/expected" in js
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest app/tests/test_static_quality.py -v -k incident_table`
Expected: FAIL.

- [ ] **Step 3: Implement**

Badge in the incidents table row (the renderer around the `q-incidents-tbody` loop), a toggle button in the drawer that `PATCH`es and re-fetches the incident, the checkbox above the table driving `expected=exclude`, and — in the timeline renderer that already draws incident spans — a muted fill for a flagged incident instead of dropping it.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest app/tests -v`
Expected: PASS — full suite.

- [ ] **Step 5: Commit**

```bash
git add app/static/index.html app/static/quality.js app/tests/test_static_quality.py
git commit -m "feat(ui): mark and filter expected incidents in the quality tab"
```

---

## Done criteria

- `.venv/bin/python -m pytest app/tests -q` green.
- A rule covering 02:55–03:15 daily, created in settings, causes the next night's reboot to appear badged, to leave the downtime percentage unchanged under "pomiń spodziewane", and to send no mail.
- An outage that outlasts the window still counts and still mails.
- `docs/superpowers/specs/2026-09-23-expected-outages-design.md` §5 matches the implementation (amended in Task 5).
