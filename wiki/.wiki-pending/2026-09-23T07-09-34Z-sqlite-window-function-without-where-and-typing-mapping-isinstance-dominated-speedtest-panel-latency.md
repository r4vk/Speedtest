---
title: "SQLite window-function-without-WHERE and typing.Mapping isinstance dominated Speedtest panel latency"
evidence: "conversation"
evidence_type: "conversation"
capture_kind: "chat-only"
suggested_action: "create"
suggested_pages: []
captured_at: "2026-09-23T07-09-34Z"
captured_by: "in-session-agent"
propagated_from: null
---

Three measured causes of the Speedtest quality panel taking ~10.6 s per refresh (2026-09-23, branch `main`, ~9.7M `probe_results` rows / 1.5 GB, 8 targets, 14-day raw retention, 24 h panel range).

## 1. A SQLite window function with no WHERE scans the whole table

`quality_db.last_result_per_target` — the only DB work behind `GET /api/targets` — was:

```sql
SELECT * FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY target_id ORDER BY started_at DESC, id DESC) AS rn
  FROM probe_results
) WHERE rn = 1
```

`EXPLAIN QUERY PLAN`: `SCAN probe_results USING INDEX idx_probe_results_target_time` + `USE TEMP B-TREE FOR LAST 2 TERMS OF ORDER BY`. Measured **9.9 s, warm, repeatable** — cost scales with the archive, not with the number of targets, and the panel polls it on every refresh. The `WHERE rn = 1` is applied *after* the window materialises every row; SQLite does not push it into the partition.

Replacement: one indexed seek per target against `idx_probe_results_target_time (target_id, started_at)`:

```sql
SELECT * FROM probe_results WHERE target_id = ?
ORDER BY started_at DESC, id DESC LIMIT 1
```

**9.912 s → 0.002 s** (~5000×). Regression guard asserts the plan contains neither `SCAN probe_results` nor `TEMP B-TREE`; `quality_db.last_result_plans()` exists solely so the test can `EXPLAIN QUERY PLAN` what production runs.

Generalisation: `ROW_NUMBER() OVER (PARTITION BY k ...)` filtered to rank 1 is the "latest row per group" idiom, but in SQLite it is only correct-and-fast when the outer filter can restrict the input. With a small, known key set, N indexed `LIMIT 1` seeks beat one window pass by orders of magnitude.

## 2. `isinstance(x, typing.Mapping)` is an ABC subclass check, per call

`stats._field` used `from typing import Mapping` and `isinstance(row, Mapping)`. cProfile over 86 400 rows: 433 118 `_field` calls → `typing._GenericAlias.__instancecheck__` → `__subclasscheck__` → `_abc._abc_subclasscheck`, **0.396 s of 0.733 s total (54 %)**. Fix: `from collections.abc import Mapping` plus a `type(row) is dict` fast path first, since rows arrive from `sqlite3` as plain dicts.

## 3. Timestamps parsed three times per row

`stats.bucket_rows` called `_timed(rows)` for the range, then `compute_stats(bucket)` re-ran `_timed` inside *every* bucket, then a third `_timed(rows)` picked the trailing partial bucket. Verified by monkeypatching `stats.parse_dt`: **360 parses for 120 rows**. Rows already arrive `ORDER BY started_at ASC, id ASC`, so the re-sorting was pure waste. Fix: `_stats_from_timed` / `_tumbling_from_timed` take `(datetime, row)` pairs; `_timed` runs once per range. Bucketing 1.250 s → 0.475 s.

## Rejected hypothesis: covering index

Adding `idx_probe_results_target_time_metrics (target_id, started_at, outcome, rtt_ms, error_kind)` cost 8.9 s to build and **+470 MB** (1505 → 1974 MB). SQLite's planner kept the narrower existing index anyway; the query time difference was inside cache-warming noise (0.699 → 0.475 s cold, 0.446 → 0.450 s warm). Not adopted.

## Rejected hypothesis: connection churn

`db.db_conn` opens a fresh `sqlite3.connect` and runs 4 PRAGMAs (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=30000`) per accessor call, and one `/timeline` request makes 13+ of them. Measured **0.33 ms each** — 100 open+PRAGMA+close cycles in 0.033 s. Not a contributor; no pooling added.

## Narrow projection

`stats.compute_stats`, `stats.bucket_rows` and `quality_views.error_kinds_histogram` read only `started_at`, `outcome`, `rtt_ms`, `error_kind`. The endpoints selected all 16 columns (including `stages_json`, `error_detail`) for the whole range, twice per refresh — once for `/api/quality/stats`, once for `/api/quality/timeline`. Added `quality_db.query_probe_metrics` (same rows, same order, `METRIC_COLUMNS` projection); only `probes.csv` via `iter_probe_results` still needs whole rows. 691 200 rows: 1.459 s → 0.467 s, peak RSS 204 → 131 MB. Note the win is Python object construction, not disk — the planner still visits each table row.

## Result (FastAPI TestClient, same seeded DB, `git archive HEAD` as the before)

| endpoint | before | after |
|---|---|---|
| `/api/targets` | 10.05 s | 0.00 s |
| `/api/quality/stats` | 2.11 s | 0.97 s |
| `/api/quality/timeline` | 2.79 s | 1.14 s |
| serial total | 14.96 s | 2.11 s |
| wall clock, 5 parallel (what the panel does) | 10.58 s | 3.40 s |

539 tests pass. Remaining lever not taken: `/stats` and `/timeline` fetch the identical row set in the same refresh; sharing it needs a freshness-keyed single-flight cache, whose invalidation (`MAX(id)` is safe for appends but blind to retention deletes) was judged disproportionate to a further ~2×.
