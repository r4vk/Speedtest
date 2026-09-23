---
title: "SQLite WAL deferred BEGIN fails instantly with database is locked when it reads before it writes"
evidence: "conversation"
evidence_type: "conversation"
capture_kind: "chat-only"
suggested_action: "create"
suggested_pages: []
captured_at: "2026-09-19T17-11-17Z"
captured_by: "in-session-agent"
propagated_from: null
---

---
title: "SQLite WAL deferred BEGIN fails instantly with database is locked when it reads before it writes"
orphaned_at: "2026-09-19T17-08-17Z"
reason: "headless: cwd unconfigured AND pointer = none/missing"
---

## SQLite WAL: a deferred `BEGIN` that reads before it writes fails instantly with "database is locked"

In WAL mode, `busy_timeout` does NOT protect a read-then-write transaction. A
plain (deferred) `BEGIN` takes a read snapshot at its first SELECT. If another
connection commits before the transaction's first write, SQLite cannot upgrade
the snapshot to a write lock and returns `SQLITE_BUSY` **immediately, without
waiting** — `PRAGMA busy_timeout=30000` and `sqlite3.connect(timeout=30)` are
both bypassed. Python surfaces it as:

```
sqlite3.OperationalError: database is locked
```

`busy_timeout` only applies when a transaction waits for a write lock it has not
yet contradicted with a stale snapshot. `BEGIN IMMEDIATE` acquires the write
lock up front, so the wait becomes the documented busy_timeout wait.

### The broken pattern

```python
with db_conn(db_path) as conn:
    conn.execute("BEGIN")                                      # deferred
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.execute("INSERT INTO settings(...) VALUES (?,?,?) ON CONFLICT ...", ...)  # <- BUSY here
    conn.execute("COMMIT")
```

Fix: `conn.execute("BEGIN IMMEDIATE")`. Nothing else changes.

### Observed in r4vk.org/Speedtest (v0.1.1, 2026-09-19)

`PUT /api/config` answered HTTP 500 intermittently. `set_setting()` is called
once per changed field, so the ~47-field settings form opened ~47 separate
read-then-write transactions while the probe scheduler flushed probe results on
a 1-second interval.

Diagnostic signature that identified it before any log was read:

- Failures returned in **~16 ms**; successes took **200–3400 ms**. A fast 500 is
  an immediate exception, NOT a lock-wait timeout. A `busy_timeout` exhaustion
  would have taken 30 s.
- Failure rate scaled with the number of fields in the payload: 1/3/6 fields →
  0 failures; 12 fields → 2/6 failures; 24 and 47 fields → 6/6 failures.
- Single-field PUTs (`{"retention_raw_days": 14}`, `{"gateway_host": "..."}`)
  passed 8/8 every time, which is why the bug read as random.
- Bisecting the payload by halves failed on BOTH halves — proof it was not a
  bad field value but the number of transactions.

### Reproducing it in a test (it is easy, and the test is not flaky)

A background thread committing single INSERTs in autocommit is enough; the
failure appeared within 0.07 s of starting a 300-iteration `set_setting` loop.

```python
def writer():
    while not stop.is_set():
        with db_conn(db_path) as conn:
            conn.execute("INSERT INTO config_changes(...) VALUES (?,?,?,?,?)", (...))

threading.Thread(target=writer).start()
for i in range(300):
    set_setting(db_path, "connect_target", f"host-{i}")   # raises without BEGIN IMMEDIATE
```

### Gotcha worth remembering

A pre-existing test asserted "a second writer waits instead of raising database
locked" and passed — because the test itself hand-wrote `BEGIN IMMEDIATE` in
both threads. It never exercised the deferred-BEGIN production path. A passing
concurrency test proves only the pattern it spells out, not the one the
production code uses.

### Rule of thumb

Every explicit transaction in a WAL SQLite app that may write should be
`BEGIN IMMEDIATE`. Write-first single statements are safe under deferred BEGIN
(the lock is taken at the statement, so busy_timeout applies), but making them
all IMMEDIATE costs nothing and removes the class of bug.
