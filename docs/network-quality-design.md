# Network quality monitoring — design spec

Status: binding design for implementing `NETWORK_QUALITY_PLAN.md` (stages 1–8, server side of 9).
This document is the authority for names, schema, formulas and API shapes. The plan is the
authority for *what* and *why*; when the plan is silent, this document decides.

All measurements are taken from the device running the container (initially the NAS, wired).
They describe the path NAS → router → internet and are not by themselves proof of ISP fault.

## 1. Conventions

- Timestamps stored as UTC ISO-8601 with `Z` suffix and millisecond precision:
  `2026-09-19T10:00:00.123Z` (helper `to_iso_z`). Display/exports convert to the local zone and
  state the zone name.
- Durations and scheduling use `time.monotonic()` / `time.perf_counter()`; wall clock only for
  the `started_at` label of a probe.
- A probe **attempt** always produces exactly one `probe_results` row. Nothing is inferred for
  ticks that did not run.
- Three orthogonal notions, never merged:
  - **outcome** of an attempt: `ok`, `timeout`, `error` (see §4).
  - **availability** of the connection: `up`, `down`, `no_data` (§8).
  - **quality**: `ok`, `degraded`, `unknown` (§8), driven by incidents (§7).
- Loss figures are computed only from `ok` + `timeout` attempts. `error` attempts are "no
  measurement" and are reported separately, never as loss and never as success.
- ICMP loss, TCP failures and UDP datagram loss are separate metrics with separate labels.
  RTT variability from ICMP/TCP is labelled "zmienność RTT", never "jitter" (jitter is reserved
  for iperf3 UDP receiver statistics).
- Legacy TCP history (`connectivity_checks`, `connectivity_periods`) is kept read-only and
  labelled `legacy_tcp`. It is never used to compute packet loss.
- Python 3.12, FastAPI, SQLite (WAL). No new heavy dependencies. `jinja2` is allowed for the
  HTML report. Tests: `pytest` + `pytest-asyncio` (asyncio_mode = auto), located in `app/tests/`.
- Every new module has a module docstring stating its responsibility. Public functions typed.
- UI strings in Polish. Code, identifiers and docs in English.

## 2. Module ownership

| Module (`app/speedtest_app/`) | Responsibility | Task |
|---|---|---|
| `db.py` | schema, migrations (`SCHEMA_VERSION = 2`), legacy accessors, settings + config change log | T1 |
| `quality_db.py` | accessors for all new tables (targets, probe results, sessions, incidents, aggregates, annotations, load tests, diagnostics, devices) | T1 |
| `probe_types.py` | enums + dataclasses shared by everything | T1 |
| `coverage.py` | monitor sessions lifecycle, observed intervals, coverage % | T1 |
| `icmp_probe.py`, `tcp_probe.py` | single-attempt probes returning `ProbeResult` | T2 |
| `probe_scheduler.py` | per-target loops, bounded concurrency, buffered batch writes | T2 |
| `stats.py` | pure statistics over probe rows (§6) | T3 |
| `incidents.py` | incident state machine (§7) | T3 |
| `aggregates.py` | hourly/daily bucket aggregation from raw rows (§6.4) | T3 |
| `dns_probe.py`, `https_probe.py` | DNS and HTTPS probes with stage timings and error categories | T4 |
| `iperf_udp.py` | iperf3 UDP/TCP command building, JSON validation, result dataclass | T5 |
| `availability.py` | availability/quality evaluator, feeds `connectivity_periods` + email | T6 |
| `quality_engine.py` | orchestration loops: scheduler start, incident evaluation, aggregation, session heartbeat | T6 |
| `diagnostics.py` | incident-triggered MTR with rate/concurrency limits | T7 |
| `load_tests.py` | scheduled iperf3 load tests, mutual exclusion, latency-under-load comparison | T7 |
| `api_quality.py` | `APIRouter` with all `/api/quality/*` and `/api/targets*` routes, CSV exports | T8 |
| `report.py` + `templates/report.html` | HTML report for the ISP | T8 |
| `retention.py` | retention policy, pruning, DB growth estimate | T9 |
| `ingest.py` + `api_ingest.py` | authenticated, idempotent import of results from other devices (stage 9 server side) | T11 |
| `static/*` | panel | T10 |

`main.py` only wires routers and startup/shutdown. Feature code never lives in `main.py`.

