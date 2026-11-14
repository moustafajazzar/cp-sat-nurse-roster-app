"""Whether a swap can work, and how that is said to each reader: the one place both are decided, so the proposer's words and the
manager's cannot drift apart.

The proposer may learn nothing about a colleague that My roster would not show them. Which reasons, and which violations a swap
newly breaks, are private to their nurse, shown to anyone or hidden from every nurse is the cell judgement's to say
(`rosters.cells.READERS`, `reader_of`); a swap only chooses between the proposer's words and its hidden sentences. So every reason
that rests on a colleague's facts, whatever it is, reaches the proposer as the same sentence, "This swap cannot work for Badr Omar
on 5 June 2026.": no rule, no text from the solver, no cost. What My roster shows is checked first, so that a hidden reason
cannot be told apart from a shown one. The unit's manager reads every reason in full, as they read the leave and the attributes.

What is left is the one bit a refusal is: a nurse who proposes the same change day after day can learn on which days a
colleague cannot take a shift, without learning why. That comes with refusing at once."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from accounts.models import Role
from nurses import membership
from nurses.models import NurseProfile
from requesting.models import what_of
from rosters.cell_words import bar_said, manager_said
from rosters.cells import PRIVATE, SHOWN, SWAP, MonthCells, NewCell, Reason, reader_of
from rosters.checking import Checked
from rosters.models import Roster
from rosters.pins import long_date
from rosters.versions import Grid
from swaps.models import WAITING, Swap, SwapCell


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


class SwapMonth:
    """The roster in force's month as a swap reads it, read once for all of a swap's cells. Whether each cell can hold its
    change, and what the changed grid newly breaks, are asked of the month's cells (`rosters.cells`). `exclude` is the swap
    being judged again, whose own cells are not another swap's."""

    def __init__(self, roster: Roster, exclude: Swap | None = None):
        self.roster, self.month = roster, roster.month
        self.cells = MonthCells(roster.unit, roster.month)
        # Everyone with a row on the grid, one no longer in the month included, whom the cells do not plan for.
        on_grid = NurseProfile.objects.filter(user__username__in=list(roster.grid)).select_related("user")
        self.on_grid = {nurse.user.username: nurse for nurse in on_grid}
        waiting = SwapCell.objects.filter(swap__roster=roster, swap__state__in=WAITING)
        if exclude is not None:
            waiting = waiting.exclude(swap=exclude)
        self.in_other_swaps = set(waiting.values_list("nurse_id", "date"))

    def cell(self, hospital_id: str, day: dt.date) -> str:
        return self.roster.grid.get(hospital_id, {}).get(day.isoformat(), "")


def _in_her_words(reason: Reason, on: str, month: dt.date) -> str:
    """Why the proposer's own cell cannot hold its change, said to her in full: a private reason, since a shown one is said to
    everyone as the manager reads it."""
    match reason.why:
        case "not_in_month":
            return f"You have no days on this roster in {month:%B %Y}."
        case "outside_dates":
            return f"{on} is outside your dates on the roster."
        case "leave":
            return f"You are on leave on {on}, so there is nothing to swap that day."
        case "barred":
            return f"{bar_said(reason, 'Your')}, so you can work no shift on {on}."
        case "shift_not_allowed":
            return f"{reason.shift} is not one of the shifts you work."
        case "held":
            return f"Your accepted {reason.origin} holds your cell on {on}, so a swap cannot change it."
    raise ValueError(f"A private reason with no words for its nurse: {reason.why}.")


