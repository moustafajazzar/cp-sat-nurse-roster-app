"""A nurse as a solve input carries them."""
from __future__ import annotations

import datetime as dt

from nurses import membership
from nurses.models import Leave, NurseProfile, Placement
from nurses.skills import counted_expiry, tracked


def nurse_block(nurse: NurseProfile, placement: Placement) -> dict:
    """One entry of a solve input's `nurses`, in the placement's unit, with the opening values and no history. `solves.input`
    starts from it, adding leave and pins, and replaces those values and the history with her carry-over (`closes.chain`)."""
    return {"id": nurse.user.username, "gender": nurse.gender, "team_leader": nurse.team_leader, "score": nurse.score,
            "ramadan_reduced": nurse.ramadan_reduced, "breastfeeding": nurse.breastfeeding,
            "ytd_overtime": nurse.ytd_overtime, "hours_balance": nurse.hours_balance,
            "night_balance": nurse.night_balance, "friday_balance": nurse.friday_balance,
            "overtime_balance": nurse.overtime_balance,
            "last_shift_kind": None, "rest_owed": 0, "allowed_shifts": placement.allowed_shifts}


def leave_dates(nurse: NurseProfile, month: dt.date) -> list[str]:
    """A nurse's `leave` in a month's solve input: ISO dates in order, each once, and no type, because a solve input is
    kept and read by people who may not see one. The solve input also lists the days outside the start and end dates, which are
    credited alike, as a set: a date recorded here can be one of those, and the solver refuses a day listed twice."""
    return leave_of([nurse], month).get(nurse.pk, [])


def leave_of(nurses: list[NurseProfile], month: dt.date) -> dict[int, list[str]]:
    """{nurse id: her `leave_dates`}, for those of `nurses` with leave in the month, in one query."""
    found: dict[int, list[str]] = {}
    for nurse_id, day in Leave.objects.current().in_month(month).filter(nurse__in=nurses).values_list("nurse_id", "date"):
        found.setdefault(nurse_id, []).append(day.isoformat())
    return found


def skills_block(rows, month: dt.date) -> dict[str, str | None]:
    """A nurse's `skills` in a month's solve input, from her current `NurseSkill` rows with their skills: each live
    skill by name, with its expiry when that falls in the month or before it (so a required one bars her, and absence keeps
    meaning no record), and null when it is valid past the month or its kind does not expire, as the solver asks. A retired
    skill is tracked no more, so it is no record: it counts toward no need and bars nobody, as `hospital.rules.required_skills`
    leaves it out."""
    last = membership.last_day_of(month)
    expiries = {row.skill.name: counted_expiry(row) for row in tracked(rows)}
    return {name: expiry.isoformat() if expiry and expiry <= last else None for name, expiry in sorted(expiries.items())}