## 3. Schema v2 (exact DDL)

`ensure_db` runs migrations idempotently, guarded by `meta.schema_version`. Migration 1→2 adds
the tables below, never drops or rewrites legacy tables, and seeds default targets (§3.1). It is
wrapped in one transaction; on failure the DB stays at version 1.

```sql
CREATE TABLE IF NOT EXISTS probe_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('gateway','internet','dns','https','tcp')),
  protocol TEXT NOT NULL CHECK (protocol IN ('icmp','tcp','dns','https')),
  host TEXT NOT NULL,                       -- hostname, IP, or URL for https
  port INTEGER NULL,                        -- tcp/https
  interval_seconds REAL NOT NULL,
  timeout_ms INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  family_pref TEXT NOT NULL DEFAULT 'auto' CHECK (family_pref IN ('auto','ipv4','ipv6')),
  extra_json TEXT NULL,                     -- protocol specific (dns: {"qname":..,"resolver":..})
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS probe_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL DEFAULT 'nas',
  target_id INTEGER NOT NULL REFERENCES probe_targets(id) ON DELETE CASCADE,
  protocol TEXT NOT NULL,
  started_at TEXT NOT NULL,
  duration_ms REAL NOT NULL,                -- wall time of the whole attempt (monotonic)
  outcome TEXT NOT NULL CHECK (outcome IN ('ok','timeout','error')),
  rtt_ms REAL NULL,                         -- only for ok
  timeout_ms INTEGER NOT NULL,              -- threshold in force for this attempt
  resolved_ip TEXT NULL,
  ip_family INTEGER NULL CHECK (ip_family IN (4,6)),
  error_kind TEXT NULL,                     -- §4.3
  error_detail TEXT NULL,                   -- short, ≤ 200 chars
  stages_json TEXT NULL,                    -- https/dns/tcp stage timings, §4.2
  load_test_id INTEGER NULL,                -- set when a load test was running
  external_id TEXT NULL                     -- stage 9 idempotency key
);
CREATE INDEX IF NOT EXISTS idx_probe_results_target_time ON probe_results(target_id, started_at);
CREATE INDEX IF NOT EXISTS idx_probe_results_time ON probe_results(started_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_probe_results_external ON probe_results(device_id, external_id)
  WHERE external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS monitor_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL DEFAULT 'nas',
  started_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,               -- heartbeat, every 30 s
  ended_at TEXT NULL,
  end_reason TEXT NULL CHECK (end_reason IN ('shutdown','unclean')),
  app_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  changed_at TEXT NOT NULL,
  key TEXT NOT NULL,
  old_value TEXT NULL,
  new_value TEXT NULL,
  source TEXT NOT NULL CHECK (source IN ('ui','env','migration','api'))
);

CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id INTEGER NOT NULL REFERENCES probe_targets(id) ON DELETE CASCADE,
  protocol TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('outage','degraded')),
  started_at TEXT NOT NULL,                 -- start of first degraded window
  ended_at TEXT NULL,                       -- end of last degraded window, NULL while open
  closed_at TEXT NULL,                      -- when the engine closed it (after stabilization)
  close_reason TEXT NULL CHECK (close_reason IN ('recovered','no_data','shutdown')),
  window_seconds INTEGER NOT NULL,          -- resolution of boundaries
  probe_interval_seconds REAL NOT NULL,
  peak_loss_pct REAL NULL,
  peak_p95_rtt_ms REAL NULL,
  longest_fail_streak INTEGER NULL,
  windows_degraded INTEGER NOT NULL DEFAULT 0,
  summary_json TEXT NULL                    -- window verdict sequence (§7)
);
CREATE INDEX IF NOT EXISTS idx_incidents_time ON incidents(started_at);

CREATE TABLE IF NOT EXISTS probe_aggregates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id INTEGER NOT NULL REFERENCES probe_targets(id) ON DELETE CASCADE,
  protocol TEXT NOT NULL,
  bucket TEXT NOT NULL CHECK (bucket IN ('1h','1d')),
  bucket_start TEXT NOT NULL,               -- UTC, aligned
  attempts INTEGER NOT NULL,
  ok_count INTEGER NOT NULL,
  timeout_count INTEGER NOT NULL,
  error_count INTEGER NOT NULL,
  loss_pct REAL NULL,
  rtt_min_ms REAL NULL, rtt_p50_ms REAL NULL, rtt_p95_ms REAL NULL, rtt_p99_ms REAL NULL,
  rtt_max_ms REAL NULL, rtt_mean_ms REAL NULL, rtt_variation_ms REAL NULL,
  longest_fail_streak INTEGER NULL,
  percentiles_from_raw INTEGER NOT NULL DEFAULT 1,  -- 0 when raw rows were already pruned
  computed_at TEXT NOT NULL,
  UNIQUE(target_id, bucket, bucket_start)
);

CREATE TABLE IF NOT EXISTS annotations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  at TEXT NOT NULL,                         -- when the symptom was observed
  label TEXT NOT NULL,                      -- e.g. "zacięcie TV"
  note TEXT NULL,
  incident_id INTEGER NULL REFERENCES incidents(id) ON DELETE SET NULL,
  source TEXT NOT NULL DEFAULT 'user'       -- always 'user'; never a measurement
);

CREATE TABLE IF NOT EXISTS load_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  ended_at TEXT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('iperf_udp','iperf_tcp','speedtest')),
  direction TEXT NOT NULL CHECK (direction IN ('upload','download','both')),
  server TEXT NULL,
  params_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running','ok','error','skipped')),
  error TEXT NULL,
  result_json TEXT NULL,                    -- validated summary (§10)
  raw_json TEXT NULL                        -- raw iperf3 output
);

CREATE TABLE IF NOT EXISTS diagnostics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NULL REFERENCES incidents(id) ON DELETE SET NULL,
  target_id INTEGER NULL REFERENCES probe_targets(id) ON DELETE SET NULL,
  tool TEXT NOT NULL,                       -- 'mtr'
  started_at TEXT NOT NULL,
  duration_ms REAL NULL,
  status TEXT NOT NULL CHECK (status IN ('ok','error','timeout')),
  error TEXT NULL,
  result_json TEXT NULL,
  raw_output TEXT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,                      -- 'nas', or agent-provided id
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('nas','macos','other')),
  token_hash TEXT NULL,                     -- sha256 of ingest token, stage 9
  created_at TEXT NOT NULL,
  last_seen_at TEXT NULL,
  last_skew_ms REAL NULL
);
```

