"""Monitor sessions, observed time and data coverage (design spec §9).

Nothing is inferred for time the monitor did not run: a range is only "observed"
while a monitor session was alive and the given test type was not blocked.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from . import quality_db
from .db import TimeRange, query_blocked_periods
from .time_utils import parse_dt, to_iso_z

#: An open session is trusted until its last heartbeat plus one heartbeat period.
HEARTBEAT_GRACE_SECONDS = 30.0

Interval = tuple[datetime, datetime]


class SessionTracker:
    """Owns the lifecycle of the current monitor session."""

    def __init__(self) -> None:
        self.db_path: str | None = None
        self.session_id: int | None = None

    def start(self, db_path: str, device_id: str, app_version: str, now_iso: str) -> int:
        """Close sessions left open by an unclean stop, then open a new one."""
        quality_db.close_stale_sessions(db_path)
        self.db_path = db_path
        self.session_id = quality_db.start_session(db_path, device_id, app_version, now_iso)
        return self.session_id

    def heartbeat(self, now_iso: str) -> None:
        if self.db_path is None or self.session_id is None:
            return
        quality_db.heartbeat_session(self.db_path, self.session_id, now_iso)

    def stop(self, now_iso: str) -> None:
        if self.db_path is None or self.session_id is None:
            return
        quality_db.end_session(self.db_path, self.session_id, now_iso, "shutdown")


def _clip(interval: Interval, start: datetime, end: datetime) -> Interval | None:
    left = max(interval[0], start)
    right = min(interval[1], end)
    return (left, right) if right > left else None


def _merge(intervals: list[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
            continue
        merged.append((start, end))
    return merged


def _subtract(intervals: list[Interval], holes: list[Interval]) -> list[Interval]:
    result = list(intervals)
    for hole_start, hole_end in holes:
        remaining: list[Interval] = []
        for start, end in result:
            if hole_end <= start or hole_start >= end:
                remaining.append((start, end))
                continue
            if start < hole_start:
                remaining.append((start, hole_start))
            if hole_end < end:
                remaining.append((hole_end, end))
        result = remaining
    return _merge(result)


def _complement(intervals: list[Interval], start: datetime, end: datetime) -> list[Interval]:
    gaps: list[Interval] = []
    cursor = start
    for interval_start, interval_end in _merge(intervals):
        if interval_start > cursor:
            gaps.append((cursor, interval_start))
        cursor = max(cursor, interval_end)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def _session_intervals(db_path: str, start: datetime, end: datetime) -> list[Interval]:
    intervals: list[Interval] = []
    for row in quality_db.query_sessions(db_path, to_iso_z(start), to_iso_z(end)):
        session_start = parse_dt(row["started_at"])
        if row["ended_at"]:
            session_end = parse_dt(row["ended_at"])
        else:
            session_end = parse_dt(row["last_seen_at"]) + timedelta(seconds=HEARTBEAT_GRACE_SECONDS)
        clipped = _clip((session_start, session_end), start, end)
        if clipped is not None:
            intervals.append(clipped)
    return _merge(intervals)


def _blocked_intervals(
    db_path: str,
    start: datetime,
    end: datetime,
    test_type: str,
) -> list[tuple[datetime, datetime, str]]:
    tr = TimeRange(start_iso=to_iso_z(start), end_iso=to_iso_z(end))
    blocked: list[tuple[datetime, datetime, str]] = []
    for row in query_blocked_periods(db_path, tr=tr, test_type=test_type):
        period_end = parse_dt(row["ended_at"]) if row["ended_at"] else end
        clipped = _clip((parse_dt(row["started_at"]), period_end), start, end)
        if clipped is not None:
            blocked.append((clipped[0], clipped[1], row["reason"]))
    return blocked


def observed_intervals(
    db_path: str,
    start: datetime,
    end: datetime,
    test_type: str = "ping",
) -> list[Interval]:
    """Union of monitor sessions in the range, minus the blocked periods."""
    if end <= start:
        return []
    sessions = _session_intervals(db_path, start, end)
    blocked = [(s, e) for s, e, _ in _blocked_intervals(db_path, start, end, test_type)]
    return _subtract(sessions, blocked)


def clip_to_observed(intervals: list[Interval], observed: list[Interval]) -> list[Interval]:
    """Intersect intervals with the observed set (pure helper)."""
    result: list[Interval] = []
    for interval in intervals:
        for window in observed:
            clipped = _clip(interval, window[0], window[1])
            if clipped is not None:
                result.append(clipped)
    return sorted(result)


def total_seconds(intervals: list[Interval]) -> float:
    return sum((end - start).total_seconds() for start, end in intervals)


def coverage(
    db_path: str,
    start: datetime,
    end: datetime,
    test_type: str = "ping",
) -> dict[str, Any]:
    """Observed seconds, coverage percentage and the gaps with their reason.

    A database without any monitor session (pre-v2 history) has unknown
    coverage: `coverage_known` is False and nothing is extrapolated.
    """
    span = max(0.0, (end - start).total_seconds())
    if quality_db.count_sessions(db_path) == 0:
        return {
            "total_seconds": span,
            "observed_seconds": None,
            "coverage_pct": None,
            "coverage_known": False,
            "gaps": [],
        }

    sessions = _session_intervals(db_path, start, end)
    blocked = _blocked_intervals(db_path, start, end, test_type)
    observed = _subtract(sessions, [(s, e) for s, e, _ in blocked])
    observed_seconds = total_seconds(observed)

    gaps: list[tuple[datetime, datetime, str]] = [
        (gap_start, gap_end, "not_running")
        for gap_start, gap_end in _complement(sessions, start, end)
    ]
    for blocked_start, blocked_end, reason in blocked:
        for piece in clip_to_observed([(blocked_start, blocked_end)], sessions):
            gaps.append((piece[0], piece[1], reason))

    return {
        "total_seconds": span,
        "observed_seconds": observed_seconds,
        "coverage_pct": (observed_seconds / span * 100.0) if span > 0 else None,
        "coverage_known": True,
        "gaps": [
            {"from": to_iso_z(gap_start), "to": to_iso_z(gap_end), "reason": reason}
            for gap_start, gap_end, reason in sorted(gaps)
        ],
    }
