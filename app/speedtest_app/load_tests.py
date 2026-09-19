"""Scheduled iperf3 load tests (design spec §10, plan stage 5).

This module owns *when* a load test runs and *what is written about it*; the
argv and the parsing belong to `iperf_udp.py`, which never touches a process.
Three rules shape everything below:

- A load test never overlaps a speed test: both take the runtime lock, and a
  load test that finds it taken records `status='skipped'` instead of queueing.
- An unconfigured server is not an outage: it is a `skipped` /
  `not_configured` row, visible but harmless.
- A direction that could not be measured is an `error` with its reason. A
  missing measurement is never written as 0 % loss.

While a run is in flight the probes keep running and their rows are stamped
with the load test id (`scheduler.set_load_test_id`), so latency under load can
later be told apart from latency at rest.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Protocol

from . import quality_db
from .iperf_udp import (
    LoadTestParams,
    LoadTestResult,
    ResultValidationError,
    build_command,
    parse_result,
    summarize_for_report,
)
from .scheduler import _seconds_until_next_aligned
from .time_utils import to_iso_z, utc_now

log = logging.getLogger(__name__)

#: Settings keys read for a load test run (spec §10).
SETTINGS_KEYS: list[str] = [
    "load_test_enabled",
    "load_test_interval_seconds",
    "load_test_server",
    "load_test_port",
    "load_test_udp_bitrate",
    "load_test_duration_seconds",
    "load_test_datagram_len",
    "load_test_directions",
    "load_test_kind",
]

#: Added to the test duration to get the subprocess timeout.
SUBPROCESS_MARGIN_SECONDS = 15.0
#: Exit code used for "the tool did not finish in time" (same as `timeout(1)`).
TIMEOUT_EXIT_CODE = 124
#: How often a disabled loop re-reads the settings.
DISABLED_POLL_SECONDS = 30.0

DIRECTIONS: frozenset[str] = frozenset({"upload", "download", "both"})
KINDS: frozenset[str] = frozenset({"iperf_udp", "iperf_tcp"})

SubprocessRunner = Callable[..., Awaitable[tuple[int, str, str]]]


class _Stampable(Protocol):
    """The one thing a load test needs from the probe scheduler."""

    def set_load_test_id(self, load_test_id: int | None) -> None: ...


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def _setting(values: Mapping[str, str], key: str, default: Any, cast: Callable[[str], Any]) -> Any:
    raw = values.get(key)
    if raw is None:
        return default
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("Setting %s has an unusable value %r, keeping %r", key, raw, default)
        return default


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"true", "1", "yes", "on"}


def _validated(field: str, value: Any, default: Any) -> Any:
    """Keep ``value`` only if `iperf_udp` accepts it, otherwise the default.

    Validation is delegated to `build_command`, so a setting can never describe
    a command that `iperf_udp` would refuse to build later; the placeholder
    server keeps the check about the one field being validated.
    """
    try:
        build_command(LoadTestParams(server="127.0.0.1", **{field: value}))
    except ValueError as exc:
        log.warning("Setting load_test_%s rejected (%s), keeping %r", field, exc, default)
        return default
    return value


@dataclass(frozen=True)
class LoadTestSettings:
    """The `load_test_*` settings of spec §10, with the spec's defaults."""

    enabled: bool = False
    interval_seconds: int = 21600
    server: str = ""
    port: int = 5201
    udp_bitrate: str = "10M"
    duration_seconds: int = 10
    datagram_len: int = 1200
    directions: str = "both"
    kind: str = "iperf_udp"

    @staticmethod
    def from_settings(values: Mapping[str, str]) -> "LoadTestSettings":
        """Parse the `load_test_*` keys; anything unusable keeps the default.

        The server itself is *not* validated here: a typo must not look like an
        unconfigured feature, so an unusable host reaches `run_once` and is
        recorded as an `error` row with its reason.
        """
        d = LoadTestSettings()
        interval = _setting(values, "load_test_interval_seconds", d.interval_seconds, int)
        if not isinstance(interval, int) or interval < 1:
            log.warning("Setting load_test_interval_seconds must be >= 1, keeping %r", d.interval_seconds)
            interval = d.interval_seconds
        directions = _setting(values, "load_test_directions", d.directions, str)
        if directions not in DIRECTIONS:
            log.warning("Setting load_test_directions has an unknown value %r", directions)
            directions = d.directions
        kind = _setting(values, "load_test_kind", d.kind, str)
        if kind not in KINDS:
            log.warning("Setting load_test_kind has an unknown value %r", kind)
            kind = d.kind
        return LoadTestSettings(
            enabled=_setting(values, "load_test_enabled", d.enabled, _as_bool),
            interval_seconds=interval,
            server=_setting(values, "load_test_server", d.server, str),
            port=_validated("port", _setting(values, "load_test_port", d.port, int), d.port),
            udp_bitrate=_validated(
                "udp_bitrate",
                _setting(values, "load_test_udp_bitrate", d.udp_bitrate, str),
                d.udp_bitrate,
            ),
            duration_seconds=_validated(
                "duration_seconds",
                _setting(values, "load_test_duration_seconds", d.duration_seconds, int),
                d.duration_seconds,
            ),
            datagram_len=_validated(
                "datagram_len",
                _setting(values, "load_test_datagram_len", d.datagram_len, int),
                d.datagram_len,
            ),
            directions=directions,
            kind=kind,
        )


