"""What a solve and the shift catalogue do to rosters."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Iterable

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from hospital.models import Shift, ShiftRename
from nurses import membership
from rosters.models import Roster, RosterState
from rosters.pins import month_pins, on_top
from solves.models import ACTIVE_STATES, Solve
from solves.result import has_a_roster_to_keep, shifts_used

if TYPE_CHECKING:
    from units.models import Unit


MONTH_OVER = "This month is over, so its draft cannot change."
FROZEN = {
    RosterState.SUBMITTED: "This roster is submitted and waits for the CNO or the Deputy CNO, so nothing can change it until "
                           "they decide. A rejection returns it to draft.",
    RosterState.APPROVED: "This roster is approved and in force, so no solve or restore can change it. Re-plan it from a "
                          "date, or change its cells by hand on the Roster page: either is a new version.",
}


def why_frozen(roster: Roster | None) -> str | None:
    """The one place a submitted or an approved roster refuses a solve's start and end and a restore. A submitted one refuses a
    save of pins too (`rosters.api._why_not_edit`); an approved one is edited by hand, each save a new version
    (`rosters.versions`), and a re-plan passes this on purpose (`solves.replans`). A rename is not a change: the grid follows it whatever the state. Hold the
    unit's lock, which a submit and a decision take, when this decides a write."""
    return FROZEN.get(roster.state) if roster is not None else None


def frozen_month(unit: "Unit", month: dt.date) -> str | None:
    return why_frozen(Roster.objects.filter(unit=unit, month=month).first())


def current_code(code: str) -> str:
    """No other shift can ever take a code a shift held, so an old code means one shift for good."""
    rename = ShiftRename.objects.select_related("shift").filter(old_code=code).first()
    return rename.shift.code if rename else code


def current_codes(codes: Iterable[str]) -> dict[str, str]:
    return {code: current_code(code) for code in set(codes)}


