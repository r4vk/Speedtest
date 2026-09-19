"""Deterministic tests for the incident diagnostics (design spec §11).

No mtr is ever executed: the runner gets a fake subprocess runner returning
canned mtr `--json` reports. `hypotheses` is pure and tested on its own.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from speedtest_app import quality_db
from speedtest_app.diagnostics import (
    DiagnosticsRunner,
    DiagnosticsSettings,
    hypotheses,
    parse_mtr_json,
)
from speedtest_app.incidents import IncidentEvent, IncidentState
from speedtest_app.probe_types import Protocol
from speedtest_app.time_utils import to_iso_z

NOW = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)
TARGET_HOST = "203.0.113.5"
GATEWAY_HOST = "192.168.1.1"


# ---------------------------------------------------------------------------
# mtr fixtures
# ---------------------------------------------------------------------------


def hop(count: int, host: str | None, loss: float | None, avg: float = 10.0) -> dict[str, Any]:
    entry: dict[str, Any] = {"count": count, "host": host if host is not None else "???"}
    if loss is not None:
        entry["Loss%"] = loss
    entry.update({"Snt": 10, "Last": avg, "Avg": avg, "Best": avg - 1, "Wrst": avg + 1, "StDev": 0.4})
    return entry


def mtr_report(*hubs: dict[str, Any]) -> str:
    return json.dumps({"report": {"mtr": {"dst": TARGET_HOST, "tests": 10}, "hubs": list(hubs)}})


CLEAN_GATEWAY = mtr_report(hop(1, GATEWAY_HOST, 0.0, avg=1.2))
LOSS_FROM_HOP_3 = mtr_report(
    hop(1, GATEWAY_HOST, 0.0, avg=1.2),
    hop(2, "198.51.100.1", 0.0, avg=8.0),
    hop(3, "198.51.100.9", 40.0, avg=30.0),
    hop(4, TARGET_HOST, 40.0, avg=31.0),
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeMtr:
    """Canned `(rc, stdout, stderr)` per host, optionally held by a gate."""

    def __init__(self, responses: dict[str, Any], gate: asyncio.Event | None = None) -> None:
        self.responses = responses
        self.gate = gate
        self.calls: list[tuple[str, list[str], float]] = []

    async def __call__(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        host = argv[-1]
        self.calls.append((host, list(argv), timeout))
        if self.gate is not None:
            await self.gate.wait()
        response = self.responses[host]
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def hosts(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def drain(rounds: int = 60) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def seed(db_path: str) -> tuple[int, int]:
    """A target and an open incident on it; returns `(target_id, incident_id)`."""
    target = quality_db.insert_target(
        db_path,
        name="internet",
        kind="internet",
        protocol=Protocol.ICMP,
        host=TARGET_HOST,
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=True,
    )
    incident_id = quality_db.insert_incident(
        db_path,
        target_id=target.id,
        protocol="icmp",
        kind="degraded",
        started_at=to_iso_z(NOW),
        window_seconds=10,
        probe_interval_seconds=1.0,
    )
    return target.id, incident_id


def new_incident(db_path: str, target_id: int) -> int:
    """Another incident on an existing target."""
    return quality_db.insert_incident(
        db_path,
        target_id=target_id,
        protocol="icmp",
        kind="degraded",
        started_at=to_iso_z(NOW),
        window_seconds=10,
        probe_interval_seconds=1.0,
    )


def make_event(
    event_type: str,
    *,
    target_id: int,
    incident_id: int,
    at: datetime = NOW,
) -> IncidentEvent:
    state = IncidentState(
        status="open",
        incident_id=incident_id,
        started_at=to_iso_z(NOW),
        kind="degraded",
        peak_loss_pct=40.0,
    )
    return IncidentEvent(
        type=event_type,  # type: ignore[arg-type]
        target_id=target_id,
        protocol="icmp",
        state=state,
        close_reason=None,
        window_start=at - timedelta(seconds=10),
        window_end=at,
    )


def make_runner(
    db_path: str,
    *,
    mtr: FakeMtr,
    gateway: str | None = None,
    clock: FakeClock | None = None,
    **overrides: Any,
) -> DiagnosticsRunner:
    settings = DiagnosticsSettings(**overrides)
    return DiagnosticsRunner(
        db_path,
        settings_getter=lambda: settings,
        gateway_host_getter=lambda: gateway,
        subprocess_runner=mtr,
        clock=clock if clock is not None else FakeClock(),
        wall_clock=lambda: NOW,
    )


def rows(db_path: str, incident_id: int | None = None) -> list[dict[str, Any]]:
    return quality_db.query_diagnostics(db_path, incident_id=incident_id)


# ---------------------------------------------------------------------------
# parse_mtr_json
# ---------------------------------------------------------------------------


class TestParseMtrJson:
    def test_hops_are_parsed(self) -> None:
        hops = parse_mtr_json(LOSS_FROM_HOP_3)
        assert [h["hop"] for h in hops] == [1, 2, 3, 4]
        assert hops[0]["host"] == GATEWAY_HOST
        assert hops[2]["loss_pct"] == pytest.approx(40.0)
        assert hops[3]["avg_ms"] == pytest.approx(31.0)

    def test_a_silent_hop_has_no_host(self) -> None:
        hops = parse_mtr_json(mtr_report(hop(1, None, 100.0)))
        assert hops[0]["host"] is None

    def test_a_missing_loss_is_unknown_never_zero(self) -> None:
        hops = parse_mtr_json(mtr_report(hop(1, GATEWAY_HOST, None)))
        assert hops[0]["loss_pct"] is None

    def test_garbage_has_no_hops(self) -> None:
        assert parse_mtr_json("mtr: command not found") == []


# ---------------------------------------------------------------------------
# hypotheses (pure)
# ---------------------------------------------------------------------------


def _phrasing_is_hypothetical(lines: list[str]) -> bool:
    return all(
        line.startswith("Możliwa przyczyna:") or line.startswith("Uwaga:") for line in lines
    )


class TestHypotheses:
    def test_loss_on_the_first_hop_points_at_the_local_network(self) -> None:
        hops = parse_mtr_json(
            mtr_report(
                hop(1, GATEWAY_HOST, 30.0),
                hop(2, "198.51.100.1", 30.0),
                hop(3, TARGET_HOST, 30.0),
            )
        )
        lines = hypotheses(hops, TARGET_HOST, None)
        assert lines
        assert _phrasing_is_hypothetical(lines)
        assert any("lokalnej" in line for line in lines)

    def test_loss_from_the_second_hop_with_a_clean_gateway_points_further(self) -> None:
        hops = parse_mtr_json(LOSS_FROM_HOP_3)
        lines = hypotheses(hops, TARGET_HOST, parse_mtr_json(CLEAN_GATEWAY))
        assert _phrasing_is_hypothetical(lines)
        assert any("dostawcy" in line for line in lines)
        assert not any("wina" in line.lower() for line in lines)

    def test_a_silent_middle_hop_with_a_clean_last_hop_is_not_loss(self) -> None:
        hops = parse_mtr_json(
            mtr_report(
                hop(1, GATEWAY_HOST, 0.0),
                hop(2, None, 100.0),
                hop(3, TARGET_HOST, 0.0),
            )
        )
        lines = hypotheses(hops, TARGET_HOST, None)
        assert _phrasing_is_hypothetical(lines)
        assert any("nie dowodzi" in line for line in lines)

    def test_a_silent_destination_is_not_read_as_a_broken_path(self) -> None:
        hops = parse_mtr_json(
            mtr_report(
                hop(1, GATEWAY_HOST, 0.0),
                hop(2, "198.51.100.1", 0.0),
                hop(3, TARGET_HOST, 100.0),
            )
        )
        lines = hypotheses(hops, TARGET_HOST, None)
        assert _phrasing_is_hypothetical(lines)
        assert any("nie odpowiada na ICMP" in line for line in lines)

    def test_a_lossy_gateway_is_read_even_when_the_target_path_is_clean(self) -> None:
        hops = parse_mtr_json(mtr_report(hop(1, GATEWAY_HOST, 0.0), hop(2, TARGET_HOST, 0.0)))
        lossy_gateway = parse_mtr_json(mtr_report(hop(1, GATEWAY_HOST, 25.0)))

        lines = hypotheses(hops, TARGET_HOST, lossy_gateway)

        assert _phrasing_is_hypothetical(lines)
        assert any("w sieci lokalnej lub na routerze" in line for line in lines)

    def test_no_loss_at_all_states_only_what_was_measured(self) -> None:
        hops = parse_mtr_json(mtr_report(hop(1, GATEWAY_HOST, 0.0), hop(2, TARGET_HOST, 0.0)))
        lines = hypotheses(hops, TARGET_HOST, None)
        assert _phrasing_is_hypothetical(lines)
        assert any("nie pokazał strat" in line for line in lines)

    def test_without_hops_nothing_is_claimed(self) -> None:
        lines = hypotheses([], TARGET_HOST, None)
        assert _phrasing_is_hypothetical(lines)
        assert any("brak" in line for line in lines)

    def test_never_blames_the_operator(self) -> None:
        for hops in (
            parse_mtr_json(LOSS_FROM_HOP_3),
            parse_mtr_json(mtr_report(hop(1, GATEWAY_HOST, 50.0), hop(2, TARGET_HOST, 50.0))),
            [],
        ):
            for line in hypotheses(hops, TARGET_HOST, None):
                assert "wina" not in line.lower()


# ---------------------------------------------------------------------------
# on_incident_event
# ---------------------------------------------------------------------------


async def test_an_opened_incident_runs_mtr_for_the_target(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")})
    runner = make_runner(db_path, mtr=mtr)

    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None)
    await drain()

    stored = rows(db_path, incident_id)
    assert len(stored) == 1
    assert stored[0]["tool"] == "mtr"
    assert stored[0]["status"] == "ok"
    assert stored[0]["target_id"] == target_id
    assert stored[0]["duration_ms"] is not None
    assert stored[0]["raw_output"] == LOSS_FROM_HOP_3
    result = json.loads(stored[0]["result_json"])
    assert [h["hop"] for h in result["hops"]] == [1, 2, 3, 4]
    assert result["hypotheses"]
    assert any("Możliwa" in line for line in result["hypotheses"])
    assert mtr.calls[0][1] == ["mtr", "--report", "--json", "-c", "10", "-n", TARGET_HOST]
    assert mtr.calls[0][2] == 90.0


async def test_the_gateway_is_diagnosed_alongside_the_target(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, ""), GATEWAY_HOST: (0, CLEAN_GATEWAY, "")})
    runner = make_runner(db_path, mtr=mtr, gateway=GATEWAY_HOST)

    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None)
    await drain()

    stored = rows(db_path, incident_id)
    assert len(stored) == 2
    roles = {json.loads(row["result_json"])["role"]: row for row in stored}
    assert set(roles) == {"gateway", "target"}
    assert json.loads(roles["gateway"]["result_json"])["host"] == GATEWAY_HOST
    assert set(mtr.hosts) == {TARGET_HOST, GATEWAY_HOST}
    # the target's hypotheses may use the gateway's clean result
    assert any("dostawcy" in line for line in json.loads(roles["target"]["result_json"])["hypotheses"])


async def test_a_gateway_equal_to_the_target_is_not_diagnosed_twice(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")})
    runner = make_runner(db_path, mtr=mtr, gateway=TARGET_HOST)

    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None)
    await drain()

    assert mtr.hosts == [TARGET_HOST]
    assert len(rows(db_path, incident_id)) == 1


async def test_the_incident_loop_is_never_blocked_by_a_running_mtr(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    gate = asyncio.Event()
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")}, gate=gate)
    runner = make_runner(db_path, mtr=mtr)

    await asyncio.wait_for(
        runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None),
        timeout=1.0,
    )
    await drain()
    assert rows(db_path, incident_id) == []  # still running, nothing written yet
    assert runner.running == 1

    gate.set()
    await drain()
    assert len(rows(db_path, incident_id)) == 1
    assert runner.running == 0


async def test_a_second_incident_while_one_runs_is_rate_limited(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    other = quality_db.insert_target(
        db_path,
        name="dns",
        kind="dns",
        protocol=Protocol.ICMP,
        host="198.51.100.20",
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=True,
    )
    other_incident = quality_db.insert_incident(
        db_path,
        target_id=other.id,
        protocol="icmp",
        kind="degraded",
        started_at=to_iso_z(NOW),
        window_seconds=10,
        probe_interval_seconds=1.0,
    )
    gate = asyncio.Event()
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")}, gate=gate)
    runner = make_runner(db_path, mtr=mtr, max_concurrent=1)

    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None)
    await drain()
    await runner.on_incident_event(
        make_event("opened", target_id=other.id, incident_id=other_incident), None
    )
    await drain()

    limited = rows(db_path, other_incident)
    assert len(limited) == 1
    assert limited[0]["status"] == "error"
    assert limited[0]["error"] == "rate_limited"
    assert limited[0]["tool"] == "mtr"
    assert mtr.hosts == [TARGET_HOST]

    gate.set()
    await drain()


async def test_a_second_run_inside_the_minimum_interval_is_rate_limited(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")})
    clock = FakeClock()
    runner = make_runner(db_path, mtr=mtr, clock=clock, min_interval_seconds=300)

    event = make_event("opened", target_id=target_id, incident_id=incident_id)
    await runner.on_incident_event(event, None)
    await drain()
    clock.now += 60.0
    await runner.on_incident_event(event, None)
    await drain()

    stored = rows(db_path, incident_id)
    assert [row["status"] for row in stored] == ["ok", "error"]
    assert stored[1]["error"] == "rate_limited"
    assert mtr.hosts == [TARGET_HOST]

    clock.now += 300.0
    await runner.on_incident_event(event, None)
    await drain()
    assert mtr.hosts == [TARGET_HOST, TARGET_HOST]


async def test_no_more_than_max_per_incident_runs(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")})
    clock = FakeClock()
    runner = make_runner(
        db_path, mtr=mtr, clock=clock, min_interval_seconds=0, max_per_incident=2
    )
    event = make_event("opened", target_id=target_id, incident_id=incident_id)

    for _ in range(3):
        clock.now += 1.0
        await runner.on_incident_event(event, None)
        await drain()

    stored = rows(db_path, incident_id)
    assert [row["status"] for row in stored] == ["ok", "ok", "error"]
    assert stored[-1]["error"] == "rate_limited"
    assert len(mtr.hosts) == 2


async def test_an_updated_event_waits_for_the_incident_to_persist(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")})
    runner = make_runner(db_path, mtr=mtr)

    early = make_event("updated", target_id=target_id, incident_id=incident_id, at=NOW + timedelta(seconds=60))
    await runner.on_incident_event(early, None)
    await drain()
    assert rows(db_path, incident_id) == []
    assert mtr.hosts == []

    late = make_event("updated", target_id=target_id, incident_id=incident_id, at=NOW + timedelta(seconds=180))
    await runner.on_incident_event(late, None)
    await drain()
    assert mtr.hosts == [TARGET_HOST]


async def test_an_updated_event_does_not_repeat_an_existing_diagnostic(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")})
    clock = FakeClock()
    runner = make_runner(db_path, mtr=mtr, clock=clock, min_interval_seconds=0)

    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None)
    await drain()
    late = make_event("updated", target_id=target_id, incident_id=incident_id, at=NOW + timedelta(seconds=300))
    await runner.on_incident_event(late, None)
    await drain()

    assert mtr.hosts == [TARGET_HOST]


async def test_a_closed_event_diagnoses_nothing(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")})
    runner = make_runner(db_path, mtr=mtr)

    await runner.on_incident_event(make_event("closed", target_id=target_id, incident_id=incident_id), None)
    await drain()

    assert mtr.hosts == []
    assert rows(db_path, incident_id) == []


async def test_disabled_diagnostics_run_nothing(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")})
    runner = make_runner(db_path, mtr=mtr, enabled=False)

    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None)
    await drain()

    assert mtr.hosts == []
    assert rows(db_path, incident_id) == []


async def test_a_timeout_is_recorded_as_such(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: TimeoutError("Timeout (90s): mtr --report --json")})
    runner = make_runner(db_path, mtr=mtr)

    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None)
    await drain()

    stored = rows(db_path, incident_id)
    assert len(stored) == 1
    assert stored[0]["status"] == "timeout"
    assert "Timeout" in stored[0]["error"]
    assert stored[0]["result_json"] is None


async def test_a_broken_tool_is_an_error_row(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: FileNotFoundError("mtr")})
    runner = make_runner(db_path, mtr=mtr)

    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None)
    await drain()

    stored = rows(db_path, incident_id)
    assert len(stored) == 1
    assert stored[0]["status"] == "error"
    assert "mtr" in stored[0]["error"]


async def test_an_unparsable_report_is_an_error_row(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: (1, "mtr: no such host", "resolve failed")})
    runner = make_runner(db_path, mtr=mtr)

    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None)
    await drain()

    stored = rows(db_path, incident_id)
    assert stored[0]["status"] == "error"
    assert stored[0]["error"]
    assert stored[0]["raw_output"] == "mtr: no such host"


async def test_a_report_without_hops_says_so(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: (0, mtr_report(), "")})
    runner = make_runner(db_path, mtr=mtr)

    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None)
    await drain()

    stored = rows(db_path, incident_id)
    assert stored[0]["status"] == "error"
    assert stored[0]["error"] == "no hops reported"


async def test_an_incident_refused_a_slot_is_still_diagnosed_later(db_path: str) -> None:
    """A refusal is not a decision: the second incident of an outage gets its mtr."""
    target_id, incident_id = seed(db_path)
    other = quality_db.insert_target(
        db_path,
        name="dns",
        kind="dns",
        protocol=Protocol.ICMP,
        host="198.51.100.20",
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=True,
    )
    other_incident = quality_db.insert_incident(
        db_path,
        target_id=other.id,
        protocol="icmp",
        kind="degraded",
        started_at=to_iso_z(NOW),
        window_seconds=10,
        probe_interval_seconds=1.0,
    )
    gate = asyncio.Event()
    mtr = FakeMtr(
        {TARGET_HOST: (0, LOSS_FROM_HOP_3, ""), "198.51.100.20": (0, LOSS_FROM_HOP_3, "")},
        gate=gate,
    )
    clock = FakeClock()
    runner = make_runner(db_path, mtr=mtr, clock=clock, max_concurrent=1, min_interval_seconds=300)

    # both incidents open in the same window; the second loses the only slot
    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None)
    await drain()
    await runner.on_incident_event(
        make_event("opened", target_id=other.id, incident_id=other_incident), None
    )
    await drain()
    gate.set()
    await drain()

    refused = rows(db_path, other_incident)
    assert [row["error"] for row in refused] == ["rate_limited"]
    assert mtr.hosts == [TARGET_HOST]

    # an `updated` window inside the interval adds no second refusal row
    clock.now += 100.0
    await runner.on_incident_event(
        make_event("updated", target_id=other.id, incident_id=other_incident, at=NOW + timedelta(seconds=600)),
        None,
    )
    await drain()
    assert len(rows(db_path, other_incident)) == 1
    assert mtr.hosts == [TARGET_HOST]

    # once the interval has passed, the incident is diagnosed after all
    clock.now += 300.0
    await runner.on_incident_event(
        make_event("updated", target_id=other.id, incident_id=other_incident, at=NOW + timedelta(seconds=900)),
        None,
    )
    await drain()

    assert mtr.hosts == [TARGET_HOST, "198.51.100.20"]
    stored = rows(db_path, other_incident)
    assert [row["status"] for row in stored] == ["error", "ok"]


async def test_a_new_incident_on_a_refused_target_is_still_answered(db_path: str) -> None:
    """The refusal cooldown belongs to the incident, not to the target."""
    target_id, first = seed(db_path)
    blocker = quality_db.insert_target(
        db_path,
        name="dns",
        kind="dns",
        protocol=Protocol.ICMP,
        host="198.51.100.20",
        interval_seconds=1.0,
        timeout_ms=1000,
        enabled=True,
    )
    gate = asyncio.Event()
    mtr = FakeMtr(
        {TARGET_HOST: (0, LOSS_FROM_HOP_3, ""), "198.51.100.20": (0, LOSS_FROM_HOP_3, "")},
        gate=gate,
    )
    clock = FakeClock()
    runner = make_runner(db_path, mtr=mtr, clock=clock, max_concurrent=1, min_interval_seconds=300)

    # another target takes the only slot, so the first incident is refused
    await runner.on_incident_event(
        make_event("opened", target_id=blocker.id, incident_id=new_incident(db_path, blocker.id)),
        None,
    )
    await drain()
    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=first), None)
    await drain()
    assert [row["error"] for row in rows(db_path, first)] == ["rate_limited"]

    # the same incident inside the cooldown still adds nothing
    clock.now += 30.0
    await runner.on_incident_event(
        make_event("updated", target_id=target_id, incident_id=first, at=NOW + timedelta(seconds=600)),
        None,
    )
    await drain()
    assert len(rows(db_path, first)) == 1

    # a new incident on the same target inside that cooldown is answered
    second = new_incident(db_path, target_id)
    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=second), None)
    await drain()
    assert [row["error"] for row in rows(db_path, second)] == ["rate_limited"]

    # and it is diagnosed, not merely recorded, once the slot is free again
    gate.set()
    await drain()
    clock.now += 30.0
    third = new_incident(db_path, target_id)
    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=third), None)
    await drain()
    assert TARGET_HOST in mtr.hosts
    assert [row["status"] for row in rows(db_path, third)] == ["ok"]


async def test_close_cancels_a_running_diagnostic(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    gate = asyncio.Event()
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")}, gate=gate)
    runner = make_runner(db_path, mtr=mtr)

    await runner.on_incident_event(make_event("opened", target_id=target_id, incident_id=incident_id), None)
    await drain()
    assert runner.running == 1

    await runner.close()

    assert runner.running == 0
    assert rows(db_path, incident_id) == []
    gate.set()
    await drain()
    assert rows(db_path, incident_id) == []


async def test_the_target_is_looked_up_when_the_event_has_none(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")})
    runner = make_runner(db_path, mtr=mtr)
    target = quality_db.get_target(db_path, target_id)

    await runner.on_incident_event(
        make_event("opened", target_id=target_id, incident_id=incident_id), target
    )
    await drain()

    assert mtr.hosts == [TARGET_HOST]


async def test_an_unusable_target_host_is_refused_instead_of_spawned(db_path: str) -> None:
    """finding I7: a host that is not a host never becomes an mtr argument."""
    target_id, incident_id = seed(db_path)
    quality_db.update_target(db_path, target_id, host="-sS 10.0.0.0/8")
    target = quality_db.get_target(db_path, target_id)
    mtr = FakeMtr({})
    runner = make_runner(db_path, mtr=mtr)

    await runner.on_incident_event(
        make_event("opened", target_id=target_id, incident_id=incident_id), target
    )
    await drain()

    assert mtr.calls == []  # nothing was spawned
    written = rows(db_path, incident_id)
    assert [(row["status"], row["error"]) for row in written] == [("error", "invalid host")]


async def test_an_unusable_gateway_host_does_not_stop_the_target_trace(db_path: str) -> None:
    target_id, incident_id = seed(db_path)
    target = quality_db.get_target(db_path, target_id)
    mtr = FakeMtr({TARGET_HOST: (0, LOSS_FROM_HOP_3, "")})
    runner = make_runner(db_path, mtr=mtr, gateway="--report")

    await runner.on_incident_event(
        make_event("opened", target_id=target_id, incident_id=incident_id), target
    )
    await drain()

    assert mtr.hosts == [TARGET_HOST]
    statuses = [(row["status"], row["error"]) for row in rows(db_path, incident_id)]
    assert ("error", "invalid host") in statuses
    assert any(status == "ok" for status, _error in statuses)