### 3.1 Seeded targets (migration 1→2, only when `probe_targets` is empty)

| name | kind | protocol | host | port | interval | timeout_ms | enabled |
|---|---|---|---|---|---|---|---|
| `gateway` | gateway | icmp | value of env `GATEWAY_HOST` or `''` | – | 1.0 | 1000 | 1 only if host non-empty |
| `cloudflare-dns` | internet | icmp | `1.1.1.1` | – | 1.0 | 1000 | 1 |
| `google-dns` | internet | icmp | `8.8.8.8` | – | 1.0 | 1000 | 1 |
| `quad9-dns` | internet | icmp | `9.9.9.9` | – | 1.0 | 1000 | 1 |
| `legacy-tcp` | tcp | tcp | current `connect_target` setting (host part) | `connect_default_port` | current `connect_interval_seconds` | current `ping_timeout_ms` | 1 |
| `dns-system` | dns | dns | `example.com` (qname), resolver: `system` in `extra_json` | – | 30 | 2000 | 1 |
| `https-cloudflare` | https | https | `https://cloudflare.com/cdn-cgi/trace` | 443 | 60 | 5000 | 1 |
| `https-google` | https | https | `https://www.google.com/generate_204` | 443 | 60 | 5000 | 1 |

Do not assume the container's default route is the home router: `gateway` stays disabled until
the user sets its host (UI or `GATEWAY_HOST`). The `devices` table is seeded with
`('nas','NAS (kabel)','nas')`.

## 4. Probes

### 4.1 Common types (`probe_types.py`)

```python
class Outcome(StrEnum): OK = "ok"; TIMEOUT = "timeout"; ERROR = "error"
class Protocol(StrEnum): ICMP = "icmp"; TCP = "tcp"; DNS = "dns"; HTTPS = "https"

@dataclass(frozen=True, slots=True)
class ProbeTarget:
    id: int; name: str; kind: str; protocol: Protocol; host: str; port: int | None
    interval_seconds: float; timeout_ms: int; enabled: bool; family_pref: str
    extra: dict[str, Any]

@dataclass(frozen=True, slots=True)
class ProbeResult:
    target_id: int; protocol: Protocol; started_at: str; duration_ms: float
    outcome: Outcome; timeout_ms: int
    rtt_ms: float | None = None; resolved_ip: str | None = None; ip_family: int | None = None
    error_kind: str | None = None; error_detail: str | None = None
    stages: dict[str, float] | None = None; device_id: str = "nas"
    load_test_id: int | None = None; external_id: str | None = None
```

