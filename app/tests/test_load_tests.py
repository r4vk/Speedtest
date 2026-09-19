"""Deterministic tests for the scheduled iperf3 load tests (design spec §10).

No iperf3 is ever executed: the runner is given a fake subprocess runner that
returns the fixture JSON of `tests/fixtures/iperf/`, a fake scheduler that
records the `set_load_test_id` stamps and a virtual `sleep`, so every assertion
below is about what the runner decided, never about timing.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from speedtest_app import quality_db
from speedtest_app.load_tests import LoadTestRunner, LoadTestSettings

FIXTURES = Path(__file__).parent / "fixtures" / "iperf"
_EPOCH = 1_750_000_000.0


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


OK_UPLOAD = (0, fixture("udp_upload_ok.json"), "")
OK_DOWNLOAD = (0, fixture("udp_download_ok.json"), "")


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeScheduler:
    """Records every `set_load_test_id` stamp and keeps the current one."""

    def __init__(self) -> None:
        self.calls: list[int | None] = []
        self.current: int | None = None

    def set_load_test_id(self, load_test_id: int | None) -> None:
        self.calls.append(load_test_id)
        self.current = load_test_id


class FakeIperf:
    """One canned `(rc, stdout, stderr)` per direction, recorded on call."""

    def __init__(
        self,
        upload: tuple[int, str, str] = OK_UPLOAD,
        download: tuple[int, str, str] = OK_DOWNLOAD,
        scheduler: FakeScheduler | None = None,
    ) -> None:
        self.responses = {"upload": upload, "download": download}
        self.scheduler = scheduler
        self.calls: list[tuple[str, list[str], float]] = []
        self.stamps: list[int | None] = []

    async def __call__(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        direction = "download" if "-R" in argv else "upload"
        self.calls.append((direction, list(argv), timeout))
        if self.scheduler is not None:
            self.stamps.append(self.scheduler.current)
        return self.responses[direction]


class FakeTime:
    """A wall clock plus a `sleep` that only moves when the test advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self._seq = 0
        self._sleepers: list[list[Any]] = []

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
        target = self.now + amount
        await self._drain()
        while True:
            due = sorted((e for e in self._sleepers if e[0] <= target), key=lambda e: (e[0], e[1]))
            if not due:
                break
            entry = due[0]
            self._sleepers.remove(entry)
            self.now = max(self.now, entry[0])
            if not entry[2].done():
                entry[2].set_result(None)
            await self._drain()
        self.now = target
        await self._drain()

    @staticmethod
    async def _drain(rounds: int = 60) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0)


def make_runner(
    db_path: str,
    *,
    iperf: FakeIperf | None = None,
    scheduler: FakeScheduler | None = None,
    lock: asyncio.Lock | None = None,
    fake_time: FakeTime | None = None,
    **overrides: Any,
) -> tuple[LoadTestRunner, FakeScheduler, FakeIperf]:
    scheduler = scheduler if scheduler is not None else FakeScheduler()
    iperf = iperf if iperf is not None else FakeIperf(scheduler=scheduler)
    settings = LoadTestSettings(**{"server": "203.0.113.5", **overrides})
    fake_time = fake_time if fake_time is not None else FakeTime()
    runner = LoadTestRunner(
        db_path,
        scheduler=scheduler,
        runtime_lock=lock if lock is not None else asyncio.Lock(),
        settings_getter=lambda: settings,
        subprocess_runner=iperf,
        wall_clock=fake_time.wall,
        sleep=fake_time.sleep,
    )
    return runner, scheduler, iperf


def row(db_path: str, load_test_id: int) -> dict[str, Any]:
    stored = quality_db.get_load_test(db_path, load_test_id)
    assert stored is not None
    return stored


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


class TestLoadTestSettings:
    def test_defaults_when_nothing_is_stored(self) -> None:
        settings = LoadTestSettings.from_settings({})
        assert settings == LoadTestSettings()
        assert settings.enabled is False
        assert settings.interval_seconds == 21600
        assert settings.server == ""
        assert settings.port == 5201
        assert settings.udp_bitrate == "10M"
        assert settings.duration_seconds == 10
        assert settings.datagram_len == 1200
        assert settings.directions == "both"
        assert settings.kind == "iperf_udp"

    def test_stored_values_are_read(self) -> None:
        settings = LoadTestSettings.from_settings(
            {
                "load_test_enabled": "true",
                "load_test_interval_seconds": "3600",
                "load_test_server": " 203.0.113.5 ",
                "load_test_port": "5202",
                "load_test_udp_bitrate": "25M",
                "load_test_duration_seconds": "15",
                "load_test_datagram_len": "1400",
                "load_test_directions": "download",
                "load_test_kind": "iperf_tcp",
            }
        )
        assert settings == LoadTestSettings(
            enabled=True,
            interval_seconds=3600,
            server="203.0.113.5",
            port=5202,
            udp_bitrate="25M",
            duration_seconds=15,
            datagram_len=1400,
            directions="download",
            kind="iperf_tcp",
        )

    def test_invalid_values_fall_back_to_the_defaults(self) -> None:
        settings = LoadTestSettings.from_settings(
            {
                "load_test_port": "99999",
                "load_test_udp_bitrate": "fast",
                "load_test_duration_seconds": "600",
                "load_test_datagram_len": "1",
                "load_test_directions": "sideways",
                "load_test_kind": "ping",
                "load_test_interval_seconds": "0",
            }
        )
        assert settings == LoadTestSettings()


