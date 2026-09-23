# Expected outages — design spec

Status: binding design. Extends `docs/network-quality-design.md`; where the two disagree about
names, schema or API shapes for the feature described here, this document decides.

An outage that happens every night at 03:00 because the router reboots is not news. It still
lands in the outage list, still drags the downtime percentage down, and still sends a mail —
which is how a person learns to ignore outage mails. This spec adds a way to say, after the
fact, "that one was expected", and a rule that says it once for a recurring window.

## 1. Scope

In scope:

- A recurring **expected window** (name, local `HH:MM` range, weekdays, optional target).
- An `expected` flag on both representations of an outage, written once when the outage closes.
- Manual marking and unmarking of a single closed outage, overriding whatever the rules said.
- Filtering by that flag in the UI, the exports and the report.
- Suppressing the recovery mail for an outage that fits an expected window.

Out of scope, deliberately:

- **No retroactive rewrite.** Creating or editing a rule never updates rows already written.
  The flag records what the rules said at the moment the outage closed, and stays that way.
- **No filtering of raw probe samples.** Loss, RTT and the per-target counters stay raw. Ranges
  older than the raw retention are served from hourly aggregates, and a 20-minute window cannot
  be honestly subtracted from an hourly bucket; a number that is exact for recent ranges and
  invented for older ones is worse than a raw one.
- **No suppression of probing.** Expected windows never stop a probe. `ping_schedules` /
  `speed_schedules` already do that, and they leave a hole in the data; this feature exists
  precisely to keep measuring through the reboot and still call it expected.

## 2. The two outage tables

"Outage" means two different rows in this codebase, and the feature has to mark both:

| | `connectivity_periods` | `incidents` |
|---|---|---|
| What | internet up/down, legacy path | degradation per `(target_id, protocol)` |
| Written by | `AvailabilityTracker` (`availability.py`) | `QualityEngine` (`quality_engine.py`) |
| Surfaced in | dashboard outage list, `q-downtime`, `q-percent`, `outages.csv` | quality tab table and drawer, `incidents.csv`, report HTML |
| Sends the recovery mail | yes (`_on_up`) | no |

Marking only `incidents` would leave the dashboard's downtime percentage untouched, which is the
number the feature exists to fix. Both tables get the same three columns and the same semantics.

## 3. Schema (migration 2 → 3)

`SCHEMA_VERSION` becomes `3`; `_migrate_2_to_3` runs in one transaction, like `_migrate_1_to_2`.

```sql
CREATE TABLE IF NOT EXISTS expected_windows (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  time_from  TEXT NOT NULL,                     -- 'HH:MM', local wall clock
  time_to    TEXT NOT NULL,                     -- 'HH:MM', local wall clock
  days       TEXT NOT NULL,                     -- JSON array, 0=Monday .. 6=Sunday
  target_id  INTEGER NULL REFERENCES probe_targets(id) ON DELETE CASCADE,
  enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  note       TEXT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expected_windows_enabled ON expected_windows(enabled);
```

Then, for each of `incidents` and `connectivity_periods`:

```sql
ALTER TABLE <table> ADD COLUMN expected         INTEGER NOT NULL DEFAULT 0 CHECK (expected IN (0,1));
ALTER TABLE <table> ADD COLUMN expected_source  TEXT NULL CHECK (expected_source IN ('rule','manual'));
ALTER TABLE <table> ADD COLUMN expected_rule_id INTEGER NULL REFERENCES expected_windows(id) ON DELETE SET NULL;
```

SQLite (verified on 3.50.4) accepts `NOT NULL` with a default, `CHECK`, and `REFERENCES` with a
`NULL` default in `ADD COLUMN`, so the constraints live in the schema rather than in Python.

```sql
CREATE INDEX IF NOT EXISTS idx_incidents_expected_time ON incidents(expected, started_at);
CREATE INDEX IF NOT EXISTS idx_connectivity_periods_expected ON connectivity_periods(expected, started_at);
```

Column meanings:

- `expected` — `0` or `1`. Every row written before the migration is `0`, and stays `0`.
- `expected_source` — `'rule'` when a window matched at close time, `'manual'` when a person
  said so, `NULL` when `expected = 0` and nobody has touched it. A manual `expected = 0` **does**
  set `expected_source = 'manual'`: that is what makes "no, this one was real" stick.
- `expected_rule_id` — the window that matched, for display ("planned: nightly router reboot").
  Deleting a rule nulls it and leaves `expected` alone; history does not change.

`target_id` on a rule scopes it to one probe target. `NULL` means every target, and is the only
value that can match a `connectivity_periods` row, which has no target.

## 4. Matching

New module `app/speedtest_app/expected_windows.py`. Pure, in the style of `incidents.py`: no
database, no network, no clock of its own. The caller passes the rules and the two timestamps.

