"""Who could take a cover, and what its shift-day still lacks: the one place a cover is judged and worded.

A cover adds one nurse to a shift-day of the roster in force. The nurses who could be that one are the month's staff
nurses off that day (on the grid, in the month, within their dates, not on leave, allowed the shift), not held off by an
accepted request, and whose taking it newly breaks no hard rule, judged as a swap is: the grid with her cell set to the shift,
put in as a pin, checked with the solver's own code in this process against the month as it stands. A hard rule the roster in
force already breaks is not hers, so a shift-day already short does not empty its own list: a headcount broken less, as one
more nurse on a short shift-day breaks it, is not broken anew. A manager's pin or a swap's to a day off is noted as replaced;
a cell in a waiting swap is left in, since any version expires that swap.

The list is ranked by how far below her owed hours this month each stands, then by least overtime this year, then by name, with
the figures from the solver's own per-nurse accounting on the roster in force, the arithmetic My month uses. Nothing of it is
stored: it is read again each time it is asked for.

Only the unit's manager reads the list, as they read the leave and the attributes it rests on. An acceptance is judged again
for its nurse alone (`CoverMonth.refusal_for`), the same way, and refused in her own words: what is hers is said in full (her
leave, her dates, her shifts, her request, a rule broken on her own row) and so is what My roster shows (a cell's shift, the
shift-day's headcount); a rule resting on a colleague, a team leader or both genders on the shift-day, is said only as the
unit's rules on that day, as a swap's proposer reads it (`swaps.judging`)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from nurse_roster.request import load_request

from accounts.models import Role
from hospital.models import Shift
from rosters.approved import ApprovedMonth, NewCell, newly_broken, with_pins
from rosters.editing import Checked, accounted, scored
from rosters.models import PinOrigin, Roster
from rosters.pins import long_date
from solves.input import assemble
from solves.result import headcount_of, shift_day

LACKS = {"team_leader": "No team leader", "gender": "Not both genders"}


@dataclass(frozen=True)
class CouldTake:
    hospital_id: str
    name: str
    worked: int
    owed: int | None  # None with the hospital's hours rule off, which counts nothing owed.
    overtime_this_year: int
    makes_whole: bool
    replaces: str | None  # The origin of her pin to a day off there, which her taking it replaces.

    @property
    def balance(self) -> int:
        """Hours worked less hours owed this month: furthest below first. Nothing is owed with the hours rule off."""
        return self.worked - self.owed if self.owed is not None else 0


@dataclass
class CoverJudged:
    reason: str | None = None
    lack: list[str] = field(default_factory=list)
    full: str | None = None
    nurses: list[CouldTake] = field(default_factory=list)


def _on(violations: list[dict], day: dt.date, code: str) -> list[dict]:
    """What a cover's shift-day lacks: its headcount, and what `LACKS` words."""
    return [row for row in violations if (row["rule"] == "headcount" or row["rule"] in LACKS)
            and shift_day(row) == (day.isoformat(), code)]


def _lack_words(violation: dict) -> str:
    if violation["rule"] == "headcount":
        present, low, high = headcount_of(violation)
        said = (f"Below its minimum headcount: {present} of at least {low}" if present < low
                else f"Above its maximum headcount: {present} of at most {high}")
    else:
        said = LACKS[violation["rule"]]
    return f"{said} (the solver's check: {violation['text']})."