Each probe module exposes `async def probe(target: ProbeTarget, *, timeout_ms: int | None = None) -> ProbeResult`.
A probe never raises; every failure becomes `outcome=error` with an `error_kind`.
A probe never exceeds `timeout_ms + 250 ms` of wall time (hard `asyncio.wait_for` guard;
guard expiry → `outcome=error, error_kind='exec_timeout'`, not a network timeout).

### 4.2 Semantics per protocol

- **ICMP** (`icmp_probe.py`): one echo request, `rtt_ms` = reply time. Implementation order:
  1. unprivileged ICMP datagram socket (`SOCK_DGRAM`, `IPPROTO_ICMP` / `IPPROTO_ICMPV6`);
  2. raw socket if `CAP_NET_RAW`; 3. `ping -c 1 -W <s> -n` subprocess (parse `time=`).
  The chosen method is detected once at startup, logged, and exposed in `/api/quality/status`
  as `icmp_method` (`dgram`|`raw`|`ping`|`unavailable`). If unavailable, ICMP targets produce
  `error/permission` rows (visible, never silent). Identity: id = pid & 0xFFFF, seq per target.
  Destination unreachable → `outcome=error, error_kind='icmp_unreachable'` (it is a reply,
  not a loss). No reply within `timeout_ms` → `timeout`.
- **TCP** (`tcp_probe.py`): `asyncio.open_connection` to host:port. `rtt_ms` = connect time.
  Refused → `error/tcp_refused`; reset → `error/tcp_reset`; DNS failure → `error/dns`;
  no SYN-ACK within timeout → `timeout`. Resolution is part of the attempt; `resolved_ip`
  recorded; `stages = {"dns_ms":..., "connect_ms":...}`.
- **DNS** (`dns_probe.py`): resolve `extra["qname"]` (default `example.com`) type A (and AAAA
  when `family_pref != ipv4`) using `dnspython` async resolver against `extra["resolver"]`
  (`"system"` → `/etc/resolv.conf`; else an IP). `rtt_ms` = query time. Error kinds:
  `dns_nxdomain`, `dns_servfail`, `dns_refused`, `dns_no_answer`, `dns_error`; no answer
  in time → `timeout`. `resolved_ip` = first answer.
- **HTTPS** (`https_probe.py`): HTTP GET of `host` (a URL) using `httpx.AsyncClient` with
  per-stage timings measured manually: `dns_ms` (getaddrinfo), `connect_ms` (TCP),
  `tls_ms` (handshake), `ttfb_ms` (first byte). `rtt_ms` = total time to first byte.
  Error kinds: `dns`, `tcp_refused`, `tcp_reset`, `tls` (cert/handshake), `http_status`
  (status ≥ 400; store status in `error_detail`), `http_protocol`; stage exceeding budget →
  `timeout`. Success requires a status < 400. A TCP success with TLS failure is `error/tls`,
  never `ok`.

### 4.3 `error_kind` vocabulary (closed set)

`dns`, `dns_nxdomain`, `dns_servfail`, `dns_refused`, `dns_no_answer`, `dns_error`,
`tcp_refused`, `tcp_reset`, `tcp_error`, `tls`, `http_status`, `http_protocol`,
`icmp_unreachable`, `permission`, `exec`, `exec_timeout`, `resolver_unavailable`.

## 5. Scheduler (`probe_scheduler.py`)

- One asyncio task per enabled target. Targets reloaded from DB every 10 s; changed
  interval/timeout/enabled restarts only that target's loop.
- Tick alignment: `next_tick = start_monotonic + n * interval`. If an attempt overruns its
  interval, the next tick is the next future multiple; skipped ticks are counted in
  `scheduler_stats.skipped_ticks[target_id]` and logged at debug. There is never a backlog.
