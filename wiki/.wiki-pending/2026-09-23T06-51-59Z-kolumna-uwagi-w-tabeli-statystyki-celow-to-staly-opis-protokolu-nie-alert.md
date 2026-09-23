---
title: "Kolumna Uwagi w tabeli Statystyki celow to staly opis protokolu, nie alert"
evidence: "conversation"
evidence_type: "conversation"
capture_kind: "chat-only"
suggested_action: "create"
suggested_pages: []
captured_at: "2026-09-23T06-51-59Z"
captured_by: "in-session-agent"
propagated_from: null
---

## Kolumna "Uwagi" w tabeli "Statystyki celów" (Speedtest, /quality)

"Uwagi" (`note` w payloadzie API) is a STATIC per-protocol label, not an alert and not a
health signal. It renders identically whether the target is healthy or failing. Source:
`app/speedtest_app/quality_views.py:39-44`:

```python
PROTOCOL_NOTES: dict[str, str] = {
    "icmp": "Utrata odpowiedzi ICMP echo",
    "tcp": "Nieudane zestawienia TCP",
    "dns": "Błędy/timeouty zapytań DNS",
    "https": "Błędy/timeouty HTTPS (DNS, TCP, TLS, HTTP)",
}
```

Emitted per target at `quality_views.py:337` as `"note": PROTOCOL_NOTES.get(str(target.protocol), "")`.
Asserted in `app/tests/test_api_quality.py:158,170-172`.

### Why it exists
The column labels the UNIT OF FAILURE for that row, because "Strata %" means a different
physical event per protocol. 0.19% ICMP loss and 0.19% TCP failure are not comparable
measurements. The docstring above the dict states this explicitly: "the label is part of
the measurement, because ICMP loss and TCP failures are not the same thing."

Reading guide per protocol:
- icmp — missing echo reply; may be ICMP rate-limiting on the router/host rather than real
  data-plane packet loss.
- tcp — TCP handshake did not complete.
- dns — DNS query errored or timed out.
- https — full chain DNS -> TCP -> TLS -> HTTP; most links, so highest baseline error rate.
  A higher % on an https target does not imply a worse network than a lower % on icmp.

### Gotcha: loss excludes errors
`Strata %` = timeouts / (ok + timeouts). `error` attempts are counted and reported
separately, never as loss and never as success — `app/speedtest_app/stats.py:9-11`.
Observed consequence in production data: the `legacy-tcp` target showed 69 Błędy with
0.00% Strata, because it had only 1 timeout out of 31344 attempts. Errors mean
"no measurement taken", not "packet lost". Read the Błędy column separately from Strata %.

Also: an empty window yields `attempts == 0` and `loss_pct is None` — "no data", never
"0 % loss" (`stats.py:12-13`).

### Sum row
The `Razem` footer row (`app/static/quality.js:604-612`) fills counters only (attempts, ok,
timeouts, errors, re-derived loss) and prints literal `–` for p50/p95/p99/Max/Zmienność RTT,
with `Σ liczników` in the Uwagi cell. Percentiles are nearest-rank on raw samples and are
never interpolated nor averaged across sub-periods or across targets; loss over several
periods is re-derived from summed counters via `merge_counters`, never averaged as
percentages (`stats.py:14-17`). The sum row also filters out targets with
`data_source === "none"` before summing.
