"""Expected maintenance windows (design spec 2026-09-23 §4).

Pure logic: the matcher is handed the rules and both timestamps and never reads
a clock, a database or a setting, so tests are deterministic. Persistence is the
caller's job — ``quality_engine`` for ``incidents``, ``availability`` for
``connectivity_periods``.

A rule is a local wall-clock window. ``_is_blocked_by_schedule`` in
``scheduler.py`` answers a similar question by comparing ``"HH:MM"`` strings
against ``now()``, which has no notion of a window instance on a given day and
none of DST; this module builds concrete local intervals instead, which is why
the two do not share code.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .time_utils import local_tz


@dataclass(frozen=True)
class ExpectedWindow:
    """One recurring window, in local wall-clock terms."""

    id: int
    name: str
    time_from: str
    time_to: str
    days: frozenset[int]          # 0=Monday .. 6=Sunday
    target_id: int | None
    note: str | None = None


def _zone() -> tzinfo:
    """The local zone as a *rule-aware* ``tzinfo``.

    ``time_utils.local_tz()`` hands back ``datetime.now().astimezone().tzinfo``
    — a fixed offset, frozen at whatever is in force the moment it is called.
    That is right for stamping "now" and wrong for a window on a day six months
    away: under a fixed +02:00 every 02:30 exists, including the one March
    skips, and the hour October runs twice happens once.

    ``TZ`` names the zone; the Dockerfile and ``docker-compose.yml`` both set
    ``TZ=Europe/Warsaw`` and the image installs ``tzdata``. When it is missing
    or unknown the fixed offset is the honest fallback — exact for a zone that
    never shifts, and no worse than what the rest of the app already does.
    """
    name = os.environ.get("TZ", "").strip().lstrip(":")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return local_tz()


def parse_hhmm(value: Any) -> time | None:
    """``'HH:MM'`` → ``time``, or ``None`` for anything else. Never raises."""
    text = str(value or "").strip()
    if len(text) != 5 or text[2] != ":":
        return None
    try:
        hh, mm = int(text[0:2]), int(text[3:5])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return time(hh, mm)


def parse_days(value: Any) -> frozenset[int]:
    """JSON array (or any iterable) of 0–6 → frozenset. Junk yields an empty set."""
    raw: Any = value
    if not isinstance(raw, (list, tuple, set, frozenset)):
        try:
            raw = json.loads(str(value))
        except (TypeError, ValueError):
            return frozenset()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    days: set[int] = set()
    for item in raw:
        try:
            day = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6:
            days.add(day)
    return frozenset(days)


def parse_windows(rows: Iterable[Mapping[str, Any]]) -> list[ExpectedWindow]:
    """DB rows → rules, dropping the disabled ones.

    A broken row is kept but will never match (:func:`match` skips it), so one
    bad rule cannot hide the rest.
    """
    windows: list[ExpectedWindow] = []
    for row in rows:
        if not bool(row.get("enabled", 1)):
            continue
        target_id = row.get("target_id")
        windows.append(
            ExpectedWindow(
                id=int(row["id"]),
                name=str(row.get("name") or ""),
                time_from=str(row.get("time_from") or ""),
                time_to=str(row.get("time_to") or ""),
                days=parse_days(row.get("days")),
                target_id=int(target_id) if target_id is not None else None,
                note=row.get("note") or None,
            )
        )
    windows.sort(key=lambda w: w.id)
    return windows


def _local_instants(day: date, at: time, tz: tzinfo) -> list[datetime]:
    """Every instant at which the clock on the wall reads ``at`` on ``day``.

    Normally one. None at all in the hour a spring-forward skips — that window
    instance simply does not happen. Two on the autumn day, when the hour runs
    twice; both are returned, oldest first.

    They come back as UTC on purpose. Two datetimes carrying one zone compare
    by wall clock and ignore ``fold``, so the repeated 02:40 would sort as
    "after" the *second* 02:20 as readily as the first — every comparison
    downstream has to be about instants, not clock faces.
    """
    naive = datetime.combine(day, at)
    out: list[datetime] = []
    for fold in (0, 1):
        moment = naive.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc)
        # A local time the clock skips round-trips to a different wall clock.
        if moment.astimezone(tz).replace(tzinfo=None) != naive:
            continue
        if moment not in out:
            out.append(moment)
    return out


def match(
    windows: Sequence[ExpectedWindow],
    started_at: datetime | None,
    ended_at: datetime | None,
    target_id: int | None = None,
) -> ExpectedWindow | None:
    """The first rule that fully contains ``[started_at, ended_at]``, or ``None``.

    Containment, not overlap: an outage that outlasts the window is a real one
    that happened to begin during a reboot, and must keep counting.
    """
    if started_at is None or ended_at is None or ended_at < started_at:
        return None
    tz = _zone()
    start_local = started_at.astimezone(tz)
    for window in windows:
        if window.target_id is not None and window.target_id != target_id:
            continue
        begin_at = parse_hhmm(window.time_from)
        end_at = parse_hhmm(window.time_to)
        if begin_at is None or end_at is None or begin_at == end_at or not window.days:
            continue
        overnight = end_at < begin_at
        # The instance covering this outage starts either on the outage's own
        # local day or, for an overnight window, on the day before.
        for offset in (0, -1) if overnight else (0,):
            day = (start_local + timedelta(days=offset)).date()
            if day.weekday() not in window.days:
                continue
            end_day = day + timedelta(days=1) if overnight else day
            ends = _local_instants(end_day, end_at, tz)
            for begin in _local_instants(day, begin_at, tz):
                # One instance closes at its own next end, not at some later
                # one: on the autumn day a twenty-minute window occurs twice
                # and stays twenty minutes long, rather than stretching across
                # the whole repeated hour.
                end = next((moment for moment in ends if moment > begin), None)
                if end is not None and begin <= started_at and ended_at <= end:
                    return window
    return None
