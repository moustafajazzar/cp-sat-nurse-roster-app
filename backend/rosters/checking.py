"""A grid checked and accounted with the solver's own code in this process, for a draft, a day's page and a change to the roster
in force alike. It reads nothing from the database, so the judgement of a cell (`rosters.cells`) and the draft's edits
(`rosters.editing`) can both build on it."""
from __future__ import annotations

from dataclasses import dataclass

from solver_service.grid import nurses_of, validate_grid


@dataclass
class Checked:
    """What a grid breaks and costs, or `reason` why that could not be worked out."""
    reason: str | None
    answer: dict | None = None

    @property
    def cost(self) -> int | None:
        return self.answer["cost"] if self.answer else None

    @property
    def violations(self) -> list[dict]:
        """The hard rules the grid breaks; none are known of a grid that could not be checked."""
        return self.answer["violations"] if self.answer else []


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