def in_current_codes(grid: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    codes = current_codes(code for row in grid.values() for code in row.values() if code)
    return {nurse: {day: codes.get(code, code) for day, code in row.items()} for nurse, row in grid.items()}


def as_a_draft_would_hold(solve: Solve, grid: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """A grid with today's codes, and with the pins that count on top, so a draft never disagrees with one. A solve given
    them holds them already; one started before a pin was saved, and an older result being restored, do not."""
    return on_top(in_current_codes(grid), month_pins(solve.unit, solve.month))


def replace_grid(solve: Solve, grid: dict[str, dict[str, str]], *, restoring: bool = False) -> Roster:
    """The draft of the solve's month, created or with its grid replaced. A shift renamed while the solve ran is carried
    here, so hold the catalogue's lock (`HospitalSettings.load(for_update=True)`) so that a rename cannot slip past. A caller
    has asked `why_frozen` first: this refuses all the same, rather than overwrite a frozen grid."""
    if frozen_month(solve.unit, solve.month):
        raise ValueError(f"The roster of {solve.audit_target} is frozen.")
    roster, _ = Roster.objects.update_or_create(unit=solve.unit, month=solve.month, defaults={
        "grid": as_a_draft_would_hold(solve, grid), "solve": solve, "restored_at": timezone.now() if restoring else None})
    return roster


def why_not_restore(solve: Solve) -> str | None:
    """Why this solve's result cannot become its month's draft now, or None. Hold the catalogue's lock and the unit's when it
    decides a restore. Its cost and each nurse's hours were worked out on the shifts as they stood then, so a shift whose
    hours moved or that has since been retired stops it: no draft could come to hold those any other way
    (`hospital.catalogue_api`). A rename does not: it is the same shift."""
    if solve.state in ACTIVE_STATES:
        return "This solve has not ended yet."
    if not has_a_roster_to_keep(solve.result):
        return "This solve left no roster to restore."
    if solve.month < membership.this_month():
        return MONTH_OVER
    frozen = frozen_month(solve.unit, solve.month)
    if frozen:
        return frozen
    if Solve.objects.filter(unit=solve.unit, month=solve.month, state__in=ACTIVE_STATES).exists():
        return "A solve of this month is queued or running, and would replace this when it ends. Stop it or wait for it first."
    draft = Roster.objects.filter(unit=solve.unit, month=solve.month, solve=solve).first()
    if draft and draft.grid == in_current_codes(solve.result["roster"]):
        return "This result is the draft already."
    if draft and draft.grid == as_a_draft_would_hold(solve, solve.result["roster"]):
        # Hand edits are pins, and a restore keeps pins, so restoring would leave this draft as it is.
        return "This result is the draft already, with your pins on top. Clear a pin to give its cell back."
    return _shift_changed_since(solve)


def _shift_changed_since(solve: Solve) -> str | None:
    hours_then = solve.input["general"]["shift_hours"]
    for code in sorted(shifts_used(solve.result)):
        shift = Shift.objects.filter(code=current_code(code)).first()
        if shift is None or not shift.active:
            return f"{code} has been retired since this solve, so this roster can no longer be worked. Solve the month again."
        if shift.hours != hours_then[code]:
            return (f"{shift.code} was {hours_then[code]} hours at this solve and is {shift.hours} now, so this roster's cost "
                    "and hours no longer hold. Solve the month again.")
    return None


def carry_rename_through_grids(old_code: str, new_code: str) -> None:
    """Call it under the catalogue's lock. A stored input and result stay what they were, as does a roster version. A submitted or
    approved grid follows too: a rename is the same shift, so nothing the approver decides on changes."""
    # By month, then unit: the order leave over two approved months locks them in (`rosters.versions.take_off_for_leave`), so
    # a rename and such a leave never wait on each other both ways.
    rosters = [roster for roster in Roster.objects.select_for_update().order_by("month", "unit_id") if _uses(roster, old_code)]
    for roster in rosters:
        roster.grid = {nurse: {day: new_code if code == old_code else code for day, code in row.items()}
                       for nurse, row in roster.grid.items()}
    Roster.objects.bulk_update(rosters, ["grid"])


def _uses(roster: Roster, code: str) -> bool:
    return any(code in row.values() for row in roster.grid.values())


def frozen_cells_on_shifts_going(unit: Unit, going: set[str],
                                 since: dt.date) -> list[tuple[Roster, list[tuple[str, dt.date, str]]]]:
    """The unit's submitted and approved rosters from `since` on, each with its cells on a shift in `going`, as (hospital ID,
    date, code) by date. Only a re-plan or month close changes such a roster, and both give it whole to the solver, which
    refuses a cell on a shift its unit does not work. A draft is solved again instead. Hold the unit's lock."""
    rosters = Roster.objects.filter(unit=unit, month__gte=since, state__in=[RosterState.SUBMITTED, RosterState.APPROVED])
    found = []
    for roster in rosters.order_by("month"):
        cells = [(nurse, dt.date.fromisoformat(day), code) for nurse, row in roster.grid.items()
                 for day, code in row.items() if code in going]
        if cells:
            found.append((roster, sorted(cells, key=lambda cell: (cell[1], cell[0]))))
    return found


def using_shift(code: str) -> list[Roster]:
    """The rosters of open months whose grid holds this shift, oldest first: those of a month not over, and an approved one of a
    month over that has not closed, whose close will read it as worked. A closed month holds nothing: its close stored the hours
    it read, and closing it again reads those (`closes.making`). Nor does a draft or a submitted roster of a month over, which
    can never be approved and so never closes."""
    from closes.models import MonthClose  # The close reads rosters.

    closed = MonthClose.objects.filter(unit=OuterRef("unit"), month=OuterRef("month"))
    open_months = (Roster.objects.select_related("unit").exclude(Exists(closed))
                   .filter(Q(month__gte=membership.this_month()) | Q(state=RosterState.APPROVED)))
    return [roster for roster in open_months.order_by("month", "unit__name") if _uses(roster, code)]
