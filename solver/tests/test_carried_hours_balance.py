"""Regression tests for a carried hours balance: a remainder of the shifts of the month that produced it, which may
since have been shortened or retired, so it imports under any later shift configuration."""
from __future__ import annotations

import io
import json

import pytest

from nurse_roster.model import build_model, solve
from nurse_roster.problem import MAX_HOURS, MAX_SHIFT_HOURS, Nurse, Problem
from tests.test_core import hours_general, mini, quiet


def month(nurse: Nurse, shift_hours: dict[str, int]) -> Problem:
    """A 31-day month from a Monday (27 working days) with the hours rule on, no overtime,
    and the given shifts as the department's."""
    p = mini(31, list(shift_hours), [nurse])
    p.general = hours_general(shift_hours=shift_hours, forbidden_successions=[], overtime_month_cap=0)
    return p


def plan(p: Problem) -> int:
    """Solve the month; the hours worked in shifts."""
    sol = solve(build_model(p), p, time_limit=10, workers=4)
    assert sol.status == "OPTIMAL" and sol.hard_errors == [], (sol.status, sol.hard_errors)
    h, sh = p.general.shift_hours, p.department.shifts
    return sum(h[sh[v]] for v in sol.assignment[0] if v >= 0)


def imported(p: Problem) -> Problem:
    """The next month as the CLI reads it: through the JSON contract and `check()`."""
    buf = io.StringIO()
    p.to_json(buf)
    return Problem.from_dict(json.loads(buf.getvalue()))


# ---- 1: the debt of a 12 h month imports after the shift is shortened, or retired -----------------
@pytest.mark.parametrize("next_shifts", [{"D": 8}, {"C": 8}], ids=["shortened", "retired"])
def test_carried_debt_imports_after_the_shift_changes(next_shifts):
    a = Nurse("a", breastfeeding=True)                         # 27 x 7 h = 189 h owed
    p1 = month(a, {"D": 12})
    worked = plan(p1)                                          # 15 D: 16 would be 3 h over the cap
    assert p1.required_hours(a) == 189 and worked == 180 and p1.carry_over(a, worked) == 9
    p2 = imported(month(Nurse("a", breastfeeding=True, hours_balance=9), next_shifts))
    b = p2.nurses[0]                                           # used to be rejected (9 >= 8) unless
    assert p2.required_hours(b) == 198 and p2.max_shortfall(b) == 7   # an unused 12 h shift stayed
    worked = plan(p2)                                          # 24 x 8 h: 25 would be 2 h over
    assert worked == 192 and p2.carry_over(b, worked) == 6     # the new remainder bound (< 8)


# ---- 2: the bound is the longest shift any configuration can define, not this month's ----------
@pytest.mark.parametrize("hours", [1, 8, 24])
def test_hours_balance_bound_does_not_depend_on_this_months_shifts(hours):
    for ok in (MAX_SHIFT_HOURS - 1, 1 - MAX_SHIFT_HOURS, MAX_SHIFT_HOURS, 10 * MAX_SHIFT_HOURS, MAX_HOURS):
        imported(month(Nurse("a", hours_balance=ok), {"D": hours}))   # a recorded debt may exceed
    for bad in (MAX_HOURS + 1, -MAX_HOURS - 1):                        # any shift
        with pytest.raises(ValueError, match=f"hours_balance must be an integer within ±{MAX_HOURS}"):
            imported(month(Nurse("a", hours_balance=bad), {"D": hours}))


# ---- 3: whatever last month's shifts, the exported balance imports next month ---------------------
@pytest.mark.parametrize("last,next", [(24, 8), (8, 24), (23, 12)])   # shifts long enough to cover
def test_every_exported_balance_imports(last, next):                   # the contract in 31 days
    a = Nurse("a", breastfeeding=True)
    p1 = month(a, {"D": last})
    carry = p1.carry_over(a, plan(p1))
    assert 0 < carry < last                                    # a remainder below last month's shift
    p2 = imported(month(Nurse("a", hours_balance=carry), {"D": next}))
    assert abs(p2.carry_over(p2.nurses[0], plan(p2))) < next   # and below this month's on the way out
