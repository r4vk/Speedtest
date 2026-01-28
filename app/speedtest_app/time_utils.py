from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    return datetime.now().astimezone()


def local_tz():
    return local_now().tzinfo or timezone.utc


def to_iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def to_local_iso(dt: datetime) -> str:
    """
    Local time without offset, ISO-ish: YYYY-MM-DDTHH:MM:SS (no timezone suffix).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    d = dt.astimezone(local_tz()).replace(microsecond=0)
    return d.replace(tzinfo=None).isoformat()


def to_local_display(dt: datetime) -> str:
    """
    Local time without offset: YYYY-MM-DD HH:MM:SS
    """
    return to_local_iso(dt).replace("T", " ")


def parse_dt(value: str) -> datetime:
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        # traktuj czasy bez strefy jako lokalne (UX w DSM)
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class ParsedRange:
    start: datetime
    end: datetime


def parse_range(from_q: str | None, to_q: str | None, default_hours: int = 24) -> ParsedRange:
    end = parse_dt(to_q) if to_q else utc_now()
    start = parse_dt(from_q) if from_q else end - timedelta(hours=default_hours)
    if start > end:
        start, end = end, start
    return ParsedRange(start=start, end=end)