# ---------------------------------------------------------------------------
# run_once: nothing to run
# ---------------------------------------------------------------------------


async def test_unconfigured_server_is_skipped_not_an_outage(db_path: str) -> None:
    runner, scheduler, iperf = make_runner(db_path, server="")

    load_test_id = await runner.run_once()

    stored = row(db_path, load_test_id)
    assert stored["status"] == "skipped"
    assert stored["error"] == "not_configured"
    assert stored["ended_at"] is not None
    assert iperf.calls == []
    assert scheduler.calls == []


async def test_a_running_speed_test_skips_the_load_test(db_path: str) -> None:
    lock = asyncio.Lock()
    await lock.acquire()
    try:
        runner, scheduler, iperf = make_runner(db_path, lock=lock)
        load_test_id = await runner.run_once()
    finally:
        lock.release()

    stored = row(db_path, load_test_id)
    assert stored["status"] == "skipped"
    assert stored["error"] == "speedtest_running"
    assert iperf.calls == []
    assert scheduler.calls == []


# ---------------------------------------------------------------------------
# run_once: a measured run
# ---------------------------------------------------------------------------


async def test_both_directions_are_measured_and_stored(db_path: str) -> None:
    runner, scheduler, iperf = make_runner(db_path)

    load_test_id = await runner.run_once(trigger="manual")

    stored = row(db_path, load_test_id)
    assert stored["status"] == "ok"
    assert stored["error"] is None
    assert stored["kind"] == "iperf_udp"
    assert stored["direction"] == "both"
    assert stored["server"] == "203.0.113.5"
    assert stored["started_at"] is not None and stored["ended_at"] is not None

    params = json.loads(stored["params_json"])
    assert params["trigger"] == "manual"
    assert params["duration_seconds"] == 10

    result = json.loads(stored["result_json"])
    # Loss comes from the receiver's counters, never from `lost_percent`.
    assert result["upload"]["packets"] == 8500
    assert result["upload"]["lost"] == 17
    assert result["upload"]["loss_pct"] == pytest.approx(0.2)
    assert result["upload"]["jitter_ms"] == pytest.approx(0.42)
    assert result["download"]["packets"] == 9000
    assert result["download"]["lost"] == 9
    assert result["download"]["loss_pct"] == pytest.approx(0.1)
    assert len(result["upload"]["intervals"]) == 10
    assert result["upload"]["status"] == "ok"

    raw = json.loads(stored["raw_json"])
    assert json.loads(raw["upload"])["start"]["version"] == "iperf 3.12"
    assert json.loads(raw["download"])["start"]["version"] == "iperf 3.12"

    # upload first, then download; both with the duration + margin timeout
    assert [call[0] for call in iperf.calls] == ["upload", "download"]
    assert {call[2] for call in iperf.calls} == {25.0}
    assert "-R" in iperf.calls[1][1]


async def test_the_run_is_stamped_on_the_probes_and_unstamped_afterwards(db_path: str) -> None:
    runner, scheduler, iperf = make_runner(db_path)

    load_test_id = await runner.run_once()

    assert iperf.stamps == [load_test_id, load_test_id]
    assert scheduler.calls == [load_test_id, None]
    assert scheduler.current is None


async def test_one_direction_only_runs_one_iperf(db_path: str) -> None:
    runner, scheduler, iperf = make_runner(db_path, directions="download")

    stored = row(db_path, await runner.run_once())

    assert stored["status"] == "ok"
    assert stored["direction"] == "download"
    assert [call[0] for call in iperf.calls] == ["download"]
    assert set(json.loads(stored["result_json"])) == {"download"}


# ---------------------------------------------------------------------------
# run_once: failures
# ---------------------------------------------------------------------------


