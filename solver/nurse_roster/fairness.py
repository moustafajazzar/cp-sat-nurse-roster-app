"""Shared fairness arithmetic used by the model, the validator and the reports.

Integer units: shares and targets in thousandths of a shift, balances in tenths. A nurse on probation takes part in
none of it (`problem.counted`): she is not flexible, has no Friday share and no overtime share, adds nothing to the
unit's overtime or to the nurses a share is divided among, and her three balances go out at 0.
"""
from __future__ import annotations

from .problem import Nurse, Problem, barred_days, counted


def fair_shifts(p: Problem) -> list[str]:
    dep = p.department
    return [s for s in dep.fair_shifts if s in dep.shifts]


def share_m(p: Problem) -> dict[str, int]:
    """Each fair shift's share of demand, in thousandths, from headcount midpoints. The
    shares sum to exactly 1000 (the rounding residual goes to the largest share), so a
    nurse's per-shift targets add up to the shifts she works and no bias is carried into
    the night balance month after month."""
    dep, demand = p.department, {}
    for hc in dep.headcounts:
        if hc.shift in fair_shifts(p):
            demand[hc.shift] = sum((hc.weekend_min + hc.weekend_max) if p.is_weekend(d)
                                   else (hc.weekday_min + hc.weekday_max) for d in range(p.days))
    names = fair_shifts(p)
    if not names:
        return {}
    tot = sum(demand.values())
    if tot == 0:                                             # no demand at all: equal shares
        demand = {s: 1 for s in names}
        tot = len(names)
    out = {s: round(1000 * demand.get(s, 0) / tot) for s in names}
    big = max(names, key=lambda s: demand.get(s, 0))
    out[big] += 1000 - sum(out.values())
    return out


def coverage_divisor(p: Problem) -> int:
    """Largest number of headcounts one shift satisfies at once (1 without `also_counts`):
    summed headcounts over-count the people needed by up to this factor."""
    return max(coverage(p).values(), default=1)


def coverage(p: Problem) -> dict[str, int]:
    """Headcounts each shift counts toward, for the shifts some nurse may actually work: an
    `also_counts` entry nobody is allowed to work satisfies nothing and must not lower the
    people needed (or the fair share). A nurse on probation satisfies no headcount: her shifts are not counted."""
    usable = set()
    for n in counted(p):
        usable |= set(p.allowed(p.nurses[n]))
    cover: dict[str, int] = {}
    for hc in p.department.headcounts:
        for s in [hc.shift] + list(hc.also_counts):
            if s in usable:
                cover[s] = cover.get(s, 0) + 1
    return cover


def weekend_available(p: Problem, nurse: Nurse) -> tuple[int, int]:
    """(weekend days the nurse is not on leave, weekend days in the month). A day barred by an expired
    required skill (`barred_days`) counts as leave here: she cannot work it either."""
    leave = set(nurse.unavailable) | set(barred_days(p, nurse))
    we = [d for d in range(p.days) if p.is_weekend(d)]
    return sum(1 for d in we if d not in leave), len(we)


def friday_slots_m(p: Problem) -> float:
    """The problem-wide part of `friday_expected_m` (weekend slots per nurse, thousandths,
    before proration): O(nurses) through `coverage`, so callers looping over nurses pass it
    in as `per` and pay it once. Divided among the counted nurses: a nurse on probation takes no weekend slot."""
    dep, n = p.department, max(1, len(counted(p)))
    slots = sum((hc.weekend_min + hc.weekend_max) / 2 for hc in dep.headcounts
                for d in range(p.days) if p.is_weekend(d)) / coverage_divisor(p)
    return 1000 * slots / n


def friday_expected_m(p: Problem, nurse: Nurse | None = None, per: float | None = None) -> int:
    """Fair number of weekend (Friday) shifts per nurse, in thousandths of a shift. For a
    given nurse it is prorated by the weekend days she is not on leave or barred, so a month of leave
    neither penalises her nor builds a Friday debt in the carried balance. With overlapping
    coverage (`also_counts`) one person can satisfy several headcounts, so the summed
    midpoints are divided by that multiplicity: the share is of people, not of headcounts.
    `per`: `friday_slots_m(p)` when the caller already has it."""
    if per is None:
        per = friday_slots_m(p)
    if nurse is None:
        return round(per)
    avail, total = weekend_available(p, nurse)
    return round(per * avail / total) if total else 0


def flexible(p: Problem, nurse: Nurse) -> bool:
    """True when the nurse may work every fair shift (so a D/N mix can be fair); never for a nurse on probation, who is
    left out of the day and night fairness."""
    return not nurse.on_probation and set(fair_shifts(p)) <= set(p.allowed(nurse))


def adj_tenths(balance: float, p: Problem) -> int:
    """Carried balance (shifts, + = ahead of fair share) clamped to the per-month cap, in
    tenths of a shift. 0 when cross-month fairness is off."""
    dep = p.department
    if not dep.carry_fairness:
        return 0
    cap = dep.carry_cap
    return round(max(-cap, min(cap, balance)) * 10)


