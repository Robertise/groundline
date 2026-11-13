from datetime import datetime, timezone, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...  # always timezone-aware UTC


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    def __init__(self, start: datetime):
        if start.tzinfo is None or start.tzinfo.utcoffset(start) is None:
            raise ValueError("start datetime must be timezone-aware")
        self._current: datetime = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)


def iso(dt: datetime) -> str:
    """Fixed-width UTC string, e.g. 2026-11-13T07:12:53.000000Z. Raise ValueError if dt is naive."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("dt must be timezone-aware")
    utc_dt = dt.astimezone(timezone.utc)
    return f"{utc_dt.strftime('%Y-%m-%dT%H:%M:%S')}.{utc_dt.microsecond:06d}Z"
