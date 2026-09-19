"""Per-target probe loops, bounded concurrency and buffered batch writes (spec §5).

The scheduler owns one asyncio task per enabled target. Each loop keeps its own
tick alignment (``start + n * interval``) so a slow target can never delay a fast
one and an overrunning attempt skips ticks instead of building a backlog. Results
are appended to an in-memory buffer flushed in batches, and to a small per-target
ring used by the availability/incident engines through :meth:`ProbeScheduler.recent`
so they never wait for the database.

Nothing in here touches the network: probes come from the injected registry.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable

from .probe_types import Outcome, ProbeResult, ProbeTarget, Protocol
from .quality_db import insert_probe_results, list_targets
from .time_utils import to_iso_z, utc_now

logger = logging.getLogger(__name__)

#: A probe is bound to its target only; timeouts come from ``target.timeout_ms``.
ProbeFn = Callable[[ProbeTarget], Awaitable[ProbeResult]]
#: Sync (run in a worker thread) or async loader of the target list.
TargetLoaderFn = Callable[[str], "list[ProbeTarget] | Awaitable[list[ProbeTarget]]"]
#: Sync (run in a worker thread) or async batch writer; returns the rows written.
WriterFn = Callable[[str, "list[ProbeResult]"], "int | Awaitable[int]"]

#: Extra wall time granted on top of ``timeout_ms`` before an attempt is killed.
GUARD_EXTRA_SECONDS = 0.25
#: Lower bound for a target interval, purely to keep the loop arithmetic sane.
MIN_INTERVAL_SECONDS = 0.05
#: Flush backoff after a failed write: 1, 2, 4, 8, 16, 30, 30, ...
MAX_FLUSH_BACKOFF_SECONDS = 30.0
#: Extra slots kept in a per-target ring on top of ``recent_seconds / interval``.
RECENT_SLACK = 10
#: At most one "dropping rows" warning per this many seconds.
DROP_LOG_SECONDS = 60.0
_MAX_ERROR_DETAIL = 200

#: Fields whose change forces a restart of the target's loop.
_LOOP_FIELDS = (
    "protocol",
    "host",
    "port",
    "interval_seconds",
    "timeout_ms",
    "family_pref",
    "extra",
)


@dataclass
class SchedulerStats:
    """Snapshot of the scheduler's health, surfaced by `/api/quality/status`."""

    buffered_rows: int
    dropped_rows: int
    skipped_ticks: dict[int, int]
    flush_errors: int
    last_flush_at: str | None


def _is_async_callable(fn: Any) -> bool:
    if inspect.iscoroutinefunction(fn):
        return True
    call = getattr(fn, "__call__", None)  # noqa: B004 - callable instances count too
    return call is not None and inspect.iscoroutinefunction(call)


async def _call(fn: Any, *args: Any) -> Any:
    """Await an async callable, or run a blocking one in a worker thread."""
    if _is_async_callable(fn):
        return await fn(*args)
    return await asyncio.to_thread(fn, *args)


def _short_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_DETAIL]


