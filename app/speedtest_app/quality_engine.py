"""Orchestration of the network-quality monitor (design spec §5, §7, §8).

This module owns loops, not knowledge: the probe scheduler runs the targets,
`availability` decides the connection state, `incidents` decides the quality
state, `aggregates` rolls raw rows up — the engine only wakes them at the right
moment, persists what they produced through `quality_db`/`db` and keeps running
when one of them fails. Every loop iteration catches its own exceptions, so a
broken database or a broken probe costs one iteration, never the monitor.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from . import aggregates, dns_probe, https_probe, icmp_probe, quality_db, stats, tcp_probe
from .availability import AvailabilitySettings, AvailabilityTracker, evaluate, quality_state
from .config import AppConfig
from .db import end_blocked_period, get_settings, start_blocked_period
from .diagnostics import SETTINGS_KEYS as DIAGNOSTICS_SETTINGS_KEYS
from .diagnostics import DiagnosticsRunner, DiagnosticsSettings
from .incidents import (
    IncidentEngine,
    IncidentEvent,
    IncidentSettings,
    incident_row_from_state,
)
from .load_tests import SETTINGS_KEYS as LOAD_TEST_SETTINGS_KEYS
from .load_tests import LoadTestRunner, LoadTestSettings
from .probe_scheduler import ProbeFn, ProbeScheduler
from .probe_types import ProbeResult, ProbeTarget, Protocol
from .runtime import get_runtime
from .scheduler import _is_blocked_by_schedule
from .time_utils import to_iso_z, utc_now

log = logging.getLogger(__name__)

Key = tuple[int, str]
Subscriber = Callable[[IncidentEvent, "ProbeTarget | None"], "Awaitable[None] | None"]

#: Settings re-read by the refresh loop (spec §7 table and §8).
SETTINGS_KEYS: list[str] = [
    "incident_window_seconds",
    "incident_min_samples",
    "incident_loss_pct_threshold",
    "incident_outage_loss_pct",
    "incident_rtt_p95_ms_threshold",
    "incident_fail_streak_threshold",
    "incident_open_windows",
    "incident_stabilization_seconds",
    "incident_no_data_close_seconds",
    "availability_eval_seconds",
    "availability_window_seconds",
]

#: How often the aggregation loop runs, and how far back it re-aggregates.
AGGREGATION_INTERVAL_SECONDS = 300.0
AGGREGATION_LOOKBACK_SECONDS = 7200.0
#: How often the `incident_*` / `availability_*` settings are re-read.
SETTINGS_REFRESH_SECONDS = 30.0
#: Floor for an evaluation window, so a broken setting cannot spin the loop.
MIN_WINDOW_SECONDS = 1.0
#: Woken this much after the window boundary, so `floor()` sees the full window.
WINDOW_TICK_LAG_SECONDS = 0.05
#: At most this many missed windows are replayed after a late tick.
MAX_CATCHUP_WINDOWS = 6
#: Columns refreshed on an `updated` event, and additionally on a `closed` one.
INCIDENT_UPDATE_FIELDS = (
    "ended_at",
    "kind",
    "peak_loss_pct",
    "peak_p95_rtt_ms",
    "longest_fail_streak",
    "windows_degraded",
    "summary_json",
)
INCIDENT_CLOSE_FIELDS = INCIDENT_UPDATE_FIELDS + ("closed_at", "close_reason")


def default_probe_registry() -> dict[Protocol, ProbeFn]:
    """The real probes, one per protocol (spec §4)."""
    return {
        Protocol.ICMP: icmp_probe.probe,
        Protocol.TCP: tcp_probe.probe,
        Protocol.DNS: dns_probe.probe,
        Protocol.HTTPS: https_probe.probe,
    }


def _epoch_to_utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


class QualityEngine:
    """Starts, feeds and stops everything the quality monitor is made of."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        app_version: str,
        device_id: str = "nas",
        probe_registry: dict[Protocol, ProbeFn] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.cfg = cfg
        self.app_version = app_version
        self.device_id = device_id
        self.icmp_method = "unknown"

        self._db_path = cfg.db_path
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._subscribers: list[Subscriber] = []
        self._last_results: dict[int, ProbeResult] = {}
        self._last_window_end: dict[Key, float] = {}
        self._open_keys: set[Key] = set()
        self._first_window_epoch = 0.0
        self._last_aggregated_day: Any = None
        self._blocked_reason: str | None = None

        values = self._read_settings()
        self._incident_settings = IncidentSettings.from_settings(values)
        self._availability_settings = AvailabilitySettings.from_settings(values)

        self.incident_engine = IncidentEngine(
            self._incident_settings, probe_interval_seconds=self._probe_interval_for
        )
        self.availability = AvailabilityTracker(self._db_path, cfg)
        self.scheduler = ProbeScheduler(
            self._db_path,
            probe_registry if probe_registry is not None else default_probe_registry(),
            max_concurrency=cfg.probe_max_concurrency,
            flush_seconds=cfg.probe_flush_seconds,
            flush_max=cfg.probe_flush_max,
            buffer_hard_max=cfg.probe_buffer_hard_max,
            device_id=device_id,
            clock=clock,
            wall_clock=wall_clock,
            sleep=sleep,
            target_loader=self._load_targets,
        )
        self.scheduler.on_result(self._remember_result)
        self.load_tests = LoadTestRunner(
            self._db_path,
            scheduler=self.scheduler,
            # The speed test lock, so a load test and a speed test never
            # overlap. It is resolved once, here: `main.lifespan` calls
            # `init_runtime()` before it builds the engine, and a later
            # `init_runtime()` would leave this holding the previous lock.
            runtime_lock=get_runtime().lock,
            settings_getter=self._read_load_test_settings,
            clock=clock,
            wall_clock=wall_clock,
            sleep=sleep,
        )
        self.diagnostics = DiagnosticsRunner(
            self._db_path,
            settings_getter=self._read_diagnostics_settings,
            gateway_host_getter=self.gateway_host,
            clock=clock,
            wall_clock=wall_clock,
        )
        self.subscribe(self.diagnostics.on_incident_event)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Detect ICMP, tidy up after a crash and start every loop."""
        if self._running:
            return
        self._running = True
        self.icmp_method = await asyncio.to_thread(icmp_probe.detect_icmp_method)
        if self.icmp_method == "unavailable":
            # ICMP targets keep running: their `permission`/`exec` rows are the
            # visible proof that the measurement was impossible (spec §4.2).
            log.warning("ICMP is unavailable; ICMP targets will report errors")
        self._close_stale_incidents()
        self._align_windows()
        await self.scheduler.start()
        self._tasks = [
            asyncio.create_task(self._availability_loop(), name="quality-availability"),
            asyncio.create_task(self._incident_loop(), name="quality-incidents"),
            asyncio.create_task(self._aggregation_loop(), name="quality-aggregation"),
            asyncio.create_task(self._settings_loop(), name="quality-settings"),
            asyncio.create_task(self.load_tests.loop(), name="quality-load-tests"),
        ]
        log.info(
            "quality engine started: ICMP method %s, %d target(s)",
            self.icmp_method,
            len(self.scheduler.targets()),
        )

    async def stop(self) -> None:
        """Stop the loops, close the open incidents and flush the scheduler."""
        if not self._running:
            return
        self._running = False
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.diagnostics.close()
        self._close_open_incidents_for_shutdown()
        await self.scheduler.stop()
        await self.availability.drain()
        log.info("quality engine stopped")

    def subscribe(self, cb: Subscriber) -> None:
        """Register a hook called with every incident event and its target."""
        self._subscribers.append(cb)

    # -- targets -----------------------------------------------------------

    def _load_targets(self, db_path: str) -> list[ProbeTarget]:
        """Scheduler target loader: no targets at all while ping is blocked.

        Called by the scheduler's reload loop (every 10 s) in a worker thread,
        so the blocked period is recorded as soon as the setting changes.
        """
        reason = self._block_reason(db_path)
        self._blocked_reason = reason
        now_iso = to_iso_z(self._wall_clock())
        try:
            if reason is not None:
                start_blocked_period(db_path, "ping", reason, now_iso=now_iso)
            else:
                end_blocked_period(db_path, "ping", now_iso=now_iso)
        except Exception:
            log.warning("Could not update the ping blocked period", exc_info=True)
        if reason is not None:
            return []
        return quality_db.list_targets(db_path, False)

    @staticmethod
    def _block_reason(db_path: str) -> str | None:
        values = get_settings(db_path, ["ping_enabled", "ping_schedules"])
        if values.get("ping_enabled", "true").strip().lower() != "true":
            return "disabled"
        if _is_blocked_by_schedule(values.get("ping_schedules", "[]")):
            return "schedule"
        return None

    def gateway_host(self) -> str | None:
        """Host of the enabled `gateway` target, for the diagnostics runner."""
        for target in self.scheduler.targets():
            if target.kind == "gateway" and target.enabled and target.host:
                return target.host
        return None

    def _probe_interval_for(self, target_id: int) -> float:
        for target in self.scheduler.targets():
            if target.id == target_id:
                return float(target.interval_seconds)
        return 1.0

    def _remember_result(self, result: ProbeResult) -> None:
        self._last_results[result.target_id] = result

    # -- load tests --------------------------------------------------------

    async def run_load_test(self, trigger: str = "manual") -> int:
        """Run one load test now and return its `load_tests` row id.

        Never raises for an unconfigured or busy monitor: the row itself says
        `skipped` / `not_configured` / `speedtest_running`.
        """
        return await self.load_tests.run_once(trigger)

    # -- availability ------------------------------------------------------

    async def _availability_loop(self) -> None:
        while self._running:
            await self._sleep(max(float(self._availability_settings.eval_seconds), 0.5))
            if not self._running:
                return
            try:
                self._evaluate_availability()
            except Exception:
                log.exception("Availability evaluation failed")

    def _evaluate_availability(self) -> None:
        settings = self._availability_settings
        now = self._wall_clock()
        state = evaluate(
            self.scheduler.recent_all(settings.window_seconds),
            self.scheduler.targets(),
            now=now,
            settings=settings,
        )
        previous = self.availability.apply(state, now)
        if previous != state:
            log.info("availability %s -> %s", previous or "unknown", state)

    # -- incidents ---------------------------------------------------------

    def _window_seconds(self) -> float:
        return max(float(self._incident_settings.window_seconds), MIN_WINDOW_SECONDS)

    def _align_windows(self) -> None:
        """First evaluated window starts at the first full boundary after now."""
        width = self._window_seconds()
        self._first_window_epoch = math.ceil(self._wall_clock().timestamp() / width) * width

    async def _incident_loop(self) -> None:
        while self._running:
            width = self._window_seconds()
            now_epoch = self._wall_clock().timestamp()
            boundary = math.floor(now_epoch / width) * width + width
            await self._sleep(max(boundary - now_epoch, 0.0) + WINDOW_TICK_LAG_SECONDS)
            if not self._running:
                return
            try:
                await self._evaluate_incidents()
            except Exception:
                log.exception("Incident evaluation failed")

    async def _evaluate_incidents(self) -> None:
        settings = self._incident_settings
        width = self._window_seconds()
        now_epoch = self._wall_clock().timestamp()
        latest_end = math.floor(now_epoch / width) * width

        targets = {
            (target.id, str(target.protocol)): target
            for target in self.scheduler.targets()
            if target.enabled
        }
        # Keys of open incidents whose target vanished (removed, disabled,
        # blocked) are still fed, with empty windows: the engine then closes
        # them with `no_data` instead of leaving a row open forever.
        keys = list(targets) + [key for key in sorted(self._open_keys) if key not in targets]
        live = set(keys)
        self._last_window_end = {
            key: value for key, value in self._last_window_end.items() if key in live
        }

        for key in keys:
            target = targets.get(key)
            for end_epoch in self._pending_windows(key, latest_end, width):
                window_end = _epoch_to_utc(end_epoch)
                window_start = window_end - timedelta(seconds=width)
                if target is None:
                    rows: list[ProbeResult] = []
                else:
                    span = now_epoch - (end_epoch - width) + 1.0
                    rows = self.scheduler.recent(target.id, span)
                window_stats = stats.window(rows, width, window_end)
                events = self.incident_engine.feed(
                    key[0], key[1], window_start, window_end, window_stats
                )
                self._last_window_end[key] = end_epoch
                for event in events:
                    self._persist_event(event, settings)
                    await self._notify(event, target)

    def _pending_windows(self, key: Key, latest_end: float, width: float) -> list[float]:
        """Window ends still to process for ``key``, oldest first."""
        if latest_end - width + 1e-6 < self._first_window_epoch:
            return []
        last = self._last_window_end.get(key)
        if last is None:
            return [latest_end]
        gap = latest_end - last
        if gap <= 1e-6:
            return []
        count = min(max(int(round(gap / width)), 1), MAX_CATCHUP_WINDOWS)
        ends = [latest_end - width * index for index in range(count - 1, -1, -1)]
        return [end for end in ends if end - width + 1e-6 >= self._first_window_epoch]

    def _persist_event(self, event: IncidentEvent, settings: IncidentSettings) -> None:
        """Write the event to `incidents`; a DB failure never stops the loop."""
        key = (event.target_id, event.protocol)
        try:
            if event.type == "opened":
                interval = self.incident_engine.probe_interval_for(*key)
                row = incident_row_from_state(
                    event.state, event.target_id, event.protocol, settings, interval
                )
                incident_id = quality_db.insert_incident(self._db_path, **row)
                self.incident_engine.attach_incident_id(*key, incident_id)
                event.state.incident_id = incident_id
                self._open_keys.add(key)
                log.info(
                    "incident %s opened on target %s (%s), kind=%s",
                    incident_id,
                    event.target_id,
                    event.protocol,
                    event.state.kind,
                )
                return

            incident_id = event.state.incident_id
            if incident_id is None:
                log.warning(
                    "incident event %s for target %s has no row to update",
                    event.type,
                    event.target_id,
                )
                return
            interval = self.incident_engine.probe_interval_for(*key)
            row = incident_row_from_state(
                event.state, event.target_id, event.protocol, settings, interval
            )
            fields = INCIDENT_CLOSE_FIELDS if event.type == "closed" else INCIDENT_UPDATE_FIELDS
            quality_db.update_incident(
                self._db_path, incident_id, **{name: row[name] for name in fields}
            )
            if event.type == "closed":
                self._open_keys.discard(key)
                log.info(
                    "incident %s closed on target %s (%s), reason=%s",
                    incident_id,
                    event.target_id,
                    event.protocol,
                    event.state.close_reason,
                )
        except Exception:
            log.exception("Persisting the %s incident event failed", event.type)

    async def _notify(self, event: IncidentEvent, target: ProbeTarget | None) -> None:
        for callback in list(self._subscribers):
            try:
                result = callback(event, target)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("Incident subscriber failed for %s", event.type)

    def _close_stale_incidents(self) -> None:
        """Close incidents an earlier crash left open (`ended_at` untouched)."""
        try:
            rows = quality_db.list_open_incidents(self._db_path)
            now_iso = to_iso_z(self._wall_clock())
            for row in rows:
                quality_db.update_incident(
                    self._db_path, int(row["id"]), closed_at=now_iso, close_reason="no_data"
                )
            if rows:
                log.warning("closed %d incident(s) left open by an unclean stop", len(rows))
        except Exception:
            log.exception("Closing stale incidents failed")

    def _close_open_incidents_for_shutdown(self) -> None:
        try:
            events = self.incident_engine.close_all("shutdown", at=self._wall_clock())
        except Exception:
            log.exception("Closing open incidents failed")
            return
        for event in events:
            self._persist_event(event, self._incident_settings)

    # -- aggregation -------------------------------------------------------

    async def _aggregation_loop(self) -> None:
        while self._running:
            await self._sleep(AGGREGATION_INTERVAL_SECONDS)
            if not self._running:
                return
            try:
                await asyncio.to_thread(self._aggregate_once)
            except Exception:
                log.exception("Aggregation failed")

    def _aggregate_once(self) -> None:
        """Re-aggregate the last two hours, plus the previous day after midnight.

        Upserts are idempotent, so overlapping runs only refresh a bucket that
        raw rows have since completed.
        """
        now = self._wall_clock()
        now_iso = to_iso_z(now)
        targets = quality_db.list_targets(self._db_path, False)
        if not targets:
            return
        aggregates.aggregate_all(
            self._db_path,
            targets,
            now - timedelta(seconds=AGGREGATION_LOOKBACK_SECONDS),
            now,
            now_iso=now_iso,
        )
        today = now.date()
        if self._last_aggregated_day is not None and today != self._last_aggregated_day:
            day_end = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
            aggregates.aggregate_all(
                self._db_path,
                targets,
                day_end - timedelta(days=1),
                day_end,
                now_iso=now_iso,
            )
            log.info("aggregated the previous UTC day (%s)", day_end.date() - timedelta(days=1))
        self._last_aggregated_day = today

    # -- settings ----------------------------------------------------------

    async def _settings_loop(self) -> None:
        while self._running:
            await self._sleep(SETTINGS_REFRESH_SECONDS)
            if not self._running:
                return
            try:
                self.refresh_settings()
            except Exception:
                log.exception("Refreshing the quality settings failed")

    def refresh_settings(self) -> None:
        """Re-read the thresholds; changed values apply to the next window."""
        values = self._read_settings()
        incident_settings = IncidentSettings.from_settings(values)
        if incident_settings != self._incident_settings:
            log.info("incident settings changed: %s", incident_settings)
            self._incident_settings = incident_settings
            self.incident_engine.settings = incident_settings
        availability_settings = AvailabilitySettings.from_settings(values)
        if availability_settings != self._availability_settings:
            log.info("availability settings changed: %s", availability_settings)
            self._availability_settings = availability_settings

    def _read_settings(self) -> dict[str, str]:
        return self._read_keys(SETTINGS_KEYS)

    def _read_keys(self, keys: list[str]) -> dict[str, str]:
        try:
            return get_settings(self._db_path, keys)
        except Exception:
            log.warning("Could not read the quality settings, keeping defaults", exc_info=True)
            return {}

    def _read_load_test_settings(self) -> LoadTestSettings:
        return LoadTestSettings.from_settings(self._read_keys(LOAD_TEST_SETTINGS_KEYS))

    def _read_diagnostics_settings(self) -> DiagnosticsSettings:
        return DiagnosticsSettings.from_settings(self._read_keys(DIAGNOSTICS_SETTINGS_KEYS))

    @property
    def availability_settings(self) -> AvailabilitySettings:
        return self._availability_settings

    @property
    def incident_settings(self) -> IncidentSettings:
        return self._incident_settings

    @property
    def blocked_reason(self) -> str | None:
        """``disabled`` / ``schedule`` while the probes are held back."""
        return self._blocked_reason

    # -- status ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Status fields of spec §12; never raises, so `/api` stays up."""
        targets = self.scheduler.targets()
        try:
            open_incidents = quality_db.list_open_incidents(self._db_path)
        except Exception:
            log.warning("Could not read the open incidents", exc_info=True)
            open_incidents = []
        availability = self.availability.last_state or "no_data"
        quality, lan_degraded = quality_state(availability, open_incidents, targets)
        scheduler_stats = self.scheduler.stats()
        return {
            "availability": availability,
            "quality": quality,
            "lan_degraded": lan_degraded,
            "icmp_method": self.icmp_method,
            "open_incidents": open_incidents,
            "blocked_reason": self._blocked_reason,
            "load_test_running": self.load_tests.running,
            "diagnostics_running": self.diagnostics.running,
            "scheduler": {
                "buffered_rows": scheduler_stats.buffered_rows,
                "dropped_rows": scheduler_stats.dropped_rows,
                "skipped_ticks": scheduler_stats.skipped_ticks,
                "restarts": scheduler_stats.restarts,
                "flush_errors": scheduler_stats.flush_errors,
                "last_flush_at": scheduler_stats.last_flush_at,
            },
            "targets": [
                {
                    "id": target.id,
                    "name": target.name,
                    "protocol": str(target.protocol),
                    "kind": target.kind,
                    "enabled": target.enabled,
                    "last": (
                        self._last_results[target.id].to_row()
                        if target.id in self._last_results
                        else None
                    ),
                }
                for target in targets
            ],
        }