```python
@dataclass(frozen=True)
class ExpectedWindow:
    id: int
    name: str
    time_from: str            # 'HH:MM'
    time_to: str              # 'HH:MM'
    days: frozenset[int]      # 0=Monday .. 6=Sunday
    target_id: int | None
    note: str | None

def parse_windows(rows: Iterable[Mapping[str, Any]]) -> list[ExpectedWindow]: ...

def match(
    windows: Sequence[ExpectedWindow],
    started_at: datetime,
    ended_at: datetime,
    target_id: int | None = None,
) -> ExpectedWindow | None: ...
```

Rules of the match, in order:

1. **Closed outages only.** `ended_at` is required. An open outage is never expected — we cannot
   yet know whether it will stay inside the window. The flag is decided at close time (§5).
2. **Scope.** A rule with `target_id = NULL` matches anything. A rule with a `target_id` matches
   only that target, and never a `connectivity_periods` row.
3. **Local wall clock.** Both timestamps are converted to the local zone (`time_utils.local_tz`).
   `days` is taken from the local weekday of `started_at`. A rule written as `03:00` means 03:00
   on the clock on the wall, before and after a DST change.
4. **Containment.** The window is expanded into concrete local intervals; the outage matches only
   if `window_start <= started_at` **and** `ended_at <= window_end`. An outage that starts at
   02:58 and ends at 06:30 does not match a 02:55–03:15 window — a real multi-hour failure that
   happens to begin during the reboot must not disappear.
5. **Overnight windows.** `time_from > time_to` (e.g. `23:50`–`00:10`) means the window crosses
   midnight; the interval starting on the previous local day is considered too, and `days` refers
   to the day the window *starts* on.
6. **First match wins**, ordered by `id`. Rules are few and a second match would not change the
   outcome, only the label.
7. A disabled rule (`enabled = 0`) is not passed to `match` at all.
8. **DST edges**, stated rather than left to the platform. On the spring-forward day a local time
   the clock skips (02:00–03:00 in Europe/Warsaw) does not exist: a window instance whose start
   falls in the gap is skipped for that day, and an outage simply does not match. On the
   autumn day the window occurs twice; both instances are built and either can match. This
   follows from building concrete local intervals per day rather than comparing `HH:MM` strings,
   which is what `_is_blocked_by_schedule` does and why that helper is not reused.

Degenerate input is not a match and never an exception: unparsable `days`, a malformed `HH:MM`,
`ended_at` before `started_at`. A broken rule must not be able to stop an outage from being
recorded.

## 5. Where the flag is written

Two write points, both on a path that is already issuing an `UPDATE`. No extra statement, no
extra transaction.

**`quality_engine.py`, on a `closed` incident event.** The engine already writes
`INCIDENT_CLOSE_FIELDS` in one `update_incident`. The rules are read at close time — one small
query, not a cache refreshed with the thresholds, because incidents close rarely — and matched
against the incident's `started_at`/`ended_at` and its `target_id`; `expected`,
`expected_source='rule'` and `expected_rule_id` join that same field set when a rule matches. No
match leaves the defaults. A rule therefore holds from the moment it is saved: one created at
03:02, mid-reboot, still marks that night's outage when it closes.

**`availability.py`, in `_on_up`.** The tracker knows `self._outage_started_at` and the recovery
timestamp. Match with `target_id = None`; on a hit:

- write the three columns onto the `connectivity_periods` row being closed, and
- skip the mail, logging at INFO which rule suppressed it and how long the outage was.

The suppression check sits next to the existing `smtp_min_outage_seconds` check, and shares its
shape: decide, log the reason, return. An outage that lasts past the window still mails.

A failure to read the rules is logged and treated as "no rules": the mail goes out and the row
is written unflagged. Losing a mail matters more than sending one too many.

## 6. Manual marking

- `PATCH /api/quality/incidents/{id}/expected` — body `{"expected": true|false, "note": "..."}`
- `PATCH /api/outages/{id}/expected` — same body, for a `connectivity_periods` row

Both set `expected` and `expected_source='manual'`, leave `expected_rule_id` untouched, and
return the updated row. Both refuse a row that is still open (`ended_at IS NULL`) with `409`, and
an unknown id with `404`.

The `note` is optional and is written to `annotations`, the table that already exists for exactly
this kind of human remark: `label='expected'`, `at` = the row's `started_at`, `source='user'`,
and `incident_id` set for the incident case. A `connectivity_periods` row has no column to
reference, so its annotation carries only the timestamp — enough to show on the timeline, and the
reason `note` is not duplicated as a column on either table.

`'manual'` always beats `'rule'`: nothing recomputes the flag after the row is closed.

## 7. Rules API

```
GET    /api/quality/expected-windows        -> {"windows": [...]}
POST   /api/quality/expected-windows        -> {"window": {...}}   201
PUT    /api/quality/expected-windows/{id}   -> {"window": {...}}
DELETE /api/quality/expected-windows/{id}   -> {"ok": true}
```

