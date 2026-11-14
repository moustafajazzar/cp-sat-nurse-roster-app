"""Whether a swap can work, and how that is said to each reader: the one place both are decided, so the proposer's words and the
manager's cannot drift apart.

The proposer may learn nothing about a colleague that My roster would not show them: a
colleague's leave reads as a day off there, and a nurse never reads a colleague's attributes (allowed shifts, Ramadan reduction,
breastfeeding, score, team leader, gender, hours). So every reason that rests on a colleague's facts, whatever it is, reaches the
proposer as the same sentence, "This swap cannot work for Badr Omar on 5 June 2026.": no rule, no text from the solver, no cost.
A reason about the proposer's own cells may be named, as may what any reader of My roster sees (a cell's shift, a date passed,
how many work a shift that day). The unit's manager reads every reason in full, as they read the leave and the attributes.

What is left is the one bit a refusal is: a nurse who proposes the same change day after day can learn on which days a
colleague cannot take a shift, without learning why. That comes with refusing at once."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from accounts.models import Role
from nurses import membership
from nurses.models import NurseProfile
from requesting.models import what_of
from rosters.approved import ApprovedMonth, NewCell, newly_broken, with_pins
from rosters.editing import Checked, scored
from rosters.models import PinOrigin, Roster
from rosters.pins import long_date
from rosters.versions import Grid
from solves.input import assemble
from swaps.models import WAITING, Swap, SwapCell

# A shift-day rule with no nurse that says nothing of anyone: how many work it, which any reader of the roster can count.
SAID_AS_IT_IS = {"headcount"}


@dataclass(frozen=True)
class Problem:
    """One reason the swap cannot work. `index` is the change it is about, when it is about one."""
    manager: str
    proposer: str
    index: int | None = None

    @classmethod
    def said(cls, words: str, index: int | None = None) -> "Problem":
        """A reason everyone may read in the same words: what My roster shows."""
        return cls(words, words, index)


@dataclass
class Judged:
    problems: list[Problem] = field(default_factory=list)
    before: Checked | None = None
    after: Checked | None = None
    grid: Grid | None = None

    def for_proposer(self) -> list[tuple[int | None, str]]:
        """Each sentence once, in order, with the change it is about."""
        seen, said = set(), []
        for problem in self.problems:
            if (problem.index, problem.proposer) not in seen:
                seen.add((problem.index, problem.proposer))
                said.append((problem.index, problem.proposer))
        return said

    @property
    def cost_before(self) -> int | None:
        return self.before.cost if self.before else None

    @property
    def cost_after(self) -> int | None:
        return self.after.cost if self.after else None


def name_of(nurse: NurseProfile) -> str:
    return nurse.user.full_name or nurse.user.username


def on_probation_said(month: dt.date) -> str:
    """Why a nurse on probation proposes no swap, in her own words."""
    return (f"You are on probation in {month:%B %Y}, and a nurse on probation takes no part in a swap: you work beside your "
            "preceptor, and the roster is planned around that.")


def cannot_work_for(name: str, day: dt.date | None) -> str:
    """The one sentence a proposer reads for any reason that rests on a colleague."""
    return f"This swap cannot work for {name} on {long_date(day)}." if day else f"This swap cannot work for {name}."


class SwapMonth(ApprovedMonth):
    """An approved roster's month as a swap reads it, read once for all of a swap's cells. `exclude` is the swap being judged
    again, whose own cells are not another swap's."""

    def __init__(self, roster: Roster, exclude: Swap | None = None):
        super().__init__(roster)
        waiting = SwapCell.objects.filter(swap__roster=roster, swap__state__in=WAITING)
        if exclude is not None:
            waiting = waiting.exclude(swap=exclude)
        self.in_other_swaps = set(waiting.values_list("nurse_id", "date"))