class ProbeScheduler:
    """Runs every enabled target on its own cadence and batches the results."""

    def __init__(
        self,
        db_path: str,
        probe_registry: dict[Protocol, ProbeFn],
        *,
        max_concurrency: int = 16,
        flush_seconds: float = 5.0,
        flush_max: int = 500,
        buffer_hard_max: int = 20_000,
        reload_seconds: float = 10.0,
        recent_seconds: float = 600.0,
        device_id: str = "nas",
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        target_loader: TargetLoaderFn | None = None,
        writer: WriterFn | None = None,
    ) -> None:
        self._db_path = db_path
        self._registry = dict(probe_registry)
        self._max_concurrency = max(1, int(max_concurrency))
        self._flush_seconds = float(flush_seconds)
        self._flush_max = max(1, int(flush_max))
        self._buffer_hard_max = max(1, int(buffer_hard_max))
        self._reload_seconds = float(reload_seconds)
        self._recent_seconds = float(recent_seconds)
        self._device_id = device_id
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._target_loader: TargetLoaderFn = target_loader or _default_target_loader
        self._writer: WriterFn = writer or insert_probe_results

        self._targets: dict[int, ProbeTarget] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._buffer: deque[ProbeResult] = deque()
        self._inflight = 0
        self._recent: dict[int, deque[tuple[float, ProbeResult]]] = {}
        self._skipped_ticks: dict[int, int] = {}
        self._dropped_rows = 0
        self._flush_errors = 0
        self._last_flush_at: str | None = None
        self._last_drop_log: float | None = None
        self._load_test_id: int | None = None
        self._callbacks: list[Callable[[ProbeResult], None]] = []

        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._flush_signal = asyncio.Event()
        self._flush_task: asyncio.Task[None] | None = None
        self._reload_task: asyncio.Task[None] | None = None
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Load the targets and start the per-target, flush and reload loops."""
        if self._running:
            return
        self._running = True
        await self.reload_targets()
        self._flush_task = asyncio.create_task(self._flush_loop(), name="probe-flush")
        self._reload_task = asyncio.create_task(self._reload_loop(), name="probe-reload")
        logger.info(
            "probe scheduler started: %d target(s), flush every %.1fs",
            len(self._tasks),
            self._flush_seconds,
        )

    async def stop(self) -> None:
        """Cancel every loop and write whatever is still buffered."""
        if not self._running:
            return
        self._running = False
        tasks = [t for t in (self._flush_task, self._reload_task) if t is not None]
        tasks.extend(self._tasks.values())
        self._tasks.clear()
        self._flush_task = None
        self._reload_task = None
        await _cancel_all(tasks)
        await self._flush_once(final=True)
        logger.info(
            "probe scheduler stopped: %d row(s) still buffered", len(self._buffer) + self._inflight
        )

    # -- targets -----------------------------------------------------------

    async def reload_targets(self) -> None:
        """Diff the DB target list against the running loops and apply it."""
        try:
            loaded = await _call(self._target_loader, self._db_path)
        except Exception as exc:  # pragma: no cover - defensive, loader is DB-bound
            logger.warning("target reload failed, keeping current targets: %s", _short_detail(exc))
            return

        incoming = {t.id: t for t in loaded}
        stale: list[asyncio.Task[None]] = []

        for target_id in list(self._targets):
            if target_id not in incoming:
                task = self._tasks.pop(target_id, None)
                if task is not None:
                    stale.append(task)
                self._targets.pop(target_id, None)
                self._recent.pop(target_id, None)
                logger.info("target %s removed", target_id)

        for target_id, target in incoming.items():
            previous = self._targets.get(target_id)
            self._targets[target_id] = target
            running = self._tasks.get(target_id)
            if not target.enabled:
                if running is not None:
                    stale.append(self._tasks.pop(target_id))
                    logger.info("target %s disabled, loop stopped", target_id)
                continue
            if running is None or running.done():
                # A loop that stopped on its own (a crash, a cancellation) is
                # supervised here: the next reload brings it back.
                self._start_target(target)
                continue
            if previous is not None and _loop_relevant_change(previous, target):
                stale.append(self._tasks.pop(target_id))
                self._start_target(target)
                logger.info("target %s changed, loop restarted", target_id)

        await _cancel_all(stale)

    def targets(self) -> list[ProbeTarget]:
        """The targets currently loaded, enabled or not, ordered by id."""
        return [self._targets[tid] for tid in sorted(self._targets)]

    def _start_target(self, target: ProbeTarget) -> None:
        self._ensure_ring(target)
        self._tasks[target.id] = asyncio.create_task(
            self._target_loop(target), name=f"probe-target-{target.id}"
        )

    def _ensure_ring(self, target: ProbeTarget) -> None:
        interval = _interval_of(target)
        maxlen = int(math.ceil(self._recent_seconds / interval)) + RECENT_SLACK
        ring = self._recent.get(target.id)
        if ring is None:
            self._recent[target.id] = deque(maxlen=maxlen)
        elif ring.maxlen != maxlen:
            self._recent[target.id] = deque(ring, maxlen=maxlen)

    # -- per-target loop ---------------------------------------------------

    async def _target_loop(self, target: ProbeTarget) -> None:
        interval = _interval_of(target)
        start = self._clock()
        tick = 0
        try:
            while self._running:
                due = start + tick * interval
                delay = due - self._clock()
                if delay > 0:
                    await self._sleep(delay)
                    if not self._running:
                        return
                await self._run_attempt(target)
                now = self._clock()
                next_tick = tick + 1
                if start + next_tick * interval <= now:
                    # The tick we should run next is already in the past: jump to
                    # the next future multiple and record what we skipped (§5).
                    jumped = int(math.floor((now - start) / interval)) + 1
                    skipped = jumped - next_tick
                    if skipped > 0:
                        self._skipped_ticks[target.id] = (
                            self._skipped_ticks.get(target.id, 0) + skipped
                        )
                        logger.debug(
                            "target %s skipped %d tick(s): attempt overran the interval",
                            target.id,
                            skipped,
                        )
                    next_tick = jumped
                tick = next_tick
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - a loop must never die silently
            logger.exception("probe loop for target %s crashed", target.id)

    async def _run_attempt(self, target: ProbeTarget) -> None:
        started_wall = self._wall_clock()
        started = self._clock()
        probe_fn = self._registry.get(target.protocol)
        if probe_fn is None:
            result = self._error_result(
                target, started_wall, started, "exec", "no probe for protocol"
            )
        else:
            guard = target.timeout_ms / 1000.0 + GUARD_EXTRA_SECONDS
            try:
                async with self._semaphore:
                    result = await asyncio.wait_for(probe_fn(target), guard)
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                result = self._error_result(
                    target, started_wall, started, "exec_timeout", "probe exceeded the hard guard"
                )
                logger.warning("probe for target %s hit the hard guard", target.id)
            except Exception as exc:
                result = self._error_result(
                    target, started_wall, started, "exec", _short_detail(exc)
                )
                logger.warning("probe for target %s failed: %s", target.id, _short_detail(exc))
        self._record(target, started_wall, result)

    def _error_result(
        self,
        target: ProbeTarget,
        started_wall: datetime,
        started: float,
        error_kind: str,
        detail: str,
    ) -> ProbeResult:
        return ProbeResult(
            target_id=target.id,
            protocol=target.protocol,
            started_at=to_iso_z(started_wall),
            duration_ms=max(0.0, (self._clock() - started) * 1000.0),
            outcome=Outcome.ERROR,
            timeout_ms=target.timeout_ms,
            error_kind=error_kind,
            error_detail=detail[:_MAX_ERROR_DETAIL],
        )

    # -- results -----------------------------------------------------------

    def _record(self, target: ProbeTarget, started_wall: datetime, result: ProbeResult) -> None:
        stamped = replace(
            result, device_id=self._device_id, load_test_id=self._load_test_id
        )
        self._ensure_ring(target)
        # The epoch is kept next to the row so recent() never re-parses ISO strings.
        self._recent[target.id].append((started_wall.timestamp(), stamped))
        self._buffer.append(stamped)
        self._enforce_hard_max()
        if len(self._buffer) >= self._flush_max:
            self._flush_signal.set()
        for callback in self._callbacks:
            try:
                callback(stamped)
            except Exception:
                logger.exception("on_result callback failed for target %s", target.id)

    def _enforce_hard_max(self) -> None:
        dropped = 0
        while len(self._buffer) + self._inflight > self._buffer_hard_max and self._buffer:
            self._buffer.popleft()
            dropped += 1
        if not dropped:
            return
        self._dropped_rows += dropped
        now = self._clock()
        if self._last_drop_log is None or now - self._last_drop_log >= DROP_LOG_SECONDS:
            self._last_drop_log = now
            logger.warning(
                "probe buffer full (%d rows), dropped %d oldest row(s), %d total",
                self._buffer_hard_max,
                dropped,
                self._dropped_rows,
            )

    def recent(self, target_id: int, seconds: float) -> list[ProbeResult]:
        """In-memory results of the last ``seconds``, oldest first."""
        ring = self._recent.get(target_id)
        if not ring:
            return []
        cutoff = self._wall_clock().timestamp() - seconds
        return [result for stamp, result in ring if stamp >= cutoff]

    def recent_all(self, seconds: float) -> dict[int, list[ProbeResult]]:
        """:meth:`recent` for every target that has in-memory history."""
        return {target_id: self.recent(target_id, seconds) for target_id in self._recent}

    def on_result(self, callback: Callable[[ProbeResult], None]) -> None:
        """Register a synchronous hook called for every recorded result."""
        self._callbacks.append(callback)

    def set_load_test_id(self, load_test_id: int | None) -> None:
        """Stamp every subsequent result with this load test id (or stop doing so)."""
        self._load_test_id = load_test_id

    def stats(self) -> SchedulerStats:
        return SchedulerStats(
            buffered_rows=len(self._buffer) + self._inflight,
            dropped_rows=self._dropped_rows,
            skipped_ticks=dict(self._skipped_ticks),
            flush_errors=self._flush_errors,
            last_flush_at=self._last_flush_at,
        )

    # -- flushing ----------------------------------------------------------

    async def _flush_loop(self) -> None:
        backoff = 0.0
        try:
            while self._running:
                if backoff > 0:
                    await self._sleep(backoff)
                else:
                    await self._sleep_until_flush(self._flush_seconds)
                if not self._running:
                    return
                ok = await self._flush_once()
                backoff = 0.0 if ok else _next_backoff(backoff)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("flush loop crashed")

    async def _sleep_until_flush(self, delay: float) -> None:
        """Sleep ``delay``, but wake early once the buffer reaches ``flush_max``."""
        if self._flush_signal.is_set():
            return
        sleeper = asyncio.ensure_future(self._sleep(delay))
        signal = asyncio.ensure_future(self._flush_signal.wait())
        try:
            await asyncio.wait({sleeper, signal}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, signal):
                task.cancel()
            await asyncio.gather(sleeper, signal, return_exceptions=True)

    async def _flush_once(self, *, final: bool = False) -> bool:
        """Write the buffered rows. Returns False when the write failed."""
        self._flush_signal.clear()
        if not self._buffer:
            return True
        rows = list(self._buffer)
        self._buffer.clear()
        self._inflight = len(rows)
        try:
            written = await _call(self._writer, self._db_path, rows)
        except asyncio.CancelledError:
            self._buffer.extendleft(reversed(rows))
            self._inflight = 0
            raise
        except Exception as exc:
            self._inflight = 0
            self._buffer.extendleft(reversed(rows))
            self._enforce_hard_max()
            self._flush_errors += 1
            level = logger.error if final else logger.warning
            level(
                "flushing %d probe result(s) failed (%s), %s",
                len(rows),
                _short_detail(exc),
                "rows lost" if final else "keeping them buffered",
            )
            return False
        self._inflight = 0
        self._last_flush_at = to_iso_z(self._wall_clock())
        logger.debug("flushed %d probe result(s) (%s written)", len(rows), written)
        return True

    # -- reloading ---------------------------------------------------------

    async def _reload_loop(self) -> None:
        try:
            while self._running:
                await self._sleep(self._reload_seconds)
                if not self._running:
                    return
                await self.reload_targets()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("target reload loop crashed")


def _default_target_loader(db_path: str) -> list[ProbeTarget]:
    """Default loader: every target in the DB, disabled ones included."""
    return list_targets(db_path, False)


def _interval_of(target: ProbeTarget) -> float:
    return max(float(target.interval_seconds), MIN_INTERVAL_SECONDS)


def _loop_relevant_change(previous: ProbeTarget, current: ProbeTarget) -> bool:
    return any(getattr(previous, field) != getattr(current, field) for field in _LOOP_FIELDS)


def _next_backoff(current: float) -> float:
    return min(MAX_FLUSH_BACKOFF_SECONDS, 1.0 if current <= 0 else current * 2.0)


async def _cancel_all(tasks: Iterable[asyncio.Task[None]]) -> None:
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