def _reason_problem(reason: Reason, month: SwapMonth, new_cell: NewCell, index: int, name: str, mine: bool) -> Problem:
    """A reason the cell judgement gives, said by its reader class: a shown one as the manager reads it, a private one in full
    to its own nurse and as the one hidden sentence to a colleague."""
    manager = manager_said(reason, name, new_cell.date, month.month, SWAP)
    if reason.reader == SHOWN:
        return Problem.said(manager, index)
    on = long_date(new_cell.date)
    return Problem(manager, _in_her_words(reason, on, month.month) if mine else cannot_work_for(name, new_cell.date), index)


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
    if new_cell.hospital_id in month.cells.on_probation:
        # Her row sits under "On probation" on My roster, so saying so tells no one more than the roster does.
        return Problem.said(on_probation_said(month.month) if mine else
                            f"{name} is on probation in {month.month:%B %Y}, so a swap cannot change their cells.", index)
    # What anyone may read of why the cell cannot hold its change is asked ahead of the cell's other reasons, whatever private
    # reason stands before it.
    shown = month.cells.shown(new_cell.hospital_id, new_cell.date, new_cell.shift)
    if shown is not None:
        return _reason_problem(shown, month, new_cell, index, name, mine)
    shift = month.cells.shift(new_cell.shift) if new_cell.shift else None
    if (shift.code if shift else "") == month.cell(new_cell.hospital_id, new_cell.date):
        whose = "Your" if mine else f"{name}'s"
        what = what_of(shift)
        return Problem(f"{name}'s cell on {on} is already {what}.", f"{whose} cell on {on} is already {what}.", index)
    reason = month.cells.may_hold(new_cell.hospital_id, new_cell.date, SWAP, new_cell.shift).reason
    if reason is not None:
        return _reason_problem(reason, month, new_cell, index, name, mine)
    if (nurse.pk, new_cell.date) in month.in_other_swaps:
        # Which swaps a colleague is in is not on My roster either.
        own, hidden = f"Your cell on {on} is already in another swap.", cannot_work_for(name, new_cell.date)
        return Problem(f"{name}'s cell on {on} is already in another swap.", own if mine else hidden, index)
    return None


def _violation_problem(violation: dict, month: SwapMonth, new_cells: list[NewCell], proposer: NurseProfile | None,
                       preceptors: dict[str, str]) -> Problem:
    text = violation["text"]
    day = dt.date.fromisoformat(violation["date"]) if violation["date"] else None
    nurse = month.on_grid.get(violation["nurse"]) if violation["nurse"] else None
    preceptor = month.on_grid.get(preceptors.get(violation["nurse"], ""))
    if violation["rule"] == "following" and nurse is not None and preceptor is not None:
        # Hers, and so a colleague's to the proposer, who is never on probation; but its hidden sentence may name what My roster
        # shows: her row under "On probation" with her preceptor's name, and both rows' cells.
        return Problem(text, f"This swap cannot work on {long_date(day)}: {name_of(nurse)} is on probation and works beside "
                             f"her preceptor, {name_of(preceptor)}, on their day shifts.")
    reader = reader_of(violation)
    if reader == PRIVATE and proposer is not None and violation["nurse"] == proposer.user.username:
        return Problem(text, f"This swap breaks a rule for you: {text}")
    if reader == SHOWN:
        return Problem(text, f"This swap breaks the unit's rules: {text}")
    if nurse is not None:
        # A rule of the whole month, such as hours, is said on the colleague's first cell, as any other of theirs.
        day = day or min((new_cell.date for new_cell in new_cells if new_cell.hospital_id == violation["nurse"]), default=None)
        return Problem(text, cannot_work_for(name_of(nurse), day))
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
    broken = month.cells.breaks(roster.grid, new_cells)
    judged.grid, judged.before, judged.after = broken.grid, broken.before, broken.after
    if judged.after.reason:
        judged.problems.append(Problem(f"The swap cannot be checked: {judged.after.reason}",
                                       "This swap cannot be checked against the roster now. Ask your manager."))
        return judged
    preceptors = {row["id"]: row["preceptor"] for row in month.cells.solve_input["nurses"] if row.get("preceptor")}
    for violation in broken.violations:
        judged.problems.append(_violation_problem(violation, month, new_cells, proposer, preceptors))
    return judged
