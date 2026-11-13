"""A solve is how a draft comes to be, so these tests use the solves app's scene."""
import datetime as dt
import json
from pathlib import Path

from django.utils import timezone

from rosters.models import Roster, RosterState, RosterVersion, VersionOrigin
from solves.input import assemble
from solves.models import Solve, SolveState
from solves.tests.conftest import before_june, catalogue, er, mona, result_of, solver  # noqa: F401
from units.models import Unit

SOUND = json.loads((Path(__file__).parent / "fixtures" / "sound_rosters.json").read_text())


def approved_roster(unit: Unit, month: str = "2026-06") -> Roster:
    """The month's roster approved as version 1, written straight to the database with no endpoint, for a test that starts from
    the roster in force. Its grid is the real one of the four-nurse sample (`fixtures/sound_rosters.json`), so the solver's own
    checks find it sound; its solve is a finished one of that grid, as an approval needs."""
    first = dt.date.fromisoformat(f"{month}-01")
    sound = SOUND[month]
    solve = Solve.objects.create(unit=unit, month=first, requested_by=unit.manager, time_limit_seconds=60,
                                 state=SolveState.SUCCEEDED, finished_at=timezone.now(), input=assemble(unit, first).input,
                                 result=result_of(sound["roster"], cost=sound["cost"]) | {"month": month})
    roster = Roster.objects.create(unit=unit, month=first, state=RosterState.APPROVED, grid=sound["roster"], solve=solve)
    RosterVersion.objects.create(roster=roster, number=1, grid=sound["roster"], origin=VersionOrigin.SOLVE, solve=solve,
                                 created_by=unit.manager)
    return roster


def locks_on(queries: list[dict], *tables: str) -> list[tuple[str, str]]:
    """Each row lock these captured queries (`CaptureQueriesContext.captured_queries`) took on one of these tables, in the order
    taken, as the table and its statement: how a test proves the order of the locks at the database itself."""
    return [(table, query["sql"]) for query in queries if " FOR " in query["sql"]
            for table in tables if f'FROM "{table}"' in query["sql"]]