def shift_adj_tenths(p: Problem, nurse: Nurse, shift: str) -> int:
    """Target adjustment for `shift`: the night balance lowers the night target and raises
    the day target by the same amount (they mirror in a two-shift unit)."""
    dep = p.department
    a = adj_tenths(nurse.night_balance, p)
    if shift == dep.night_shift:
        return a
    if shift == dep.day_shift:
        return -a
    return 0


def balances_out(p: Problem, nurse: Nurse, row: list[int], *, share: dict[str, int] | None = None,
                 per: float | None = None) -> tuple[float, float]:
    """(night_balance, friday_balance) to carry into next month, given this month's row.
    The night balance moves only when nights are a fair shift for this nurse (otherwise her
    nights have no fair share to compare with); the Friday balance compares her Fridays
    with the expectation prorated for her leave. `share` / `per`: `share_m(p)` and
    `friday_slots_m(p)` when the caller loops over nurses and has them already. A nurse on probation carries 0 and 0
    out, whatever came in: she starts level with her colleagues when it ends."""
    if nurse.on_probation:
        return 0.0, 0.0
    dep = p.department
    if share is None:
        share = share_m(p)
    idx = {s: i for i, s in enumerate(dep.shifts)}
    k = sum(1 for v in row if v >= 0)
    night_out = nurse.night_balance
    if flexible(p, nurse) and dep.night_shift in fair_shifts(p):
        nights = sum(1 for v in row if v == idx[dep.night_shift])
        night_out += nights - share[dep.night_shift] * k / 1000
    friday_out = nurse.friday_balance
    if dep.fair_weekends:
        fridays = sum(1 for d, v in enumerate(row) if v >= 0 and p.is_weekend(d))
        friday_out += fridays - friday_expected_m(p, nurse, per) / 1000
    return round(night_out, 2), round(friday_out, 2)


# ---- the overtime share ------------------------------------------------------------------------
# A nurse's share of the unit's overtime O is O x r / R, r the hours she owes this month
# (`Problem.required_hours`) and R the hours every nurse of the problem owes. Her target is the
# share less her carried `overtime_balance`, held within ±`overtime_carry_cap`, when the unit carries
# fairness. Her priced hours are her overtime above the target, beyond `overtime_tolerance`, in
# whole hours rounded down (the target rounded up to a whole hour: a part hour is never priced),
# and never more than her overtime. Integer arithmetic in hundredths of an hour scaled by R, so the
# model (`model.build_model`) and the validator (`validate`) count exactly the same hours.

def overtime_owed(p: Problem) -> int:
    """R: the hours every counted nurse of the problem owes this month (a nurse on probation has no share)."""
    return sum(p.required_hours(p.nurses[n]) for n in counted(p))


def unit_overtime(p: Problem, overtime: list[int]) -> int:
    """O: the unit's overtime this month, from each nurse's (in the nurses' order): the counted nurses' only. A nurse
    on probation's rounding overtime is on her own record, outside the department cap and the overtime share."""
    return sum(overtime[n] for n in counted(p))


def overtime_adj_100(p: Problem, nurse: Nurse) -> int:
    """Her carried overtime balance (hours, + = above her share so far) held within the unit's limit, in
    hundredths of an hour; 0 when the unit does not carry fairness between months."""
    dep = p.department
    if not dep.carry_fairness:
        return 0
    cap = dep.overtime_carry_cap
    return round(max(-cap, min(cap, nurse.overtime_balance)) * 100)


def overtime_share(p: Problem, nurse: Nurse, total: int, owed: int) -> float:
    """Her share, in hours, of `total`, the unit's overtime this month; `owed` is `overtime_owed(p)`."""
    return total * p.required_hours(nurse) / owed if owed else 0.0


def overtime_priced(p: Problem, nurse: Nurse, overtime: int, total: int, owed: int) -> int:
    """Whole hours of her `overtime` priced: above her target by more than the tolerance, rounded down,
    at most her overtime; 0 for a nurse who owes no hours (no share) or works no overtime. With
    S = 100 R: floor((S (ot - tol) - 100 r O + R adj) / S), the model's linear form exactly."""
    r = p.required_hours(nurse)
    if r == 0 or overtime <= 0 or nurse.on_probation:
        return 0
    scale = 100 * owed
    x = scale * (overtime - p.department.overtime_tolerance) - 100 * r * total + owed * overtime_adj_100(p, nurse)
    return max(0, min(overtime, x // scale))


def overtime_balance_out(p: Problem, nurse: Nurse, overtime: int, total: int, owed: int) -> float:
    """Her `overtime_balance` for next month: in + overtime - share, rounded as the shift balances are.
    It moves while the unit's switch and the hospital's hours rule are on, whether or not the unit
    carries fairness and whatever the weight (the price is the hospital's; the record is the unit's),
    as the night balance moves; otherwise it is handed on unchanged. A nurse on probation's is 0 out, whatever came in."""
    if nurse.on_probation:
        return 0.0
    if not (p.department.share_overtime and p.general.hours_rule_enabled):
        return nurse.overtime_balance
    return round(nurse.overtime_balance + overtime - overtime_share(p, nurse, total, owed), 2)