- Global `asyncio.Semaphore(PROBE_MAX_CONCURRENCY)` (default 16) bounds concurrent probes;
  ICMP attempts to different targets never wait on each other's timeout because the semaphore
  is only held while the probe runs and the pool is larger than the target count.
- Results go to an in-memory buffer flushed by `quality_db.insert_probe_results(rows)` every
  `probe_flush_seconds` (default 5) or when the buffer holds `probe_flush_max` rows (default
  500), and on shutdown. A failed flush retries with exponential backoff (1,2,4,8, max 30 s)
  while keeping at most `probe_buffer_hard_max` (default 20 000) rows; beyond that the oldest
  rows are dropped and the drop count is logged and exposed in status. Buffer loss on an
  abrupt stop is therefore at most `probe_flush_seconds` of data; documented in README.
- The scheduler exposes `recent(target_id, seconds)` → list of in-memory results for the
  availability evaluator and incident engine, so they never wait on the DB flush.

## 6. Statistics (`stats.py`) — pure functions over `Iterable[ProbeResult | Mapping]`

```python
@dataclass(frozen=True)
class ProbeStats:
    attempts: int; ok: int; timeouts: int; errors: int
    loss_pct: float | None        # timeouts / (ok + timeouts) * 100; None if ok+timeouts == 0
    rtt_min_ms, rtt_p50_ms, rtt_p95_ms, rtt_p99_ms, rtt_max_ms, rtt_mean_ms: float | None
    rtt_variation_ms: float | None  # mean |rtt_i - rtt_{i-1}| over consecutive ok samples
    rtt_spread_ms: float | None     # p95 - p50
    longest_fail_streak: int        # max run of consecutive timeouts; error rows are skipped
    first_at: str | None; last_at: str | None
```

- Percentiles: nearest-rank on the sorted `ok` RTTs, `idx = ceil(p/100 * n) - 1`. Never
  interpolate, never average percentiles of sub-periods.
- `rtt_*` are `None` when `ok < min_samples` (default 1 for reporting, incident thresholds use
  their own minimum).
- Loss over multiple periods is re-derived from summed counters (`sum timeouts / sum (ok+timeouts)`),
  never as an average of percentages. `stats.merge_counters(list[ProbeStats])` implements this
  and sets `rtt_*` to `None` unless raw rows are supplied.
- Windows: `stats.window(rows, window_seconds, end_at)` and `stats.tumbling_windows(rows,
  window_seconds, start_at, end_at)` produce `(window_start, window_end, ProbeStats)`; windows
  with `attempts == 0` are returned with `attempts=0` (no data) — never dropped.

### 6.4 Aggregates (`aggregates.py`)

`compute_bucket(rows, bucket, bucket_start)` → row for `probe_aggregates` from raw rows only.
`aggregate_range(db, target_id, bucket, start, end)` upserts every complete bucket. A daily
bucket is computed from the day's raw rows (not from hourly rows). If raw rows for a bucket were
pruned, the existing aggregate row is left as-is; when no aggregate exists and no raw rows
exist, nothing is written (missing = no data). `percentiles_from_raw = 0` is set by the
retention job when it prunes the raw rows a bucket was built from.

## 7. Incidents (`incidents.py`)

State machine per `(target_id, protocol)`, evaluated on tumbling windows of `window_seconds`
(setting `incident_window_seconds`, default 10) using the scheduler's in-memory results.

Settings (all configurable, persisted in `settings`):

| key | default | meaning |
|---|---|---|
| `incident_window_seconds` | 10 | evaluation window |
| `incident_min_samples` | 5 | fewer `ok+timeout` attempts in a window → `unknown` |
| `incident_loss_pct_threshold` | 20.0 | window loss ≥ → degraded |
| `incident_outage_loss_pct` | 100.0 | window loss ≥ → outage |
| `incident_rtt_p95_ms_threshold` | 150.0 | window p95 RTT ≥ → degraded |
| `incident_fail_streak_threshold` | 3 | consecutive timeouts ≥ → degraded |
| `incident_open_windows` | 2 | consecutive degraded windows to open |
| `incident_stabilization_seconds` | 60 | continuous healthy time to close |
| `incident_no_data_close_seconds` | 300 | continuous unknown → close with `no_data` |

Window classification: `unknown` if `ok + timeouts < min_samples`; else `outage` if
`loss_pct ≥ outage_loss_pct`; else `degraded` if any threshold hit; else `healthy`.

