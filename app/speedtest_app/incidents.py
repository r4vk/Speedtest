"""Incident state machine per ``(target_id, protocol)`` (design spec §7).

Pure logic: the engine is fed the statistics of tumbling windows and returns
events; it never reads a clock, a database or the network, so tests are
deterministic. Persistence is the caller's job (``quality_engine.py``), which
turns a state into an ``incidents`` row with :func:`incident_row_from_state`.

Boundaries are window boundaries, never physical fault times: the incident's
``started_at`` is the start of the first degraded window and ``ended_at`` the
end of the last one, at the resolution recorded in ``window_seconds``.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping

from .stats import ProbeStats
from .time_utils import to_iso_z

Verdict = Literal["unknown", "healthy", "degraded", "outage"]
EventType = Literal["opened", "updated", "closed"]
CloseReason = Literal["recovered", "no_data", "shutdown"]

#: Verdicts that keep an incident alive.
BAD_VERDICTS: frozenset[str] = frozenset({"degraded", "outage"})

#: Maximum number of window verdicts kept in ``summary_json`` (spec §7).
MAX_SUMMARY_WINDOWS = 720

Key = tuple[int, str]


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _setting(values: Mapping[str, str], key: str, default: Any, cast: Callable[[str], Any]) -> Any:
    """Read one ``incident_*`` setting; missing, empty or invalid → default."""
    raw = values.get(key)
    if raw is None:
        return default
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class IncidentSettings:
    """Thresholds of spec §7, with the spec's defaults."""

    window_seconds: float = 10.0
    min_samples: int = 5
    loss_pct_threshold: float = 20.0
    outage_loss_pct: float = 100.0
    rtt_p95_ms_threshold: float = 150.0
    fail_streak_threshold: int = 3
    open_windows: int = 2
    stabilization_seconds: float = 60.0
    no_data_close_seconds: float = 300.0

    @staticmethod
    def from_settings(values: Mapping[str, str]) -> "IncidentSettings":
        """Parse the ``incident_*`` keys of the ``settings`` table.

        Unknown keys are ignored; a missing, empty or unparsable value keeps
        the default instead of disabling the incident engine.
        """
        defaults = IncidentSettings()
        return IncidentSettings(
            window_seconds=_setting(values, "incident_window_seconds", defaults.window_seconds, float),
            min_samples=_setting(values, "incident_min_samples", defaults.min_samples, int),
            loss_pct_threshold=_setting(
                values, "incident_loss_pct_threshold", defaults.loss_pct_threshold, float
            ),
            outage_loss_pct=_setting(values, "incident_outage_loss_pct", defaults.outage_loss_pct, float),
            rtt_p95_ms_threshold=_setting(
                values, "incident_rtt_p95_ms_threshold", defaults.rtt_p95_ms_threshold, float
            ),
            fail_streak_threshold=_setting(
                values, "incident_fail_streak_threshold", defaults.fail_streak_threshold, int
            ),
            open_windows=_setting(values, "incident_open_windows", defaults.open_windows, int),
            stabilization_seconds=_setting(
                values, "incident_stabilization_seconds", defaults.stabilization_seconds, float
            ),
            no_data_close_seconds=_setting(
                values, "incident_no_data_close_seconds", defaults.no_data_close_seconds, float
            ),
        )


def classify_window(stats: ProbeStats, s: IncidentSettings) -> Verdict:
    """Verdict of one evaluation window (spec §7).

    Too few measurable attempts — or none at all, however many ``error``
    attempts there were — is ``unknown``: a window nobody could measure is not
    a healthy one. Only loss reaching ``outage_loss_pct`` makes an ``outage``;
    the RTT and fail-streak thresholds can only make a window ``degraded``.
    """
    if stats.loss_pct is None or (stats.ok + stats.timeouts) < s.min_samples:
        return "unknown"
    if stats.loss_pct >= s.outage_loss_pct:
        return "outage"
    if stats.loss_pct >= s.loss_pct_threshold:
        return "degraded"
    if stats.rtt_p95_ms is not None and stats.rtt_p95_ms >= s.rtt_p95_ms_threshold:
        return "degraded"
    if stats.longest_fail_streak >= s.fail_streak_threshold:
        return "degraded"
    return "healthy"


@dataclass
class IncidentState:
    """Live state of one ``(target_id, protocol)`` key.

    Times are UTC ISO-Z strings, ready for the ``incidents`` columns.
    ``closed_at``/``close_reason`` are filled in on the closing window so a
    snapshot carries the whole row.
    """

    status: Literal["closed", "pending", "open"] = "closed"
    incident_id: int | None = None
    pending_count: int = 0
    pending_started_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    kind: Literal["degraded", "outage"] | None = None
    peak_loss_pct: float | None = None
    peak_p95_rtt_ms: float | None = None
    longest_fail_streak: int = 0
    windows_degraded: int = 0
    healthy_seconds: float = 0.0
    unknown_seconds: float = 0.0
    closed_at: str | None = None
    close_reason: CloseReason | None = None
    #: ``[window_start_iso, verdict, loss_pct, p95]`` per window, oldest first.
    windows: list[list[Any]] = field(default_factory=list)


