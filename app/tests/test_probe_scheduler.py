"""Deterministic tests for the probe scheduler (design spec §5).

Everything is injected: a virtual clock (monotonic + wall), a virtual ``sleep``
that only moves when the test advances time, a fake target loader, a fake writer
and fake probes. No real timers, no real network, no real sleeping.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

from speedtest_app.probe_scheduler import ProbeScheduler
from speedtest_app.probe_types import Outcome, ProbeResult, ProbeTarget, Protocol
from speedtest_app.time_utils import to_iso_z

# --------------------------------------------------------------------------
# virtual time
# --------------------------------------------------------------------------

_EPOCH = 1_750_000_000.0  # arbitrary but fixed wall-clock anchor


async def drain(rounds: int = 60) -> None:
    """Let every task that is ready run until the loop is quiet."""
    for _ in range(rounds):
        await asyncio.sleep(0)


class FakeTime:
    """A monotonic/wall clock pair plus a ``sleep`` driven only by ``advance``."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self._seq = 0
        self._sleepers: list[list[Any]] = []  # [deadline, seq, future]

    def clock(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return datetime.fromtimestamp(_EPOCH + self.now, tz=timezone.utc)

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        self._seq += 1
        entry: list[Any] = [self.now + delay, self._seq, asyncio.get_running_loop().create_future()]
        self._sleepers.append(entry)
        try:
            await entry[2]
        finally:
            if entry in self._sleepers:
                self._sleepers.remove(entry)

    async def advance(self, amount: float) -> None:
        """Move virtual time forward, waking sleepers in deadline order."""
        target = self.now + amount
        await drain()
        while True:
            due = sorted((e for e in self._sleepers if e[0] <= target), key=lambda e: (e[0], e[1]))
            if not due:
                break
            entry = due[0]
            self._sleepers.remove(entry)
            self.now = max(self.now, entry[0])
            if not entry[2].done():
                entry[2].set_result(None)
            await drain()
        self.now = target
        await drain()


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


def make_target(
    target_id: int,
    *,
    protocol: Protocol = Protocol.ICMP,
    interval: float = 1.0,
    timeout_ms: int = 5000,
    enabled: bool = True,
    host: str = "1.1.1.1",
) -> ProbeTarget:
    return ProbeTarget(
        id=target_id,
        name=f"target-{target_id}",
        kind="internet",
        protocol=protocol,
        host=host,
        port=None,
        interval_seconds=interval,
        timeout_ms=timeout_ms,
        enabled=enabled,
        family_pref="auto",
        extra={},
    )


def make_result(
    target: ProbeTarget,
    fake: FakeTime,
    *,
    outcome: Outcome = Outcome.OK,
    rtt_ms: float | None = 12.5,
) -> ProbeResult:
    return ProbeResult(
        target_id=target.id,
        protocol=target.protocol,
        started_at=to_iso_z(fake.wall()),
        duration_ms=1.0,
        outcome=outcome,
        timeout_ms=target.timeout_ms,
        rtt_ms=rtt_ms,
    )


class FakeWriter:
    """Async stand-in for ``quality_db.insert_probe_results``."""

    def __init__(self, fail_times: int = 0) -> None:
        self.batches: list[list[ProbeResult]] = []
        self.calls = 0
        self.fail_times = fail_times

    async def __call__(self, db_path: str, rows: list[ProbeResult]) -> int:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("writer is down")
        self.batches.append(list(rows))
        return len(rows)

    @property
    def written(self) -> list[ProbeResult]:
        return [row for batch in self.batches for row in batch]


def build_scheduler(
    fake: FakeTime,
    targets: list[ProbeTarget],
    registry: dict[Protocol, Callable[[ProbeTarget], Any]],
    writer: FakeWriter | None = None,
    **kwargs: Any,
) -> ProbeScheduler:
    return ProbeScheduler(
        "unused.db",
        registry,
        clock=fake.clock,
        wall_clock=fake.wall,
        sleep=fake.sleep,
        target_loader=lambda _path: list(targets),
        writer=writer or FakeWriter(),
        **kwargs,
    )


@asynccontextmanager
async def running(scheduler: ProbeScheduler) -> AsyncIterator[ProbeScheduler]:
    await scheduler.start()
    try:
        yield scheduler
    finally:
        await scheduler.stop()


# --------------------------------------------------------------------------
# (a) independence of per-target loops
# --------------------------------------------------------------------------


async def test_slow_target_does_not_delay_the_fast_one() -> None:
    fake = FakeTime()
    released = asyncio.Event()
    fast_calls: list[float] = []

    async def slow_probe(target: ProbeTarget) -> ProbeResult:
        await released.wait()
        return make_result(target, fake, outcome=Outcome.TIMEOUT, rtt_ms=None)

    async def fast_probe(target: ProbeTarget) -> ProbeResult:
        fast_calls.append(fake.now)
        return make_result(target, fake)

    targets = [make_target(1), make_target(2, protocol=Protocol.TCP)]
    scheduler = build_scheduler(
        fake, targets, {Protocol.ICMP: slow_probe, Protocol.TCP: fast_probe}
    )

    async with running(scheduler):
        await fake.advance(0)
        assert fast_calls == [1000.0]
        await fake.advance(1.0)
        await fake.advance(1.0)
        assert fast_calls == [1000.0, 1001.0, 1002.0]
        assert scheduler.recent(1, 60.0) == []

        released.set()
        await drain()
        assert len(scheduler.recent(1, 60.0)) == 1

        await fake.advance(1.0)
        assert len(fast_calls) == 4


# --------------------------------------------------------------------------
# (b) overrunning attempts skip ticks instead of building a backlog
# --------------------------------------------------------------------------


async def test_overrun_skips_ticks_without_backlog() -> None:
    fake = FakeTime()
    starts: list[float] = []

    async def slow_probe(target: ProbeTarget) -> ProbeResult:
        starts.append(fake.now)
        await fake.sleep(2.6)
        return make_result(target, fake)

    target = make_target(1, interval=1.0)
    scheduler = build_scheduler(fake, [target], {Protocol.ICMP: slow_probe})

    async with running(scheduler):
        await fake.advance(0)
        assert starts == [1000.0]
        await fake.advance(2.6)  # attempt finishes at 1002.6, ticks 1 and 2 are gone
        assert scheduler.stats().skipped_ticks == {1: 2}
        await fake.advance(0.4)  # next tick is the next future multiple: 1003.0
        assert starts == [1000.0, 1003.0]
        assert scheduler.stats().skipped_ticks == {1: 2}


# --------------------------------------------------------------------------
# (c) flushing by count and by time
# --------------------------------------------------------------------------


async def test_flush_by_count() -> None:
    fake = FakeTime()
    writer = FakeWriter()

    async def probe(target: ProbeTarget) -> ProbeResult:
        return make_result(target, fake)

    scheduler = build_scheduler(
        fake,
        [make_target(1, interval=1.0)],
        {Protocol.ICMP: probe},
        writer=writer,
        flush_seconds=1000.0,
        flush_max=3,
    )

    async with running(scheduler):
        await fake.advance(0)
        await fake.advance(1.0)
        assert writer.calls == 0
        assert scheduler.stats().buffered_rows == 2

        await fake.advance(1.0)
        assert writer.calls == 1
        assert len(writer.batches[0]) == 3
        assert scheduler.stats().buffered_rows == 0
        assert scheduler.stats().last_flush_at is not None


async def test_flush_by_time() -> None:
    fake = FakeTime()
    writer = FakeWriter()

    async def probe(target: ProbeTarget) -> ProbeResult:
        return make_result(target, fake)

    scheduler = build_scheduler(
        fake,
        [make_target(1, interval=1000.0)],  # exactly one attempt during the test
        {Protocol.ICMP: probe},
        writer=writer,
        flush_seconds=5.0,
        flush_max=500,
    )

    async with running(scheduler):
        await fake.advance(4.0)
        assert writer.calls == 0
        await fake.advance(1.0)
        assert writer.calls == 1
        assert len(writer.batches[0]) == 1


# --------------------------------------------------------------------------
# (d) a failing writer keeps the rows and backs off
# --------------------------------------------------------------------------


async def test_failed_flush_retains_rows_and_backs_off() -> None:
    fake = FakeTime()
    writer = FakeWriter(fail_times=2)

    async def probe(target: ProbeTarget) -> ProbeResult:
        return make_result(target, fake)

    scheduler = build_scheduler(
        fake,
        [make_target(1, interval=1000.0)],
        {Protocol.ICMP: probe},
        writer=writer,
        flush_seconds=100.0,
        flush_max=1,
    )

    async with running(scheduler):
        await fake.advance(0)
        assert writer.calls == 1
        assert scheduler.stats().flush_errors == 1
        assert scheduler.stats().buffered_rows == 1
        assert writer.batches == []

        await fake.advance(1.0)  # first backoff step
        assert writer.calls == 2
        assert scheduler.stats().flush_errors == 2
        assert scheduler.stats().buffered_rows == 1

        await fake.advance(1.0)  # second backoff step is 2 s, nothing yet
        assert writer.calls == 2

        await fake.advance(1.0)
        assert writer.calls == 3
        assert scheduler.stats().flush_errors == 2
        assert scheduler.stats().buffered_rows == 0
        assert len(writer.written) == 1


# --------------------------------------------------------------------------
# (e) the hard buffer cap drops the oldest rows
# --------------------------------------------------------------------------


async def test_buffer_hard_max_drops_oldest_rows() -> None:
    fake = FakeTime()
    writer = FakeWriter()
    counter = 0

    async def probe(target: ProbeTarget) -> ProbeResult:
        nonlocal counter
        counter += 1
        return make_result(target, fake, rtt_ms=float(counter))

    scheduler = build_scheduler(
        fake,
        [make_target(1, interval=1.0)],
        {Protocol.ICMP: probe},
        writer=writer,
        flush_seconds=1000.0,
        flush_max=1000,
        buffer_hard_max=5,
    )

    async with running(scheduler):
        await fake.advance(0)
        for _ in range(7):
            await fake.advance(1.0)
        stats = scheduler.stats()
        assert stats.buffered_rows == 5
        assert stats.dropped_rows == 3

    assert [row.rtt_ms for row in writer.written] == [4.0, 5.0, 6.0, 7.0, 8.0]


# --------------------------------------------------------------------------
# (f) reload restarts only the changed loop
# --------------------------------------------------------------------------


async def test_reload_restarts_only_the_changed_target() -> None:
    fake = FakeTime()

    async def probe(target: ProbeTarget) -> ProbeResult:
        return make_result(target, fake)

    current = [make_target(1, interval=1.0), make_target(2, protocol=Protocol.TCP, interval=1.0)]
    scheduler = ProbeScheduler(
        "unused.db",
        {Protocol.ICMP: probe, Protocol.TCP: probe},
        clock=fake.clock,
        wall_clock=fake.wall,
        sleep=fake.sleep,
        target_loader=lambda _path: list(current),
        writer=FakeWriter(),
    )

    async with running(scheduler):
        await fake.advance(0)
        task_one = scheduler._tasks[1]
        task_two = scheduler._tasks[2]

        current[0] = make_target(1, interval=5.0)
        await scheduler.reload_targets()
        await drain()

        assert scheduler._tasks[2] is task_two
        assert scheduler._tasks[1] is not task_one
        assert task_one.done()
        assert {t.id: t.interval_seconds for t in scheduler.targets()} == {1: 5.0, 2: 1.0}


async def test_reload_stops_disabled_target_but_keeps_recent() -> None:
    fake = FakeTime()

    async def probe(target: ProbeTarget) -> ProbeResult:
        return make_result(target, fake)

    current = [make_target(1, interval=1.0)]
    scheduler = ProbeScheduler(
        "unused.db",
        {Protocol.ICMP: probe},
        clock=fake.clock,
        wall_clock=fake.wall,
        sleep=fake.sleep,
        target_loader=lambda _path: list(current),
        writer=FakeWriter(),
    )

    async with running(scheduler):
        await fake.advance(0)
        assert len(scheduler.recent(1, 60.0)) == 1

        current[0] = make_target(1, interval=1.0, enabled=False)
        await scheduler.reload_targets()
        await drain()
        assert 1 not in scheduler._tasks

        await fake.advance(5.0)
        assert len(scheduler.recent(1, 60.0)) == 1  # loop stopped, history kept


# --------------------------------------------------------------------------
# (g) a protocol without a probe is visible, never silent
# --------------------------------------------------------------------------


async def test_unknown_protocol_produces_error_exec_rows() -> None:
    fake = FakeTime()
    scheduler = build_scheduler(fake, [make_target(1, protocol=Protocol.DNS, interval=1.0)], {})

    async with running(scheduler):
        await fake.advance(0)
        await fake.advance(1.0)
        results = scheduler.recent(1, 60.0)
        assert len(results) == 2
        assert all(r.outcome is Outcome.ERROR for r in results)
        assert all(r.error_kind == "exec" for r in results)
        assert all(r.error_detail == "no probe for protocol" for r in results)


# --------------------------------------------------------------------------
# (h) recent() window
# --------------------------------------------------------------------------


async def test_recent_filters_by_window_oldest_first() -> None:
    fake = FakeTime()

    async def probe(target: ProbeTarget) -> ProbeResult:
        return make_result(target, fake)

    scheduler = build_scheduler(fake, [make_target(1, interval=1.0)], {Protocol.ICMP: probe})

    async with running(scheduler):
        await fake.advance(0)
        for _ in range(5):
            await fake.advance(1.0)

        assert len(scheduler.recent(1, 60.0)) == 6
        window = scheduler.recent(1, 2.5)
        assert len(window) == 3
        assert [r.started_at for r in window] == sorted(r.started_at for r in window)
        assert scheduler.recent(99, 60.0) == []
        assert scheduler.recent_all(2.5)[1] == window


# --------------------------------------------------------------------------
# (i) stop() flushes what is left
# --------------------------------------------------------------------------


async def test_stop_performs_a_final_flush() -> None:
    fake = FakeTime()
    writer = FakeWriter()

    async def probe(target: ProbeTarget) -> ProbeResult:
        return make_result(target, fake)

    scheduler = build_scheduler(
        fake,
        [make_target(1, interval=1.0)],
        {Protocol.ICMP: probe},
        writer=writer,
        flush_seconds=1000.0,
        flush_max=1000,
    )

    await scheduler.start()
    await fake.advance(0)
    await fake.advance(1.0)
    assert writer.calls == 0

    await scheduler.stop()
    assert writer.calls == 1
    assert len(writer.batches[0]) == 2
    assert scheduler.stats().buffered_rows == 0


# --------------------------------------------------------------------------
# (j) a raising probe becomes error/exec and the loop survives
# --------------------------------------------------------------------------


async def test_probe_exception_becomes_error_exec_and_loop_continues() -> None:
    fake = FakeTime()
    calls = 0

    async def boom(target: ProbeTarget) -> ProbeResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("probe exploded")

    target = make_target(1, interval=1.0, timeout_ms=1234)
    scheduler = build_scheduler(fake, [target], {Protocol.ICMP: boom})

    async with running(scheduler):
        await fake.advance(0)
        await fake.advance(1.0)
        assert calls == 2
        results = scheduler.recent(1, 60.0)
        assert len(results) == 2
        first = results[0]
        assert first.outcome is Outcome.ERROR
        assert first.error_kind == "exec"
        assert first.error_detail is not None
        assert "RuntimeError" in first.error_detail
        assert "probe exploded" in first.error_detail
        assert first.timeout_ms == 1234
        assert first.protocol is Protocol.ICMP


# --------------------------------------------------------------------------
# stamping and the result hook
# --------------------------------------------------------------------------


async def test_results_are_stamped_and_callbacks_are_isolated() -> None:
    fake = FakeTime()
    seen: list[ProbeResult] = []

    async def probe(target: ProbeTarget) -> ProbeResult:
        return make_result(target, fake)

    def good_callback(result: ProbeResult) -> None:
        seen.append(result)

    def bad_callback(result: ProbeResult) -> None:
        raise ValueError("callback exploded")

    scheduler = build_scheduler(
        fake, [make_target(1, interval=1.0)], {Protocol.ICMP: probe}, device_id="nas-2"
    )
    scheduler.on_result(bad_callback)
    scheduler.on_result(good_callback)

    async with running(scheduler):
        await fake.advance(0)
        scheduler.set_load_test_id(7)
        await fake.advance(1.0)
        scheduler.set_load_test_id(None)
        await fake.advance(1.0)

    assert [r.load_test_id for r in seen] == [None, 7, None]
    assert {r.device_id for r in seen} == {"nas-2"}


async def test_stats_defaults_and_targets_view() -> None:
    fake = FakeTime()

    async def probe(target: ProbeTarget) -> ProbeResult:
        return make_result(target, fake)

    scheduler = build_scheduler(fake, [make_target(1, interval=1.0)], {Protocol.ICMP: probe})
    stats = scheduler.stats()
    assert (stats.buffered_rows, stats.dropped_rows, stats.flush_errors) == (0, 0, 0)
    assert stats.skipped_ticks == {}
    assert stats.last_flush_at is None
    assert scheduler.targets() == []

    async with running(scheduler):
        await fake.advance(0)
        assert [t.id for t in scheduler.targets()] == [1]


async def test_reload_supervises_a_stopped_loop() -> None:
    fake = FakeTime()

    async def probe(target: ProbeTarget) -> ProbeResult:
        return make_result(target, fake)

    scheduler = build_scheduler(fake, [make_target(1, interval=1.0)], {Protocol.ICMP: probe})

    async with running(scheduler):
        await fake.advance(0)
        dead = scheduler._tasks[1]
        dead.cancel()
        await drain()
        assert dead.done()

        await scheduler.reload_targets()
        await drain()
        assert scheduler._tasks[1] is not dead

        await fake.advance(1.0)
        assert len(scheduler.recent(1, 60.0)) >= 2