Transitions:

- `CLOSED` → `PENDING` on a degraded/outage window; `PENDING` → `OPEN` after
  `incident_open_windows` consecutive degraded/outage windows (the incident `started_at` is the
  start of the first of them); a healthy window in `PENDING` resets to `CLOSED`; `unknown`
  windows in `PENDING` are ignored (neither count nor reset).
- `OPEN`: every degraded/outage window extends `ended_at` to its end and updates peaks;
  kind escalates `degraded → outage` if any window is outage (never de-escalates).
  Healthy windows accumulate `healthy_seconds`; when `healthy_seconds ≥ stabilization_seconds`
  the incident closes with `close_reason='recovered'`, `closed_at` = now, `ended_at` unchanged
  (last degraded window end). A degraded window resets `healthy_seconds` to 0.
  `unknown` windows accumulate `unknown_seconds`; at `≥ no_data_close_seconds` the incident
  closes with `close_reason='no_data'` and `ended_at` = last degraded window end.
- Engine shutdown closes open incidents with `close_reason='shutdown'` (the next session
  starts fresh; a still-degraded link opens a new incident).

The engine is a pure class `IncidentEngine(settings, clock)` with
`feed(target_id, protocol, window_start, window_end, stats) -> list[IncidentEvent]`
(events: `opened`, `updated`, `closed`) so tests are deterministic. Persistence is done by the
caller (`quality_engine.py`) via `quality_db`.

`summary_json` holds the sequence of window verdicts (`[[window_start, verdict, loss_pct,
p95], ...]`, capped at 720 entries) — the "przebieg" of the incident.

## 8. Availability & quality (`availability.py`)

Evaluated every `availability_eval_seconds` (default 5) over the last `availability_window_seconds`
(default 10) using enabled targets with `kind in ('internet','tcp')` (gateway excluded — the
gateway answering does not mean the internet works):

- `up` if at least one such target had an `ok` attempt in the window;
- `down` if the window holds ≥ `incident_min_samples` `ok+timeout` attempts across those
  targets and none is `ok`;
- `no_data` otherwise (monitor just started, all probes erroring, ICMP unavailable, disabled).

`connectivity_periods` is written from this state (`is_up = 1` for `up`, `0` for `down`; no row
change on `no_data`, but the current open period is **ended** when the state becomes `no_data`
so that unobserved time is not attributed to the previous state). Email notifications keep the
existing rule (on recovery, outage ≥ `SMTP_MIN_OUTAGE_SECONDS`).

Quality: `degraded` if any incident is open on an internet/tcp target, `unknown` if availability
is `no_data`, else `ok`. Gateway incidents are reported separately as `lan_degraded: true`.

Legacy `connectivity_loop` is removed; `speedtest_loop` stays.

## 9. Sessions & coverage (`coverage.py`)

- On startup: close any session with `ended_at IS NULL` using `ended_at = last_seen_at`,
  `end_reason = 'unclean'`; then insert a new session. Heartbeat updates `last_seen_at` every
  30 s. On clean shutdown set `ended_at = now, end_reason = 'shutdown'`.
- `observed_intervals(db, start, end, test_type='ping')` = union of session intervals
  `[started_at, coalesce(ended_at, last_seen_at + 30s)]` clipped to the range, minus
  `blocked_periods` of that type. Returns sorted, non-overlapping `[(start, end)]`.
- `coverage(db, start, end)` → `{"total_seconds", "observed_seconds", "coverage_pct",
  "gaps": [{"from","to","reason"}]}` where reason is `not_running` or `disabled`/`schedule`.
- `/api/report/quality` (legacy endpoint) is changed to clip every down period to observed
  intervals and to return `observed_seconds`, `coverage_pct`, `gaps`. Downtime % is
  `downtime / observed_seconds` (and `downtime_percent_of_range` kept for compatibility).
  A DB with no sessions (pre-v2 history) has coverage `None` and the legacy behaviour is
  flagged with `"coverage_known": false` — never extrapolated.

## 10. Load tests (`iperf_udp.py`, `load_tests.py`)

- `iperf_udp.build_command(params) -> list[str]`: `iperf3 -c <server> -p <port> -J -t <dur>
  [-u -b <bitrate> -l <datagram_len>] [-R for download]`. Both directions = two sequential runs.
