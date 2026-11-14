"""Whether a cell of a unit's month can hold a change: the one place it is judged, for a manager's edit, a swap, a cover and a
request alike, so that no two of them can give different accounts of one fact. A pin exists whether or not the month has a
roster, so the month is read from the nurses, their leave and the unit's rules, and needs none.

Each origin keeps its own rules, checked before it asks here, and every sentence it says to a nurse. Nothing here says anything
to anyone: a reason is a value, worded beside this module for a manager (`rosters.cell_words`) and by each origin for a nurse.

Asked of a grid, it also says what a change would newly break (`MonthCells.breaks`), with the solver's own code. Each origin
decides what a newly broken rule means to it: a swap and a cover are refused for one."""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass
from functools import cached_property
from typing import Literal

from hospital.models import Shift
from hospital.rules import required_skills
from nurses import membership, probation
from nurses.models import Leave, NurseProfile
from nurses.rostering import skills_block
from nurses.skills import current_skills
from rosters.checking import Checked, scored
from rosters.drafts import current_code
from rosters.models import PinnedCell, PinOrigin
from rosters.versions import Grid
from solves.input import assemble, barred_from
from solves.result import headcount_of, shift_day, skill_need_of
from units.models import Unit

# Why a cell could not hold what is asked whatever pin is on it, and so why a saved pin is set aside. The API's schema lists
# them in this order, which is not the order they are judged in (`MonthCells.cannot_hold`).
AsideWhy = Literal["not_in_month", "outside_dates", "leave", "shift_not_worked", "shift_not_allowed", "barred", "night_on_probation"]
# Those, and a pin on the cell that does not yield to the origin asking.
Why = Literal[AsideWhy, "held"]

# The origins that ask a cell to change, named as a version's origin is (`rosters.in_force.Origin`). A request asks when it
# is decided: accepted, or what it promises changed.
Asking = Literal["edit", "swap", "cover", "request"]
EDIT: Asking = "edit"
SWAP: Asking = "swap"
COVER: Asking = "cover"
REQUEST: Asking = "request"

# What a cell is asked to hold: a shift, by a code as someone wrote it or as the row a pin or a cover holds, or nothing for a
# day off.
Holds = Shift | str | None

# Which pin yields to which change: {the origin asking: the origins of the pins it replaces}. A pin of any other origin holds its
# cell against that change, set aside or not, since a pin set aside is not ended and may count again. Leave asks nothing here:
# it ends every pin on its days.
YIELDS: dict[Asking, set[PinOrigin]] = {
    EDIT: {PinOrigin.MANAGER, PinOrigin.SWAP, PinOrigin.COVER},
    SWAP: {PinOrigin.MANAGER, PinOrigin.SWAP, PinOrigin.COVER},
    # A cover is not expected to meet a cover's pin: a cover goes only to a nurse off its day on the roster in force, and a
    # cover's pin put her on a shift that day, which an edit or a swap takes her off only by replacing the pin, and leave only by
    # ending it. It is answered all the same, as a swap's pin is: a pin set aside reaches no solve, so a re-plan can still move
    # its cell to a day off, and a cover then puts her on a shift there as a swap would.
    COVER: {PinOrigin.MANAGER, PinOrigin.SWAP, PinOrigin.COVER},
    REQUEST: {PinOrigin.MANAGER, PinOrigin.REQUEST},
}


# Who may read a reason, or a violation a change newly breaks, in full. The unit's manager reads every one whatever its class,
# as they read the leave and the attributes it rests on; the class is what a nurse may read, since she learns nothing of a
# colleague that My roster would not show her. Each origin chooses from it between saying a reason in its reader's words and its
# one hidden sentence, and lists no reason of its own as private.
Reader = Literal["private", "shown", "hidden"]
# The nurse it is about, and her manager: her own facts, said to her in full and to a colleague only as the hidden sentence.
PRIVATE: Reader = "private"
# Anyone: what My roster shows every reader, said as it is.
SHOWN: Reader = "shown"
# No nurse: a violation resting on who holds which attribute on a shift-day, which no nurse's roster shows.
HIDDEN: Reader = "hidden"

# Each reason's reader class. Her dates, her leave (a day off on My roster), her bar, her allowed shifts and the pin on her cell
# (which choice of hers holds it is not on My roster) are hers. The unit's shifts, and who is on probation, are on My roster.
READERS: dict[Why, Reader] = {
    "not_in_month": PRIVATE,
    "outside_dates": PRIVATE,
    "leave": PRIVATE,
    "shift_not_worked": SHOWN,
    "barred": PRIVATE,
    "shift_not_allowed": PRIVATE,
    "night_on_probation": SHOWN,
    "held": PRIVATE,
}


