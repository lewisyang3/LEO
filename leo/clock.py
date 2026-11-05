"""Clocks (ROADMAP 0.5). Code that needs "today" takes a `Clock` or a date, never `date.today()`."""

import datetime
from dataclasses import dataclass

from leo.config import Settings, get_settings
from leo.interfaces import Clock


@dataclass(frozen=True)
class FixedClock:
    day: datetime.date

    def today(self) -> datetime.date:
        return self.day


class SystemClock:
    def today(self) -> datetime.date:
        return datetime.date.today()


def get_clock(settings: Settings | None = None) -> Clock:
    """`LEO_TODAY` when set (the demo pins 2026-11-02), else the system date."""
    settings = settings or get_settings()
    if settings.leo_today is not None:
        return FixedClock(settings.leo_today)
    return SystemClock()