- `iperf_udp.parse_result(stdout, direction, kind) -> LoadTestResult | ValidationError`:
  validates JSON structure; requires `end.sum` (UDP: `lost_packets`, `packets`, `lost_percent`,
  `jitter_ms`; TCP: `sum_sent`/`sum_received` bits_per_second). Missing fields, `error` key in
  JSON, or `packets == 0` → `status='error'` with reason; never a synthetic 0 % loss. Intervals
  are kept in `result_json["intervals"]` (per-second `lost_packets`, `packets`, `jitter_ms`).
- Receiver statistics are the authoritative loss for the direction; the sender's view is stored
  under `sender` for reference.
- `load_tests.py`: schedule from settings (`load_test_enabled` default false,
  `load_test_interval_seconds` default 21600, `load_test_server`, `load_test_port` 5201,
  `load_test_udp_bitrate` default `10M`, `load_test_duration_seconds` 10, `load_test_datagram_len`
  1200, `load_test_directions` `both`). Unconfigured server → `status='skipped'` with
  `error='not_configured'` (feature unconfigured, not an outage). Uses the runtime speedtest lock:
  never overlaps a speed test; probes keep running during the test and rows get
  `load_test_id`. `compare_latency(db, load_test_id)` returns ICMP stats for the test span vs.
  the 5 minutes before it, per target.

## 11. Diagnostics (`diagnostics.py`)

- Trigger: incident `opened` event (or `updated` after it has been open ≥ 120 s and no
  diagnostic yet) → `mtr --report --json -c 10 -n <target host>` via
  `network_tools._run_subprocess`, plus the gateway host if configured.
- Limits: `diagnostics_max_concurrent` 1, `diagnostics_min_interval_seconds` 300 per target,
  `diagnostics_max_per_incident` 3. Over-limit triggers are recorded as
  `status='error', error='rate_limited'` rows so the omission is visible.
- Stored with `incident_id`, raw output and parsed hops. Interpretation text in the API
  (`hypotheses`) is phrased as hypotheses ("możliwa przyczyna"), never as a verdict.

## 12. API contract (`api_quality.py`, prefix `/api`)

All time query params accept the same formats as `parse_range`; responses give times in local
ISO (like existing endpoints) plus `"tz": "<zone name>"` at the top level.

- `GET /api/targets` → `{"items":[ProbeTarget as dict + "last_result": {...} | null]}`
- `POST /api/targets` body `{name, kind, protocol, host, port?, interval_seconds?, timeout_ms?,
  enabled?, family_pref?, extra?}` → target; `PUT /api/targets/{id}` partial; `DELETE`.
  Validation: interval ≥ 0.2 s (≥ 1.0 unless `diagnostic_mode` setting is true), timeout ≤
  interval*1000 or 60000, host via `network_tools._validate_hostname` (or URL for https).
- `GET /api/quality/status` → `{"now","tz","measured_from":"NAS (kabel)","availability":
  "up|down|no_data","quality":"ok|degraded|unknown","lan_degraded":bool,"icmp_method",
  "open_incidents":[incident],"session":{"started_at","last_seen_at"},"coverage_24h_pct",
  "scheduler":{"buffered_rows","dropped_rows","skipped_ticks"}}`
- `GET /api/quality/stats?from&to` → `{"range","tz","coverage":{...},"targets":[{"target":{...},
  "stats":ProbeStats,"note":"ICMP echo loss"|"TCP connect failures"|...}],"legacy_tcp":
  {"attempts","failures"} | null}`
- `GET /api/quality/timeline?from&to&bucket_seconds=60` → per target list of
  `{"t","attempts","ok","timeouts","errors","loss_pct","p50","p95","max"}`; plus `"incidents"`,
  `"gaps"`, `"load_tests"`, `"annotations"` for a shared axis. Bucket ≥ 10 s; the server caps
  points at 2000 per target by raising the bucket.
- `GET /api/quality/incidents?from&to` and `GET /api/quality/incidents/{id}` (detail includes
  `windows` from `summary_json`, `diagnostics`, `related_targets` stats for the same span,
  `load_tests` overlapping, `annotations`).
