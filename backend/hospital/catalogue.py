"""What the catalogue starts with. The hours are the reviewed set the solver was shaped on; the forbidden
successions in `hospital.rules.DEFAULT_RULES` name these codes. Names, times and colours are ours for the
CNO to correct, and hospital/tests/test_shifts.py holds the codes and hours to the solver's."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta

from django.db import transaction
from hijridate import Gregorian, Hijri
from nurse_roster.calendar import parse_month

from hospital.models import FixedRamadanMonth, LeaveType, Shift

DEFAULT_SHIFTS = [
    ("D", "Day", 12, time(7), time(19), "#0284c7"),
    ("N", "Night", 12, time(19), time(7), "#4338ca"),
    ("A", "Morning", 8, time(7), time(15), "#16a34a"),
    ("B", "Evening", 8, time(15), time(23), "#d97706"),
    ("C", "Late night", 8, time(23), time(7), "#7c3aed"),
]

# Every type counts the same for a nurse's hours, so nothing here may suggest otherwise, such as "Unpaid".
DEFAULT_LEAVE_TYPES = ["Annual", "Compassionate", "Educational", "Emergency", "Maternity", "Sick"]


def seed_catalogue() -> None:
    """Only into an empty table: nothing here is ever deleted, so empty means never seeded, and a shift
    the CNO has renamed must not come back under its old name."""
    with transaction.atomic():
        if not Shift.objects.exists():
            for code, display_name, hours, start_time, end_time, colour in DEFAULT_SHIFTS:
                Shift.objects.create(code=code, display_name=display_name, hours=hours, start_time=start_time,
                                     end_time=end_time, colour=colour)
        if not LeaveType.objects.exists():
            LeaveType.objects.bulk_create(LeaveType(name=name) for name in DEFAULT_LEAVE_TYPES)


def derived_ramadan_dates(month: str) -> list[date]:
    """The solver's own derivation, so a page and a solve cannot disagree. Only its calendar module is
    used here: the solver container is still the only place that solves."""
    info = parse_month(month)
    return [info.date(day) for day in info.ramadan_days]


def ramadan_dates(month: str) -> list[date]:
    """The Ramadan days of a month ("YYYY-MM"): the CNO's when fixed, else the calendar's."""
    fixed = FixedRamadanMonth.objects.filter(month=month).first()
    return fixed.dates if fixed else derived_ramadan_dates(month)


def month_of(day: date) -> str:
    return day.strftime("%Y-%m")


def shift_month(month: str, step: int) -> str:
    """The "YYYY-MM" `step` months after `month` (before it when negative)."""
    year, index = divmod(int(month[:4]) * 12 + int(month[5:]) - 1 + step, 12)
    return f"{year:04d}-{index + 1:02d}"


@dataclass(frozen=True, slots=True)
class CalendarRamadan:
    hijri_year: int
    days: list[date]  # The calendar's, in order: one unbroken run.

    @property
    def months(self) -> list[str]:
        return sorted({month_of(day) for day in self.days})


def calendar_ramadans(year: int) -> list[CalendarRamadan]:
    """Every Ramadan with a day in the Gregorian `year`, in date order, by the calendar. The days are the solver's month
    derivation, read from December before to January after: a Ramadan is at most 30 days, so one with a day in `year`
    lies within them. hijridate is used directly only to name each by its Hijri year."""
    days = [day for step in range(-1, 13) for day in derived_ramadan_dates(shift_month(f"{year:04d}-01", step))]
    runs: list[list[date]] = []
    for day in days:
        if runs and day == runs[-1][-1] + timedelta(days=1):
            runs[-1].append(day)
        else:
            runs.append([day])
    return [CalendarRamadan(Gregorian(run[0].year, run[0].month, run[0].day).to_hijri().year, run)
            for run in runs if any(day.year == year for day in run)]


def calendar_arafat_days(year: int) -> list[date]:
    """Every Arafat Day (9 Dhu al-Hijjah) in the Gregorian `year` by the calendar, in date order: one, or two in a year
    such as 2039. A Hijri year with no day in `year` cannot hold one, so only those overlapping it are asked."""
    days = []
    for hijri_year in range(Gregorian(year, 1, 1).to_hijri().year, Gregorian(year, 12, 31).to_hijri().year + 1):
        try:
            day = Hijri(hijri_year, 12, 9).to_gregorian()
        except (ValueError, OverflowError):  # Beyond the calendar's data, at its ends.
            continue
        if day.year == year:
            days.append(date(day.year, day.month, day.day))
    return days


def calendar_ramadan(hijri_year: int) -> CalendarRamadan | None:
    """Ramadan `hijri_year` by the calendar, or None when the calendar's data does not reach it. hijridate only finds
    the Gregorian year; the days are `calendar_ramadans`'."""
    try:
        year = Hijri(hijri_year, 9, 1).to_gregorian().year
    except (ValueError, OverflowError):
        return None
    try:
        return next((ramadan for ramadan in calendar_ramadans(year) if ramadan.hijri_year == hijri_year), None)
    except ValueError:  # A neighbouring month beyond the solver's calendar.
        return None