def _cell_problem(month: SwapMonth, new_cell: NewCell, index: int, proposer: NurseProfile | None) -> Problem | None:
    """Why this one cell cannot be in the swap. `proposer` is None when the manager judges it again; the proposer's words are
    worked out either way. What My roster shows is checked first and said as it is (a date passed, a shift the unit does not
    work, a cell already holding its change), so that a reason resting on a colleague's facts, checked after and said only as
    "cannot work for", is never told apart from those: a colleague's leave reads "already a day off" when asked for a day off,
    exactly as a day off does."""
    nurse = month.on_grid.get(new_cell.hospital_id)
    if new_cell.hospital_id not in month.roster.grid or nurse is None:
        return Problem.said(f"There is no {new_cell.hospital_id} on this roster.", index)
    mine = proposer is not None and nurse.pk == proposer.pk
    name, on = name_of(nurse), long_date(new_cell.date)
    if new_cell.date < membership.today():
        return Problem.said(f"{on} has passed, so its cells can no longer be swapped.", index)
    if nurse.user.role != Role.STAFF_NURSE:
        return Problem.said(f"{name} is not a staff nurse, so a swap cannot change their cells.", index)
    serving = month.serving.get(new_cell.hospital_id)
    if serving is not None and serving.pk in month.judge.on_probation:
        # Her row sits under "On probation" on My roster, so saying so tells no one more than the roster does.
        return Problem.said(on_probation_said(month.month) if mine else
                            f"{name} is on probation in {month.month:%B %Y}, so a swap cannot change their cells.", index)
    shift = month.shift_of(new_cell.shift)
    if new_cell.shift and shift is None:
        return Problem.said(f"{new_cell.shift} is not one of this unit's shifts.", index)
    if (shift.code if shift else "") == month.cell(new_cell.hospital_id, new_cell.date):
        whose = "Your" if mine else f"{name}'s"
        what = what_of(shift)
        return Problem(f"{name}'s cell on {on} is already {what}.", f"{whose} cell on {on} is already {what}.", index)
    hidden = cannot_work_for(name, new_cell.date)
    why = month.judge.day_why(serving.pk, new_cell.date) if serving else "not_in_month"
    if why is not None:
        manager = (month.judge.refusal(nurse.pk, name, new_cell.date, None, None) if serving
                   else f"{name} is not on this unit's roster for {month.month:%B %Y}.")
        own = {"not_in_month": f"You have no days on this roster in {month.month:%B %Y}.",
               "outside_dates": f"{on} is outside your dates on the roster.",
               "leave": f"You are on leave on {on}, so there is nothing to swap that day."}[why]
        return Problem(manager, own if mine else hidden, index)
    match month.judge.shift_why(serving.pk, new_cell.date, shift):
        case "barred":
            # A colleague's skills are hers, hidden as her leave is; the proposer's own are said in full.
            manager = month.judge.refusal(nurse.pk, name, new_cell.date, shift.code, shift)
            own = f"{month.judge.bar_said(nurse.pk, new_cell.date, 'Your')}, so you can work no shift on {on}."
            return Problem(manager, own if mine else hidden, index)
        case "shift_not_allowed":
            manager = month.judge.refusal(nurse.pk, name, new_cell.date, shift.code, shift)
            return Problem(manager, f"{shift.code} is not one of the shifts you work." if mine else hidden, index)
    pin = month.pins.get((nurse.pk, new_cell.date))
    if pin is not None and pin.origin == PinOrigin.REQUEST:
        # A colleague's request is hers and her manager's to read, so it is hidden like her leave.
        own = f"Your accepted request holds your cell on {on}, so a swap cannot change it."
        return Problem(f"An accepted request holds {name}'s cell on {on}, so change it there.", own if mine else hidden, index)
    if (nurse.pk, new_cell.date) in month.in_other_swaps:
        # Which swaps a colleague is in is not on My roster either.
        own = f"Your cell on {on} is already in another swap."
        return Problem(f"{name}'s cell on {on} is already in another swap.", own if mine else hidden, index)
    return None


def _violation_problem(violation: dict, month: SwapMonth, new_cells: list[NewCell], proposer: NurseProfile | None,
                       preceptors: dict[str, str]) -> Problem:
    text = violation["text"]
    day = dt.date.fromisoformat(violation["date"]) if violation["date"] else None
    nurse = month.on_grid.get(violation["nurse"]) if violation["nurse"] else None
    preceptor = month.on_grid.get(preceptors.get(violation["nurse"], ""))
    if violation["rule"] == "following" and nurse is not None and preceptor is not None:
        # What My roster shows: her row under "On probation" with her preceptor's name, and both rows' cells.
        return Problem(text, f"This swap cannot work on {long_date(day)}: {name_of(nurse)} is on probation and works beside "
                             f"her preceptor, {name_of(preceptor)}, on their day shifts.")
    if nurse is not None:
        if proposer is not None and nurse.pk == proposer.pk:
            return Problem(text, f"This swap breaks a rule for you: {text}")
        # A rule of the whole month, such as hours, is said on the colleague's first cell, as any other of theirs.
        day = day or min((new_cell.date for new_cell in new_cells if new_cell.hospital_id == violation["nurse"]), default=None)
        return Problem(text, cannot_work_for(name_of(nurse), day))
    if violation["rule"] in SAID_AS_IT_IS:
        return Problem(text, f"This swap breaks the unit's rules: {text}")
    # A team leader or both genders missing from a shift-day would say who holds which attribute.
    hidden = (f"This swap cannot work on {long_date(day)}: that day's shifts would break the unit's rules." if day
              else "This swap would break the unit's rules.")
    return Problem(text, hidden)


def judge(roster: Roster, new_cells: list[NewCell], proposer: NurseProfile | None, exclude: Swap | None = None) -> Judged:
    """Every reason the swap cannot work: each cell's, and when every cell can hold its change, what the grid would newly break
    on the month as it stands, with the solver's own code. A hard rule the roster in force already breaks is not the swap's,
    since an edit may save one, and a headcount already broken counts only when broken further."""
    month = SwapMonth(roster, exclude)
    judged = Judged()
    for index, new_cell in enumerate(new_cells):
        problem = _cell_problem(month, new_cell, index, proposer)
        if problem:
            judged.problems.append(problem)
    if judged.problems:
        return judged
    solve_input = assemble(month.unit, month.month).input
    judged.grid = month.grid_after(new_cells)
    judged.before = scored(solve_input, roster.grid)
    judged.after = scored(with_pins(solve_input, month, new_cells), judged.grid)
    if judged.after.reason:
        judged.problems.append(Problem(f"The swap cannot be checked: {judged.after.reason}",
                                       "This swap cannot be checked against the roster now. Ask your manager."))
        return judged
    preceptors = {row["id"]: row["preceptor"] for row in solve_input["nurses"] if row.get("preceptor")}
    for violation in newly_broken(judged.before.violations, judged.after.violations):
        judged.problems.append(_violation_problem(violation, month, new_cells, proposer, preceptors))
    return judged