- `POST /api/quality/annotations {at,label,note?,incident_id?}`, `DELETE /api/quality/annotations/{id}`.
- `GET /api/quality/coverage?from&to`.
- `GET /api/quality/load-tests?from&to`, `GET /api/quality/load-tests/{id}`, `POST /api/quality/load-tests/run`.
- `GET /api/quality/diagnostics?from&to&incident_id`.
- `GET /api/quality/report.html?from&to` → printable report (§13).
- `GET /api/quality/export/probes.csv?from&to[&target_id]`, `.../incidents.csv`,
  `.../aggregates.csv?bucket=1h|1d`, `.../load-tests.csv`.
- Settings for §7, §8, §10, §11, §14 are exposed through the existing `/api/config`
  (`ConfigResponse`/`ConfigUpdate` extended) — T8 adds the fields; T6 reads them.

## 13. Report (`report.py`, `templates/report.html`)

Self-contained HTML (inline CSS, inline SVG charts, no CDN), A4 print CSS. Sections in order:
1. Header: range (local time + zone), generated at, app version, device ("NAS, kabel"), method
   summary (ICMP echo 1 s, TCP connect, DNS, HTTPS) and configuration (targets table).
2. Data coverage: observed/total seconds, %, list of gaps with reasons. Retention note when
   raw rows for part of the range were pruned (§14).
3. Availability summary (from `connectivity_periods` clipped to observed time) and per-target
   counters/statistics table (ICMP, TCP, DNS, HTTPS separately; legacy TCP in its own table
   without any loss column).
4. Incidents table + per-incident detail (windows, diagnostics, annotations marked "zgłoszenie
   użytkownika").
5. Load tests (UDP loss per direction, jitter, latency under load vs. baseline).
6. Charts: loss % and p95 RTT per target over time (SVG, from the same bucket function as
   `/api/quality/timeline` so numbers agree).
7. Limitations (fixed text): sampling resolution, NAS path only, MTR hop silence, no direction
   attribution, timeouts as configured thresholds.

Every number in the report comes from the same functions that serve the API and CSV; the report
test asserts equality with `/api/quality/stats` for the same range.

## 14. Retention (`retention.py`)

Settings: `retention_raw_days` (default 14), `retention_aggregate_days` (365),
`retention_incident_days` (730), `retention_load_test_raw_days` (90, `raw_json` nulled after),
`retention_diagnostics_days` (365). Job runs hourly: ensures aggregates exist for every bucket
about to lose raw rows (calls `aggregates.aggregate_range` first), then deletes in batches of
5000 with short transactions; marks `percentiles_from_raw = 0` on affected buckets.
The raw-delete cutoff is `now - retention_raw_days` floored to the start of its UTC day, so no
bucket is ever half-deleted before it has been aggregated (a bucket straddling a literal cutoff
would otherwise be re-aggregated later from surviving rows only). Raw rows therefore survive up
to 24 h longer than configured, never less.
`estimate_growth(db)` reports rows/day and bytes/day from the last 24 h.
Exports and report state "surowe dane niedostępne (retencja)" for ranges before
`now - retention_raw_days`; the probes CSV for such a range is empty with a header comment line.

## 15. Stage 9 server side (`ingest.py`, `api_ingest.py`)

- `POST /api/ingest/results` with header `Authorization: Bearer <token>`; body
  `{"device_id","sent_at","results":[ProbeResult dict with external_id]}`; targets matched by name.
  Idempotent through the unique `(device_id, external_id)` index (`INSERT OR IGNORE`); the
  response returns `{"accepted","duplicates","rejected":[...]}`.
- Device tokens: `POST /api/devices` creates a device and returns the token once; stored as
  sha256. Only `kind != 'nas'` devices can ingest.
- Clock skew: the server stores `last_skew_ms = server_now - sent_at` on the device; the
  timeline offsets nothing — it reports skew so a human can judge.

## 16. Testing conventions

- `app/tests/conftest.py` provides `db_path` (tmp file, `ensure_db` applied), `client`
  (FastAPI `TestClient` with `DATA_DIR` pointed to a tmp dir; app imported after env is set),
  `frozen_clock` helper for monotonic/wall clocks.
- Tests never touch the real network except those marked `@pytest.mark.network` (skipped by
  default; enabled with `-m network`).
- Legacy-DB fixture: `tests/fixtures/app_v1.sql` creates a v1 schema with sample rows; the
  migration test asserts row counts before/after and `schema_version == 2`.
- Run: `cd app && ../.venv/bin/python -m pytest -q`.
