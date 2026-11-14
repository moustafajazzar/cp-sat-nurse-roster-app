"""A manager's hand edits of the draft: what each would do, whether it may, and what the grid would then break and cost."""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass
from typing import Literal

from solver_service.grid import nurses_of, validate_grid

from hospital.models import Shift
from rosters.drafts import current_code, in_current_codes
from rosters.models import PinEnded, PinnedCell, PinOrigin, Roster
from rosters.pins import Judge, end, long_date, pin_cell
from solves.input import assemble

Action = Literal["shift", "off", "clear"]


@dataclass(frozen=True)
class Edit:
    hospital_id: str
    date: dt.date
    action: Action
    shift: str | None = None

    @property
    def day(self) -> str:
        return self.date.isoformat()


class DraftEdits:
    """A draft and what may be done to it by hand, read once for all of a save's or a check's edits."""

    def __init__(self, roster: Roster):
        self.roster = roster
        self.judge = Judge(roster.unit, roster.month)
        self.nurses = {nurse.user.username: nurse for nurse in self.judge.nurses.values()}
        self.pins = {(pin.nurse.user.username, pin.date): pin for pin in PinnedCell.objects.current().in_month(roster.month)
                     .filter(unit=roster.unit).select_related("nurse__user", "shift")}
        self.shifts = {shift.code: shift for shift in Shift.objects.filter(code__in=roster.unit.rules.get("shifts", []))}

    def shift_of(self, edit: Edit) -> Shift | None:
        return self.shifts.get(current_code(edit.shift or ""))

    def refusal(self, edit: Edit) -> str | None:
        """Why this edit may not be made. Clearing is always open, on a pin set aside too: nothing it does can break a rule."""
        pin = self.pins.get((edit.hospital_id, edit.date))
        if pin is not None and pin.origin != PinOrigin.MANAGER:
            return f"This pin comes from an accepted {pin.origin}, on {long_date(edit.date)}, so change it there."
        if edit.action == "clear":
            return None
        nurse = self.nurses.get(edit.hospital_id)
        # In one sentence, and the same for a nurse of another unit as for no such nurse.
        if nurse is None or edit.day not in self.roster.grid.get(edit.hospital_id, ()):
            return f"{edit.hospital_id} has no cell on {long_date(edit.date)} in this roster."
        asked = edit.shift if edit.action == "shift" else None
        return self.judge.refusal(nurse.pk, nurse.user.full_name or edit.hospital_id, edit.date, asked, self.shift_of(edit))

    def cell_after(self, edit: Edit) -> str:
        """Clearing gives the cell back to its solve. On a credited day, leave wins, which is also how a manager takes a shift off
        a day that became leave after the solve; and a nurse no longer in the unit's month, such as one transferred out, works
        nothing here, so clearing takes their shift off."""
        if edit.action == "shift":
            return self.shift_of(edit).code
        if edit.action == "off":
            return ""
        nurse = self.nurses.get(edit.hospital_id)
        if nurse is None or self.judge.day_why(nurse.pk, edit.date) in ("leave", "outside_dates"):
            return ""
        solved = in_current_codes(self.roster.solve.result["roster"])
        return solved.get(edit.hospital_id, {}).get(edit.day, "")

    def grid_after(self, edits: list[Edit]) -> dict[str, dict[str, str]]:
        grid = copy.deepcopy(self.roster.grid)
        for edit in edits:
            if edit.day in grid.get(edit.hospital_id, ()):
                grid[edit.hospital_id][edit.day] = self.cell_after(edit)
        return grid

    def save(self, edits: list[Edit], by) -> tuple[int, int]:
        """A draft's: the pins and the grid. How many pins and how many of the grid's cells this changed. Takes no lock of its
        own: see `rosters.api.save_pins`."""
        changed = self.save_pins(edits, by)
        grid = self.grid_after(edits)
        cells = sum(grid[nurse][day] != code for nurse, row in self.roster.grid.items() for day, code in row.items())
        self.roster.grid = grid
        self.roster.save(update_fields=["grid"])  # `updated_at` stays: see its help text.
        return changed, cells

    def save_pins(self, edits: list[Edit], by) -> int:
        """The pins alone, as the roster in force needs them, whose grid only a change writes (`rosters.in_force`). How many pins
        this made or ended."""
        changed = 0
        for edit in edits:
            pin = self.pins.get((edit.hospital_id, edit.date))
            wanted = None if edit.action != "shift" else self.shift_of(edit)
            if pin is not None and edit.action != "clear" and pin.shift_id == (wanted.pk if wanted else None):
                continue
            if edit.action == "clear" and pin is not None:
                end([pin], PinEnded.CLEARED, by)
            elif edit.action != "clear":
                pin_cell(self.roster.unit, self.nurses[edit.hospital_id], edit.date, wanted, PinOrigin.MANAGER, by)
            changed += edit.action != "clear" or pin is not None
        return changed


@dataclass
class Checked:
    """What a grid breaks and costs, or `reason` why that could not be worked out."""
    reason: str | None
    answer: dict | None = None

    @property
    def cost(self) -> int | None:
        return self.answer["cost"] if self.answer else None


def _on_the_month(solve_input: dict, grid: dict[str, dict[str, str]]) -> tuple[dict, dict[str, dict[str, str]]]:
    """The loader wants exactly the input's nurses. One who left has no say in the month, and one who joined since the solve
    has no shifts yet, nor any pin of theirs a cell to break: the draft already warns of both."""
    days = {day for row in grid.values() for day in row}
    rows = [row if row["id"] in grid else {key: value for key, value in row.items() if key != "pinned"}
            for row in solve_input["nurses"]]
    return {**solve_input, "nurses": rows}, {row["id"]: grid.get(row["id"]) or dict.fromkeys(days, "") for row in rows}


def scored(solve_input: dict, grid: dict[str, dict[str, str]]) -> Checked:
    """In this process with the solver's own code, like a stored result's explanation (`solves.result.explanation`), so a
    search under way or a solver that is down cannot stop it. A grid or rules the loader refuses are an answer to show, not an
    error."""
    try:
        return Checked(None, validate_grid(*_on_the_month(solve_input, grid)))
    except ValueError as e:
        return Checked(str(e))


def accounted(solve_input: dict, grid: dict[str, dict[str, str]]) -> dict | None:
    """Each nurse's hours on this grid, with the solver's own arithmetic (`nurse_roster.result.nurse_accounting`, a result's
    `nurses` block, which My month's `rosters.own_month` matches). None when the loader refuses the grid or the rules."""
    try:
        return nurses_of(*_on_the_month(solve_input, grid))
    except ValueError:
        return None


def with_edits(solve_input: dict, month: DraftEdits, edits: list[Edit]) -> dict:
    """The input as it would be once these are saved, so the solver's `pin` rule reads the grid they make."""
    edited = copy.deepcopy(solve_input)
    rows = {row["id"]: row for row in edited["nurses"]}
    for edit in edits:
        row = rows.get(edit.hospital_id)
        if row is None:
            continue
        pinned = row.setdefault("pinned", {})
        if edit.action == "clear":
            pinned.pop(edit.day, None)
        else:
            pinned[edit.day] = month.cell_after(edit)
        if not pinned:
            del row["pinned"]
    return edited


def check_saved(roster: Roster) -> Checked:
    return scored(assemble(roster.unit, roster.month).input, roster.grid)