async def test_a_failing_direction_keeps_the_other_direction(db_path: str) -> None:
    iperf = FakeIperf(download=(0, fixture("udp_missing_jitter.json"), ""))
    runner, _scheduler, _iperf = make_runner(db_path, iperf=iperf)

    stored = row(db_path, await runner.run_once())

    assert stored["status"] == "error"
    assert stored["error"] == "missing receiver field jitter_ms"
    result = json.loads(stored["result_json"])
    assert result["download"] == {
        "status": "error",
        "reason": "missing receiver field jitter_ms",
        "direction": "download",
    }
    assert result["upload"]["loss_pct"] == pytest.approx(0.2)
    # the raw output of the failing direction is kept for inspection
    assert "lost_percent" in json.loads(stored["raw_json"])["download"]


async def test_a_non_zero_exit_reports_the_tool_reason(db_path: str) -> None:
    iperf = FakeIperf(
        upload=(1, fixture("error_connect_refused.json"), ""),
        download=(1, fixture("error_connect_refused.json"), ""),
    )
    runner, scheduler, _iperf = make_runner(db_path, iperf=iperf)

    stored = row(db_path, await runner.run_once())

    assert stored["status"] == "error"
    assert "Connection refused" in stored["error"]
    assert json.loads(stored["result_json"])["upload"]["status"] == "error"
    assert scheduler.calls == [stored["id"], None]


async def test_a_timeout_is_an_error_never_a_zero_loss_result(db_path: str) -> None:
    iperf = FakeIperf(upload=(124, "", "timeout"), download=(124, "", "timeout"))
    runner, _scheduler, _iperf = make_runner(db_path, iperf=iperf)

    stored = row(db_path, await runner.run_once())

    assert stored["status"] == "error"
    assert stored["error"] == "timeout"
    result = json.loads(stored["result_json"])
    assert result["upload"]["reason"] == "timeout"
    assert "loss_pct" not in result["upload"]


async def test_a_broken_tool_is_recorded_and_unstamps_the_scheduler(db_path: str) -> None:
    async def explode(argv: list[str], timeout: float) -> tuple[int, str, str]:
        raise FileNotFoundError("iperf3")

    runner, scheduler, _iperf = make_runner(db_path, iperf=explode)  # type: ignore[arg-type]

    stored = row(db_path, await runner.run_once())

    assert stored["status"] == "error"
    assert "iperf3" in stored["error"]
    assert scheduler.calls == [stored["id"], None]


async def test_an_invalid_server_is_an_error_row(db_path: str) -> None:
    runner, _scheduler, iperf = make_runner(db_path, server="not a host!")

    stored = row(db_path, await runner.run_once())

    assert stored["status"] == "error"
    assert stored["error"]
    assert iperf.calls == []


async def test_a_cancelled_run_never_stays_running(db_path: str) -> None:
    gate = asyncio.Event()

    async def blocking(argv: list[str], timeout: float) -> tuple[int, str, str]:
        await gate.wait()
        return OK_UPLOAD

    runner, scheduler, _iperf = make_runner(db_path, iperf=blocking)  # type: ignore[arg-type]
    task = asyncio.create_task(runner.run_once())
    await FakeTime._drain()

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    stored = quality_db.query_load_tests(db_path, "0000", "9999")
    assert [row["status"] for row in stored] == ["error"]
    assert stored[0]["error"] == "cancelled"
    assert stored[0]["ended_at"] is not None
    assert scheduler.calls[-1] is None


# ---------------------------------------------------------------------------
# loop
# ---------------------------------------------------------------------------


async def test_the_loop_runs_nothing_while_the_feature_is_disabled(db_path: str) -> None:
    fake_time = FakeTime()
    runner, _scheduler, iperf = make_runner(
        db_path, fake_time=fake_time, enabled=False, interval_seconds=60
    )
    task = asyncio.create_task(runner.loop())
    try:
        await fake_time.advance(3600.0)
        assert iperf.calls == []
        assert quality_db.query_load_tests(db_path, "0000", "9999") == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_the_loop_runs_on_the_interval_when_enabled(db_path: str) -> None:
    fake_time = FakeTime()
    runner, _scheduler, iperf = make_runner(
        db_path, fake_time=fake_time, enabled=True, interval_seconds=60
    )
    task = asyncio.create_task(runner.loop())
    try:
        await fake_time.advance(60.0)
        assert [call[0] for call in iperf.calls[:2]] == ["upload", "download"]
        rows = quality_db.query_load_tests(db_path, "0000", "9999")
        assert rows and rows[0]["status"] == "ok"
        assert json.loads(rows[0]["params_json"])["trigger"] == "schedule"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
