"""Gregorian month facts needed by the solver, including which days fall in Ramadan.

Uses the Umm al-Qura calendar (hijridate). Ramadan is Hijri month 9 and can span two
Gregorian months, so the result is a list of 0-based day indices within the requested month.
"""
from __future__ import annotations

import calendar as _cal
import re
from dataclasses import dataclass
from datetime import date, timedelta

from hijridate import Gregorian

RAMADAN = 9
_MONTH = re.compile(r"(\d{4})-(\d{2})")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


@dataclass(frozen=True, slots=True)
class MonthInfo:
    year: int
    month: int
    days: int
    first_weekday: int          # 0 = Monday
    ramadan_days: list[int]     # 0-based day indices that fall in Ramadan

    # ---- dates <-> 0-based day indices (a day outside the month is refused; `Problem.day_words` says one in words) ----
    @property
    def label(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    def iso(self, d: int) -> str:
        """Day index -> ISO date, e.g. 4 -> "2026-03-05"."""
        if not isinstance(d, int) or isinstance(d, bool) or not 0 <= d < self.days:
            raise ValueError(f"day index {d!r} outside 0..{self.days - 1}")
        return f"{self.label}-{d + 1:02d}"

    def date(self, d: int) -> date:
        return date(self.year, self.month, d + 1)

    def index(self, text: object, where: str) -> int:
        """ISO date within this month -> day index; ValueError names `where` otherwise."""
        x = parse_date(text, where)
        if (x.year, x.month) != (self.year, self.month):
            raise ValueError(f"{where}: {text} is outside {self.label}")
        return x.day - 1

    def dates(self) -> list[str]:
        return [self.iso(d) for d in range(self.days)]

    # ---- a planning cutoff: a day of the month, or the day after it (= the whole month elapsed)
    @property
    def next_first(self) -> str:
        return (self.date(self.days - 1) + timedelta(days=1)).isoformat()

    def cutoff_iso(self, d: int) -> str:
        return self.next_first if d == self.days else self.iso(d)

    def cutoff_index(self, text: object, where: str) -> int:
        """Like `index`, but the first day after the month is accepted too and maps to
        `days`: every day of the month has happened (a month-end reconciliation)."""
        if text == self.next_first:
            return self.days
        x = parse_date(text, where)
        if (x.year, x.month) != (self.year, self.month):
            raise ValueError(f"{where}: {text} is outside {self.label} (the day after it, "
                             f"{self.next_first}, marks the whole month as elapsed)")
        return x.day - 1


# Umm al-Qura data in hijridate covers these Gregorian dates; outside them the
# converter raises OverflowError, which we turn into a clear ValueError.
GREGORIAN_RANGE = (date(1924, 8, 1), date(2077, 11, 16))


def month_info(year: int, month: int) -> MonthInfo:
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1..12, got {month}")
    days = _cal.monthrange(year, month)[1]
    first_weekday = date(year, month, 1).weekday()
    lo, hi = GREGORIAN_RANGE
    if date(year, month, 1) < lo or date(year, month, days) > hi:
        raise ValueError(f"{year}-{month:02d}: Ramadan dates are only known for "
                         f"{lo.isoformat()} .. {hi.isoformat()}")
    ramadan = [d for d in range(days)
               if Gregorian(year, month, d + 1).to_hijri().month == RAMADAN]
    return MonthInfo(year, month, days, first_weekday, ramadan)


def parse_date(text: object, where: str) -> date:
    """Strict "YYYY-MM-DD" -> date; ValueError names `where` otherwise."""
    m = _DATE.fullmatch(text) if isinstance(text, str) else None
    if m is None:
        raise ValueError(f"{where}: {text!r} is not a date (YYYY-MM-DD)")
    y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        raise ValueError(f"{where}: {text} is not a calendar date") from None


def parse_month(text: object) -> MonthInfo:
    """Strict "YYYY-MM" -> month facts (ValueError otherwise)."""
    m = _MONTH.fullmatch(text) if isinstance(text, str) else None
    if m is None:
        raise ValueError(f"month must be YYYY-MM, got {text!r}")
    return month_info(int(m.group(1)), int(m.group(2)))