def reader_of(violation: dict) -> Reader:
    """A violation's reader class: one on a nurse is hers, a headcount is what anyone can count on My roster, and any other on
    a shift-day (a team leader, both genders, a skill need, or a rule not known here) would say who holds which attribute."""
    if violation["nurse"]:
        return PRIVATE
    return SHOWN if violation["rule"] == "headcount" else HIDDEN


@dataclass(frozen=True)
class Reason:
    """Why a cell cannot hold what is asked, with the facts a sentence about it needs. `shift` is the code of a shift the
    unit does not work, as it was asked, or of the shift she may not work, as it is now; `skill` and `last` are a bar's skill and
    the last day it counted; `origin` is that of the pin that holds the cell."""
    why: Why
    shift: str | None = None
    skill: str | None = None
    last: dt.date | None = None
    origin: PinOrigin | None = None

    @property
    def reader(self) -> Reader:
        """Who may read it in full (`READERS`)."""
        return READERS[self.why]


@dataclass(frozen=True)
class Answer:
    """Whether a cell may hold a change: it may, with nothing set; it may and replaces the pin `replaces`; it may not, for
    `reason`."""
    reason: Reason | None = None
    replaces: PinnedCell | None = None


@dataclass(frozen=True)
class NewCell:
    """One cell a change asks for: a nurse's date and what it should hold, a shift's code or None for a day off."""
    hospital_id: str
    date: dt.date
    shift: str | None

    @property
    def day(self) -> str:
        return self.date.isoformat()


@dataclass(frozen=True)
class Broken:
    """What a change newly breaks on a grid: `before` is the grid's check and `after` the changed grid's, each with the
    solver's own code, `grid` the changed grid, and `violations` the hard rules it newly breaks. A grid or rules the solver's
    code refuses are an answer to show, not an error: that check's `reason` says why, and nothing is newly broken when the
    changed grid could not be checked."""
    before: Checked
    after: Checked
    grid: Grid
    violations: list[dict]