class CoverMonth(ApprovedMonth):
    """The roster in force's month as a cover reads it, read once for every nurse judged."""

    def __init__(self, roster: Roster, day: dt.date, shift: Shift):
        super().__init__(roster)
        self.day, self.shift = day, shift
        self.input = assemble(self.unit, self.month).input
        self.before = scored(self.input, roster.grid)

    def exclusion(self, hospital_id: str) -> str | None:
        """Why this nurse could not take the cover before her grid is checked, as a key worded for her: `not_on_grid`,
        `not_staff`, `works`, `not_in_month`, `outside_dates`, `leave`, `on_probation`, `barred`, `shift_not_allowed` or
        `request`."""
        nurse = self.on_grid.get(hospital_id)
        if hospital_id not in self.roster.grid or nurse is None:
            return "not_on_grid"
        if nurse.user.role != Role.STAFF_NURSE:
            return "not_staff"
        if self.cell(hospital_id, self.day):
            return "works"
        serving = self.serving.get(hospital_id)
        why = self.judge.day_why(serving.pk, self.day) if serving else "not_in_month"
        if why is not None:
            return why
        if serving.pk in self.judge.on_probation:
            return "on_probation"  # She fills no gap: a shift is fully staffed without her.
        why = self.judge.shift_why(serving.pk, self.day, self.shift)
        if why is not None:
            # The cover's own shift is one the unit works, so anything but the bar is her allowed shifts.
            return "barred" if why == "barred" else "shift_not_allowed"
        pin = self.pins.get((nurse.pk, self.day))
        if pin is not None and pin.origin == PinOrigin.REQUEST:
            return "request"
        return None

    def refusal_for(self, hospital_id: str) -> list[str]:
        """Why she cannot take it now, in her own words: nothing when she can."""
        on = long_date(self.day)
        why = self.exclusion(hospital_id)
        if why == "works":
            held = self.shifts.get(self.cell(hospital_id, self.day))
            what = f"{held.display_name} ({held.code})" if held else self.cell(hospital_id, self.day)
            return [f"You already work {what} on {on}, so you cannot take this cover."]
        if why == "barred":
            return [f"{self.judge.bar_said(self.serving[hospital_id].pk, self.day, 'Your')}, so you cannot take this cover."]
        if why is not None:
            return [{
                "not_on_grid": f"You have no row on the {self.unit.name} roster for {self.month:%B %Y}, so you cannot take "
                               "this cover.",
                "not_staff": "Only a staff nurse takes a cover.",
                "not_in_month": f"You have no days on the {self.unit.name} roster in {self.month:%B %Y}, so you cannot take "
                                "this cover.",
                "outside_dates": f"{on} is outside your dates on the roster, so you cannot take this cover.",
                "leave": f"You are on leave on {on}, so you cannot take this cover.",
                "on_probation": f"You are on probation in {self.month:%B %Y}, and a nurse on probation takes no cover: a shift "
                                "is fully staffed without her.",
                "shift_not_allowed": f"{self.shift.code} is not one of the shifts you work, so you cannot take this cover.",
                "request": f"Your accepted request holds your cell on {on}, so you cannot take this cover.",
            }[why]]
        if self.before.reason:
            return ["This cover cannot be checked against the roster now. Ask your manager."]
        after, broken = self.with_her(hospital_id)
        if after.reason:
            return ["This cover cannot be checked against the roster now. Ask your manager."]
        said = []
        for violation in broken:
            text = violation["text"]
            if violation["nurse"] == hospital_id:
                said.append(f"This breaks a rule for you: {text}")
            elif violation["rule"] == "headcount":
                said.append(f"Taking this cover would break the unit's rules: {text}")
            else:
                # A colleague's rule, or a team leader or both genders on a shift-day, would say who holds which attribute.
                said.append(f"You cannot take this cover: the shifts on {on} would break the unit's rules.")
        return list(dict.fromkeys(said))

    def with_her(self, hospital_id: str) -> tuple[Checked, list[dict]]:
        """Her grid's check, and the hard rules it newly breaks."""
        new_cells = [NewCell(hospital_id, self.day, self.shift.code)]
        after = scored(with_pins(self.input, self, new_cells), self.grid_after(new_cells))
        return after, (newly_broken(self.before.violations, after.violations) if after.answer else [])

    def full(self) -> str | None:
        """Why one more nurse cannot help, when the shift-day is already at its maximum headcount."""
        problem = load_request(self.input).problem
        if not problem.department.headcount_enabled:
            return None
        index = (self.day - self.month).days
        # The solver's own reading: a nurse on probation counts toward no headcount (`problem.counted`).
        uncounted = {row["id"] for row in self.input["nurses"] if row.get("on_probation")}
        for headcount in problem.department.headcounts:
            if headcount.shift != self.shift.code:
                continue
            high = headcount.weekend_max if problem.is_weekend(index) else headcount.weekday_max
            counted = {self.shift.code, *headcount.also_counts}
            present = sum(1 for hospital_id, row in self.roster.grid.items()
                          if hospital_id not in uncounted and row.get(self.day.isoformat()) in counted)
            if present >= high:
                return (f"{self.shift.display_name} on {long_date(self.day)} already has {present} of at most {high}, so one "
                        f"more nurse would break its headcount. To change who works it, edit the roster.")
        return None


def judge(roster: Roster, day: dt.date, shift: Shift) -> CoverJudged:
    """The shift-day's lack and the ranked list of who could take it, on the month as it stands. A grid or rules the loader
    refuses are an answer to show, not an error."""
    month = CoverMonth(roster, day, shift)
    if month.before.reason:
        return CoverJudged(reason=f"The roster in force cannot be checked: {month.before.reason}")
    judged = CoverJudged(lack=[_lack_words(row) for row in _on(month.before.violations, day, shift.code)])
    judged.full = month.full()
    if judged.full:
        return judged
    figures = accounted(month.input, roster.grid) or {}
    counted = month.input["general"].get("hours_rule_enabled", True)
    for hospital_id in sorted(roster.grid):
        if month.exclusion(hospital_id) is not None:
            continue
        after, broken = month.with_her(hospital_id)
        if after.reason:
            judged.reason = f"A nurse's grid cannot be checked: {after.reason}"
            judged.nurses = []
            return judged
        if broken or hospital_id not in figures:
            continue
        nurse, row = month.on_grid[hospital_id], figures[hospital_id]
        pin = month.pins.get((nurse.pk, day))
        judged.nurses.append(CouldTake(
            hospital_id=hospital_id, name=nurse.user.full_name or hospital_id, worked=row["hours_worked"],
            owed=row["required"] if counted else None, overtime_this_year=row["carry_over"]["ytd_overtime"],
            makes_whole=not _on(after.violations, day, shift.code),
            replaces=pin.origin if pin is not None else None))
    judged.nurses.sort(key=lambda could: (could.balance, could.overtime_this_year, could.name.casefold(), could.hospital_id))
    return judged