@dataclass(frozen=True)
class IncidentEvent:
    """What the engine decided about one window. ``state`` is a snapshot copy."""

    type: EventType
    target_id: int
    protocol: str
    state: IncidentState
    close_reason: CloseReason | None
    window_start: datetime
    window_end: datetime


class IncidentEngine:
    """Tumbling-window state machine of spec §7.

    ``feed`` is called once per window and per key, in chronological order;
    keys are independent. ``probe_interval_seconds`` is asked for the target's
    sampling interval when an incident opens, so the stored incident records
    the resolution it was detected with.
    """

    def __init__(
        self,
        settings: IncidentSettings,
        *,
        probe_interval_seconds: Callable[[int], float],
    ) -> None:
        self.settings = settings
        self._probe_interval_seconds = probe_interval_seconds
        self._states: dict[Key, IncidentState] = {}
        self._intervals: dict[Key, float] = {}
        self._last_window: dict[Key, tuple[datetime, datetime]] = {}

    # -- public API ---------------------------------------------------------

    def state_for(self, target_id: int, protocol: str) -> IncidentState:
        """The live state of a key (created as ``closed`` when first asked for)."""
        return self._states.setdefault(self._key(target_id, protocol), IncidentState())

    def attach_incident_id(self, target_id: int, protocol: str, incident_id: int) -> None:
        """Remember the row id the persister created for the open incident."""
        self.state_for(target_id, protocol).incident_id = incident_id

    def probe_interval_for(self, target_id: int, protocol: str) -> float:
        """Sampling interval captured when the incident opened."""
        key = self._key(target_id, protocol)
        if key in self._intervals:
            return self._intervals[key]
        return float(self._probe_interval_seconds(key[0]))

    def feed(
        self,
        target_id: int,
        protocol: str,
        window_start: datetime,
        window_end: datetime,
        stats: ProbeStats,
    ) -> list[IncidentEvent]:
        """Apply one window to a key and return the events it caused.

        At most one event per window: ``opened`` when the window completed the
        pending run, ``closed`` when it completed stabilization or the no-data
        period, otherwise ``updated`` for every window of an open incident so
        the caller can refresh ``summary_json``.
        """
        key = self._key(target_id, protocol)
        state = self._states.setdefault(key, IncidentState())
        start = _as_utc(window_start)
        end = _as_utc(window_end)
        self._last_window[key] = (start, end)
        verdict = classify_window(stats, self.settings)
        seconds = max((end - start).total_seconds(), 0.0)

        if state.status == "open":
            return self._feed_open(key, state, verdict, start, end, stats, seconds)
        return self._feed_not_open(key, state, verdict, start, end, stats)

    def close_all(self, reason: CloseReason = "shutdown", *, at: datetime | None = None) -> list[IncidentEvent]:
        """Close every open incident (engine shutdown).

        Pending keys are dropped silently: nothing was persisted for them. The
        close time is the end of the last window fed for that key, unless ``at``
        says otherwise.
        """
        events: list[IncidentEvent] = []
        for key, state in list(self._states.items()):
            if state.status == "open":
                start, end = self._last_window.get(key, (None, None))
                closed_at = _as_utc(at) if at is not None else end
                events.append(self._close(key, state, reason, start, end, closed_at=closed_at))
            elif state.status == "pending":
                self._states[key] = IncidentState()
        return events

    # -- transitions --------------------------------------------------------

    def _feed_not_open(
        self,
        key: Key,
        state: IncidentState,
        verdict: Verdict,
        window_start: datetime,
        window_end: datetime,
        stats: ProbeStats,
    ) -> list[IncidentEvent]:
        if verdict in BAD_VERDICTS:
            if state.status != "pending":
                state.status = "pending"
                state.pending_started_at = to_iso_z(window_start)
            state.pending_count += 1
            self._record_window(state, window_start, verdict, stats)
            self._apply_bad_window(state, window_end, verdict, stats)
            if state.pending_count >= max(self.settings.open_windows, 1):
                state.status = "open"
                state.started_at = state.pending_started_at
                self._intervals[key] = float(self._probe_interval_seconds(key[0]))
                return [self._event("opened", key, state, window_start, window_end)]
            return []

        if verdict == "healthy" and state.status == "pending":
            # the run was not consecutive: forget it, nothing was persisted
            self._states[key] = IncidentState()
        # `unknown` windows neither count nor reset while pending
        return []

    def _feed_open(
        self,
        key: Key,
        state: IncidentState,
        verdict: Verdict,
        window_start: datetime,
        window_end: datetime,
        stats: ProbeStats,
        seconds: float,
    ) -> list[IncidentEvent]:
        self._record_window(state, window_start, verdict, stats)

        if verdict in BAD_VERDICTS:
            self._apply_bad_window(state, window_end, verdict, stats)
            return [self._event("updated", key, state, window_start, window_end)]

        if verdict == "healthy":
            state.unknown_seconds = 0.0
            state.healthy_seconds += seconds
            if state.healthy_seconds >= self.settings.stabilization_seconds:
                return [self._close(key, state, "recovered", window_start, window_end)]
            return [self._event("updated", key, state, window_start, window_end)]

        # unknown: the link may still be broken, we simply cannot tell
        state.unknown_seconds += seconds
        if state.unknown_seconds >= self.settings.no_data_close_seconds:
            return [self._close(key, state, "no_data", window_start, window_end)]
        return [self._event("updated", key, state, window_start, window_end)]

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _key(target_id: int, protocol: str) -> Key:
        return (int(target_id), str(protocol))

    @staticmethod
    def _record_window(
        state: IncidentState,
        window_start: datetime,
        verdict: Verdict,
        stats: ProbeStats,
    ) -> None:
        state.windows.append([to_iso_z(window_start), verdict, stats.loss_pct, stats.rtt_p95_ms])
        if len(state.windows) > MAX_SUMMARY_WINDOWS:
            del state.windows[: len(state.windows) - MAX_SUMMARY_WINDOWS]

    @staticmethod
    def _apply_bad_window(
        state: IncidentState,
        window_end: datetime,
        verdict: Verdict,
        stats: ProbeStats,
    ) -> None:
        state.ended_at = to_iso_z(window_end)
        state.windows_degraded += 1
        if stats.loss_pct is not None:
            state.peak_loss_pct = (
                stats.loss_pct if state.peak_loss_pct is None else max(state.peak_loss_pct, stats.loss_pct)
            )
        if stats.rtt_p95_ms is not None:
            state.peak_p95_rtt_ms = (
                stats.rtt_p95_ms
                if state.peak_p95_rtt_ms is None
                else max(state.peak_p95_rtt_ms, stats.rtt_p95_ms)
            )
        state.longest_fail_streak = max(state.longest_fail_streak, stats.longest_fail_streak)
        if verdict == "outage":
            state.kind = "outage"  # escalation only, never back to `degraded`
        elif state.kind is None:
            state.kind = "degraded"
        # stabilization and the no-data period both have to be continuous
        state.healthy_seconds = 0.0
        state.unknown_seconds = 0.0

    def _event(
        self,
        event_type: EventType,
        key: Key,
        state: IncidentState,
        window_start: datetime | None,
        window_end: datetime | None,
    ) -> IncidentEvent:
        return IncidentEvent(
            type=event_type,
            target_id=key[0],
            protocol=key[1],
            state=copy.deepcopy(state),
            close_reason=state.close_reason,
            window_start=window_start,
            window_end=window_end,
        )

    def _close(
        self,
        key: Key,
        state: IncidentState,
        reason: CloseReason,
        window_start: datetime | None,
        window_end: datetime | None,
        *,
        closed_at: datetime | None = None,
    ) -> IncidentEvent:
        """Close the incident; ``ended_at`` keeps the last degraded window end."""
        state.status = "closed"
        state.close_reason = reason
        moment = closed_at if closed_at is not None else window_end
        state.closed_at = to_iso_z(moment) if moment is not None else None
        event = self._event("closed", key, state, window_start, window_end)
        self._states[key] = IncidentState()
        return event


def incident_row_from_state(
    state: IncidentState,
    target_id: int,
    protocol: str,
    settings: IncidentSettings,
    probe_interval: float,
) -> dict[str, Any]:
    """The ``incidents`` column dict of a state snapshot (spec §3, §7).

    Only real columns are produced: ``quality_db.insert_incident`` /
    ``update_incident`` reject anything else.
    """
    return {
        "target_id": int(target_id),
        "protocol": str(protocol),
        "kind": state.kind or "degraded",
        "started_at": state.started_at,
        "ended_at": state.ended_at,
        "closed_at": state.closed_at,
        "close_reason": state.close_reason,
        "window_seconds": int(settings.window_seconds),
        "probe_interval_seconds": float(probe_interval),
        "peak_loss_pct": state.peak_loss_pct,
        "peak_p95_rtt_ms": state.peak_p95_rtt_ms,
        "longest_fail_streak": state.longest_fail_streak,
        "windows_degraded": state.windows_degraded,
        "summary_json": json.dumps(state.windows),
    }