class MonthCells:
    """The cells of a unit's month, read once for every question asked of them. A nurse is named by her hospital ID, as a
    grid and every origin name her."""

    def __init__(self, unit: Unit, month: dt.date):
        self.unit, self.month = unit, month
        serving = [nurse for nurse in membership.rostered_in(unit, month).select_related("user")
                   .prefetch_related(current_skills()) if not membership.no_longer_serving(nurse)]
        # {hospital ID: nurse}: those with a day in the month, whom a solve of it would plan for.
        self.nurses = {nurse.user.username: nurse for nurse in serving}
        self._by_pk = {nurse.pk: nurse for nurse in serving}
        self._leave = set(Leave.objects.current().in_month(month).values_list("nurse_id", "date"))
        worked = Shift.objects.filter(active=True, code__in=unit.rules.get("shifts", []))
        self._worked = {shift.code: shift for shift in worked}
        required = required_skills()
        # {nurse's pk: the bar on her month}, read as her solve input reads her skills, so the two cannot disagree.
        self._bars = {nurse.pk: bar for nurse in serving
                      if (bar := barred_from(skills_block(nurse.current_skills, month), required, month))}
        # The hospital IDs of those on probation this month, who work no night shift: read as her solve input reads it.
        self.on_probation = {hospital_id for hospital_id, nurse in self.nurses.items()
                             if probation.on_probation_in(nurse, month)}
        self._night_shift = unit.rules.get("night_shift")
        # The unit's pins of the month by nurse and date, for a listing of them. One left behind by a nurse transferred out is
        # among them: it is this unit's still, and holds its cell here.
        self.pins = list(PinnedCell.objects.current().in_month(month).filter(unit=unit).select_related("shift", "nurse__user")
                         .order_by("nurse__user__username", "date"))
        # {(hospital ID, date): the pin on that cell}.
        self._pin_by_cell = {(pin.nurse.user.username, pin.date): pin for pin in self.pins}
        self._checked: tuple[Grid, Checked] | None = None

    def shift(self, code: str) -> Shift | None:
        """The shift this code names among those the unit works, an older code of it included; None for any other."""
        return self._worked.get(current_code(code))

    def cannot_hold(self, hospital_id: str, day: dt.date, holds: Holds) -> Reason | None:
        """Why this nurse's cell could not hold `holds` whatever pin is on it; None when it could. A day off is always hers on
        a day she holds anything, a barred day's too."""
        return self._cannot_hold(self.nurses.get(hospital_id), day, holds)

    def set_aside(self, pin: PinnedCell, *, over: bool = False) -> Reason | None:
        """Why this saved pin of the unit's month counts for nothing now: its cell could not hold it, by the judgement a new
        pin there gets with precedence left out. None for a pin that counts. `over` for a month that is over, which its close
        makes fact: the reasons about the day still set a pin aside, but one to a shift the unit has stopped working, or she may
        no longer work, was worked when it was set, and counts."""
        return self._cannot_hold(self._by_pk.get(pin.nurse_id), pin.date, None if over else pin.shift)

    def pin_on(self, hospital_id: str, day: dt.date) -> PinnedCell | None:
        """The pin on this nurse's cell, whatever its origin and whether or not it is set aside; None when there is none."""
        return self._pin_by_cell.get((hospital_id, day))

    def holding_already(self, hospital_id: str, day: dt.date, holds: Holds) -> PinnedCell | None:
        """The pin on this nurse's cell when it holds just `holds` already, a shift by any older code of it too, whatever its
        origin; None otherwise. A code is followed whether or not the unit still works it, since a pin can hold a shift the unit
        has stopped working."""
        pin = self.pin_on(hospital_id, day)
        asked = holds.code if isinstance(holds, Shift) else current_code(holds) if holds else ""
        return pin if pin is not None and pin.code == asked else None

    def may_change(self, hospital_id: str, day: dt.date, asking: Asking) -> Answer:
        """Whether the origin asking (an edit, a swap, a cover or a request, as `rosters.in_force.Origin` names them) may change
        this nurse's cell at all, whatever it would then hold: it may when no pin is on it, and when the pin on it yields to that
        origin, which the change then replaces. All a clear is asked, since ending a pin can break nothing."""
        pin = self.pin_on(hospital_id, day)
        if pin is None:
            return Answer()
        if pin.origin in YIELDS[asking]:
            return Answer(replaces=pin)
        return Answer(reason=Reason("held", origin=PinOrigin(pin.origin)))

    def managers_pin_replaced(self, hospital_id: str, day: dt.date, asking: Asking) -> str | None:
        """What the manager's own pin on this nurse's cell holds (a shift's code, or "" for a day off) when a change of the
        origin asking would replace it, for a page to say before the manager allows that change; None when the change would
        replace no pin of theirs. A swap's, a cover's and a request's pin are not the manager's to be told of here: each is
        changed where it was made."""
        replaced = self.may_change(hospital_id, day, asking).replaces
        return replaced.code if replaced is not None and replaced.origin == PinOrigin.MANAGER else None

    def may_hold(self, hospital_id: str, day: dt.date, asking: Asking, holds: Holds) -> Answer:
        """Whether this nurse's cell may hold `holds` for the origin asking. What the cell cannot hold is said before a pin
        that does not yield."""
        reason = self.cannot_hold(hospital_id, day, holds)
        return Answer(reason=reason) if reason else self.may_change(hospital_id, day, asking)

    def shown(self, hospital_id: str, day: dt.date, holds: Holds) -> Reason | None:
        """The first reason this nurse's cell could not hold `holds` that anyone may read (`SHOWN`), whatever private reason
        stands before it in the one order. An origin that says a private reason to a colleague only as its hidden sentence asks
        this first: were a shown reason answered only when nothing private stood before it, saying it, or not, would tell her
        which colleague had a private one."""
        return next((reason for reason in self._reasons(self.nurses.get(hospital_id), day, holds) if reason.reader == SHOWN),
                    None)

    def _cannot_hold(self, nurse: NurseProfile | None, day: dt.date, holds: Holds) -> Reason | None:
        return next(self._reasons(nurse, day, holds), None)

    def _reasons(self, nurse: NurseProfile | None, day: dt.date, holds: Holds):
        """Every reason the cell could not hold `holds`, in their one order: those about the day, which stop a day off too, then
        those about the shift. Read lazily, so asking for the first reads no further."""
        if nurse is None:
            yield Reason("not_in_month")
        elif membership.outside_dates(nurse, day):
            yield Reason("outside_dates")
        if nurse is not None and (nurse.pk, day) in self._leave:
            yield Reason("leave")
        if not holds:
            return
        # A row is the unit's when the unit works its code now; a code is followed from any older one first.
        asked, shift = (holds.code, self._worked.get(holds.code)) if isinstance(holds, Shift) else (holds, self.shift(holds))
        if shift is None:
            yield Reason("shift_not_worked", shift=asked)
            return
        if nurse is None:
            return
        bar = self._bars.get(nurse.pk)
        if bar is not None and day >= bar.first:
            yield Reason("barred", skill=bar.skill, last=bar.last)
        allowed = membership.placement_in(nurse, self.month).allowed_shifts
        if allowed is not None and shift.code not in allowed:
            yield Reason("shift_not_allowed", shift=shift.code)
        if nurse.user.username in self.on_probation and shift.code == self._night_shift:
            yield Reason("night_on_probation", shift=shift.code)

    @cached_property
    def solve_input(self) -> dict:
        """The month's solve input as it stands, which a grid is checked against. Read only once a grid is asked about, so asking
        of a cell alone runs nothing of the solver's."""
        return assemble(self.unit, self.month).input

    def checked(self, grid: Grid) -> Checked:
        """This grid's check with the solver's own code, on the month as it stands. The last grid's is kept: a cover asks what
        each of the month's nurses would newly break on the one roster in force."""
        if self._checked is None or self._checked[0] is not grid:
            self._checked = (grid, scored(self.solve_input, grid))
        return self._checked[1]

    def breaks(self, grid: Grid, new_cells: list[NewCell]) -> Broken:
        """What these new cells would newly break on `grid`, by `newly_broken`: a hard rule the grid already breaks is not the
        change's. Each new cell is put in as a pin, so the solver's `pin` rule holds it, and a pin there that it replaces does not
        count against it. Every new cell's nurse has a row on `grid`."""
        before = self.checked(grid)
        changed = copy.deepcopy(grid)
        solve_input = copy.deepcopy(self.solve_input)
        rows = {row["id"]: row for row in solve_input["nurses"]}
        for new_cell in new_cells:
            shift = self.shift(new_cell.shift) if new_cell.shift else None
            changed[new_cell.hospital_id][new_cell.day] = shift.code if shift else ""
            # A nurse the month's input does not plan for has no pin to hold; the loader leaves her out of the check.
            if new_cell.hospital_id in rows:
                rows[new_cell.hospital_id].setdefault("pinned", {})[new_cell.day] = shift.code if shift else ""
        after = scored(solve_input, changed)
        return Broken(before, after, changed, newly_broken(before.violations, after.violations) if after.answer else [])