Validation (422 on failure): `name` non-empty; `time_from`/`time_to` matching `^\d{2}:\d{2}$`
with a valid hour and minute; `days` a non-empty array of distinct integers 0–6; `target_id`
either `NULL` or an existing target. `time_from == time_to` is rejected — a zero-length window is
always a mistake, and read as a 24-hour one it would silently mark everything.

Every mutation writes a `config_changes` row, as the other settings mutations do.

## 8. Filtering

A single query parameter, `expected`, on the endpoints that list outages:

| Value | Meaning |
|---|---|
| `all` (default) | everything, flag included in the payload |
| `exclude` | `WHERE expected = 0` |
| `only` | `WHERE expected = 1` |

Applied to: `/api/quality/incidents`, `/api/outages`, `/api/report/quality`,
`/api/export/incidents.csv` and `/api/export/outages.csv`. An unknown value falls back to `all`.

**Not** the printable report (`/api/quality/report.html`). It is the copy handed to an ISP, and a
total that silently omits rows is a total nobody can reconcile against the raw data. There an
expected outage stays in both tables, labelled, with its own line in the summary (§9).

The downtime figures live on `/api/report/quality`, not on `/api/outages` — that is where
`q-downtime` and `q-percent` are filled from — and it recomputes them from the filtered set, so
"pomiń spodziewane" moves the number the user is actually looking at. The list endpoints state
the filter used and the count suppressed, so a filtered view can never be mistaken for a clean
night:

```json
{"range": {}, "expected_filter": "exclude", "expected_hidden": 7, "items": []}
```

`expected_hidden` is `0` under `all`, and counts the rows the other two modes dropped.

Both CSV exports gain `expected` and `expected_source` columns and keep their existing timezone
comment line.

## 9. UI

**Settings — "Okna serwisowe" (expected windows).** A table of rules with add, enable/disable and
delete: name, from, to, weekday checkboxes, target select (default "wszystkie"), note. Placed next
to the existing schedule settings, whose weekday convention it shares. A disabled rule is shown as
disabled rather than silently doing nothing; changing a rule's hours is delete-and-add, which is
honest about what it does to history — the rows already flagged keep the verdict the old rule gave
them either way (§1).

**Dashboard (`app.js`).** Outage rows carry a badge with the rule name (or "oznaczone ręcznie").
A "pomiń spodziewane" checkbox drives `expected=exclude`; `q-downtime` and `q-percent` follow the
filtered response. When rows are hidden, the list shows a one-line footer: "ukryto 7 spodziewanych".

**Quality tab (`quality.js`).** The same badge in the incidents table, the same checkbox above it,
and a toggle in the incident drawer that calls the `PATCH` endpoint and refreshes the row. The
timeline shades an expected incident in a muted tone rather than hiding it — a gap with no
explanation is worse than a marked one.

**Report HTML.** An `expected` column in the incidents table and a line in the summary: total
downtime, of which expected.

## 10. Testing

New `app/tests/test_expected_windows.py`, pure-logic, following `test_incidents.py`:

- containment: inside → match; overlapping either edge → no match; equal bounds → match
- overnight window `23:50`–`00:10`, including the weekday of the starting day
- DST, per §4 rule 8: on the spring-forward day a window instance starting inside the skipped
  hour is not built and nothing matches; on the autumn day both instances of the repeated window
  are built and an outage in either matches
- weekday scoping: a Monday-only rule ignores Sunday's outage
- target scoping: a rule for target 3 ignores target 4 and ignores `target_id=None` callers
- open outage (`ended_at is None`) never matches
- malformed rule (bad `days` JSON, bad `HH:MM`, reversed timestamps) → `None`, no exception

Extensions:

- `test_incidents.py` / `test_quality_engine.py`: a closed incident inside a window is written
  with `expected=1, expected_source='rule', expected_rule_id=<id>`; one that overruns is not
- `test_availability.py`: mail suppressed for a contained outage, sent for one that overruns,
  sent when reading the rules raises; the closed period carries the flag
- `test_api_quality.py`: rules CRUD including the validation failures of §7; `expected` filter in
  all three modes; `PATCH` sets `'manual'`; `409` on an open row; `404` on an unknown id
- `test_exports.py` / `test_report.py`: new columns and the summary line
- `test_migration.py`: 2 → 3 on a populated v2 database; existing rows read `expected = 0`;
  the migration is idempotent on an already-v3 database
- `test_static_app_js.py` / `test_static_quality.py`: the drift guards this repo already keeps
  for new controls

## 11. Retention

`expected_windows` rows are configuration, not measurements: retention never deletes them.
Deleting an outage row under `retention_incident_days` takes its flag with it, as it should.