# ---------------------------------------------------------------------------
# subprocess
# ---------------------------------------------------------------------------


async def _run_iperf(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Run one iperf3 invocation; a timeout is reported, never raised."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        await proc.wait()
        return (TIMEOUT_EXIT_CODE, "", "timeout")
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _failure_reason(returncode: int, stdout: str, stderr: str) -> str:
    """The most specific reason a failed iperf3 invocation gave us."""
    if returncode == TIMEOUT_EXIT_CODE:
        return "timeout"
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return data["error"]
    detail = next((line.strip() for line in stderr.splitlines() if line.strip()), "")
    return f"iperf3 exited with {returncode}: {detail}" if detail else f"iperf3 exited with {returncode}"


def _direction_result(result: LoadTestResult) -> dict[str, Any]:
    """The per-direction block of `result_json` (summary + per-second detail)."""
    return {
        **summarize_for_report(result),
        "status": "ok",
        "duration_seconds": result.duration_seconds,
        "receiver": result.receiver,
        "sender": result.sender,
        "intervals": [asdict(interval) for interval in result.intervals],
    }


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


class LoadTestRunner:
    """Runs load tests on a schedule and on demand, one at a time."""

    def __init__(
        self,
        db_path: str,
        *,
        scheduler: _Stampable,
        runtime_lock: asyncio.Lock,
        settings_getter: Callable[[], LoadTestSettings],
        subprocess_runner: SubprocessRunner = _run_iperf,
        wall_clock: Callable[[], datetime] = utc_now,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._db_path = db_path
        self._scheduler = scheduler
        self._runtime_lock = runtime_lock
        self._settings_getter = settings_getter
        self._subprocess_runner = subprocess_runner
        self._wall_clock = wall_clock
        self._clock = clock
        self._sleep = sleep
        self._last_settings = LoadTestSettings()
        self._running = False

    @property
    def running(self) -> bool:
        """True while an iperf3 run of this runner is in flight."""
        return self._running

    def settings(self) -> LoadTestSettings:
        """The current settings; a failing getter keeps the last known ones."""
        try:
            self._last_settings = self._settings_getter()
        except Exception:
            log.warning("Could not read the load test settings, keeping the last known ones", exc_info=True)
        return self._last_settings

    # -- one run -----------------------------------------------------------

    async def run_once(self, trigger: str = "schedule") -> int:
        """Run (or visibly skip) one load test and return its `load_tests` id."""
        settings = self.settings()
        directions = (
            ["upload", "download"] if settings.directions == "both" else [settings.directions]
        )
        load_test_id = quality_db.insert_load_test(
            self._db_path,
            started_at=to_iso_z(self._wall_clock()),
            kind=settings.kind,
            direction=settings.directions,
            server=settings.server or None,
            params_json=json.dumps(
                {
                    "trigger": trigger,
                    "server": settings.server,
                    "port": settings.port,
                    "kind": settings.kind,
                    "directions": settings.directions,
                    "duration_seconds": settings.duration_seconds,
                    "udp_bitrate": settings.udp_bitrate,
                    "datagram_len": settings.datagram_len,
                },
                ensure_ascii=False,
            ),
            status="running",
        )

        if not settings.server:
            self._finish(load_test_id, status="skipped", error="not_configured")
            log.info("load test %s skipped: no iperf3 server configured", load_test_id)
            return load_test_id
        if self._runtime_lock.locked():
            self._finish(load_test_id, status="skipped", error="speedtest_running")
            log.info("load test %s skipped: a speed test is running", load_test_id)
            return load_test_id

        began = self._clock()
        async with self._runtime_lock:
            self._running = True
            self._scheduler.set_load_test_id(load_test_id)
            try:
                results, raws = await self._run_directions(settings, directions)
            except asyncio.CancelledError:
                # A stopped monitor must not leave a row `running` for ever.
                self._finish(load_test_id, status="error", error="cancelled")
                raise
            except Exception as exc:
                log.exception("Load test %s failed", load_test_id)
                self._finish(load_test_id, status="error", error=f"load test failed: {exc}")
                return load_test_id
            finally:
                self._scheduler.set_load_test_id(None)
                self._running = False

        reasons = [
            results[direction]["reason"]
            for direction in directions
            if results.get(direction, {}).get("status") != "ok"
        ]
        self._finish(
            load_test_id,
            status="ok" if not reasons else "error",
            error=reasons[0] if reasons else None,
            result_json=json.dumps(results, ensure_ascii=False),
            raw_json=json.dumps(raws, ensure_ascii=False),
        )
        log.info(
            "load test %s finished in %.1f s: %s (%s)",
            load_test_id,
            self._clock() - began,
            "ok" if not reasons else "error",
            settings.directions,
        )
        return load_test_id

    async def _run_directions(
        self, settings: LoadTestSettings, directions: list[str]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """Run the requested directions sequentially; one failure is not fatal."""
        results: dict[str, dict[str, Any]] = {}
        raws: dict[str, str] = {}
        for direction in directions:
            result, raw = await self._run_direction(settings, direction)
            results[direction] = result
            if raw:
                raws[direction] = raw
        return results, raws

    async def _run_direction(
        self, settings: LoadTestSettings, direction: str
    ) -> tuple[dict[str, Any], str]:
        params = LoadTestParams(
            server=settings.server,
            port=settings.port,
            kind=settings.kind,  # type: ignore[arg-type]
            direction=direction,  # type: ignore[arg-type]
            duration_seconds=settings.duration_seconds,
            udp_bitrate=settings.udp_bitrate,
            datagram_len=settings.datagram_len,
        )
        try:
            argv = build_command(params)
        except ValueError as exc:
            return _failed(direction, str(exc)), ""

        timeout = float(settings.duration_seconds) + SUBPROCESS_MARGIN_SECONDS
        try:
            returncode, stdout, stderr = await self._subprocess_runner(argv, timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("iperf3 could not be run (%s)", direction, exc_info=True)
            return _failed(direction, f"iperf3 could not be run: {exc}"), ""

        if returncode != 0:
            return _failed(direction, _failure_reason(returncode, stdout, stderr)), stdout
        try:
            result = parse_result(stdout, kind=settings.kind, direction=direction)
        except ResultValidationError as exc:
            return _failed(direction, exc.reason), stdout
        return _direction_result(result), stdout

    def _finish(self, load_test_id: int, **fields: Any) -> None:
        try:
            quality_db.update_load_test(
                self._db_path,
                load_test_id,
                ended_at=to_iso_z(self._wall_clock()),
                **fields,
            )
        except Exception:
            log.exception("Could not close the load test row %s", load_test_id)

    # -- schedule ----------------------------------------------------------

    async def loop(self) -> None:
        """Run a load test on every aligned interval while the feature is on."""
        while True:
            try:
                settings = self.settings()
                if not settings.enabled:
                    await self._sleep(DISABLED_POLL_SECONDS)
                    continue
                await self._sleep(_seconds_until_next_aligned(settings.interval_seconds))
                if not self.settings().enabled:
                    continue
                await self.run_once("schedule")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Load test iteration failed")
                await self._sleep(DISABLED_POLL_SECONDS)


def _failed(direction: str, reason: str) -> dict[str, Any]:
    """A direction nobody could measure — a reason, never a 0 % loss result."""
    return {"status": "error", "reason": reason, "direction": direction}