def _beyond(violation: dict) -> int:
    """How far a headcount is outside its range."""
    present, low, high = headcount_of(violation)
    return low - present if present < low else present - high


def _short(violation: dict) -> tuple[tuple, int] | None:
    """A skill need's shift-day and skill, which name it (a shift-day can need several skills), and how many nurses holding
    the skill it is short; None for another rule, or a need that does not state its figures."""
    where, need = shift_day(violation), skill_need_of(violation)
    if violation["rule"] != "skill_need" or where is None or need is None:
        return None
    skill, short = need
    return (where, skill), short


def _as_broken(violation: dict) -> tuple:
    """A violation with how far it breaks its rule, which only its words say: one broken further reads differently."""
    where = shift_day(violation)
    return violation["rule"], violation["nurse"], violation["date"], where and where[1], violation["text"]


def newly_broken(before: list[dict], after: list[dict]) -> list[dict]:
    """The violations of `after` that `before` does not hold, or holds broken less: what a change to a grid breaks, between
    the grid's violations and the changed grid's. A hard rule the grid already breaks is not the change's, since an edit may
    save one. A headcount is compared by how far it is outside its range, and a skill need by how many nurses holding its skill
    it is short, as their words change when they come nearer: one more nurse on a short shift-day breaks it less, not anew. Any
    other rule, or a skill need that does not state its figures, is compared by its words."""
    already = {_as_broken(violation) for violation in before}
    headcounts = {shift_day(row): _beyond(row) for row in before if row["rule"] == "headcount" and shift_day(row)}
    needs = dict(short for row in before if (short := _short(row)))
    new = []
    for violation in after:
        where, short = shift_day(violation), _short(violation)
        if violation["rule"] == "headcount" and where in headcounts:
            if _beyond(violation) > headcounts[where]:
                new.append(violation)
        elif short is not None and short[0] in needs:
            if short[1] > needs[short[0]]:
                new.append(violation)
        elif _as_broken(violation) not in already:
            new.append(violation)
    return new
