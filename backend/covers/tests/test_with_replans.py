"""Covers and re-plans, mid-June: a re-plan's version that puts one more nurse on a cover's shift-day fills it, as any version
does, and a cover accepted while a re-plan runs makes it end `outdated`, with nothing applied."""
import pytest

from covers.models import Cover, CoverState
from covers.tests.conftest import answered, offered, opened
from notifications.tests.test_events import as_user
from rosters.models import VersionOrigin
from rosters.tests.test_replan import SWAPPED, answered as solver_answered, in_force, mid_june, moved, replanned, under_way, while_it_runs  # noqa: F401
from solves.models import ReplanOutcome

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


def test_a_re_plans_version_putting_a_nurse_on_its_shift_day_fills_it(mona, er, solver, in_force):
    """From the 12th: Dana works the only Night on the 15th, and the scripted re-plan puts Amal on it beside her."""
    opened(mona, er, 15, "N")

    solve = replanned(mona, er, solver, solver_answered(moved(Amal_15="N")))

    assert solve.replan_outcome == ReplanOutcome.VERSION
    assert er.rosters.get().versions.first().origin == VersionOrigin.REPLAN
    assert Cover.objects.get().state == CoverState.FILLED


def test_a_cover_accepted_while_a_re_plan_runs_makes_it_end_outdated_with_nothing_applied(mona, er, solver, in_force):
    """Amal alone could take Night on the 15th, beside Dana."""
    cover_id = opened(mona, er, 15, "N")["cover"]["id"]
    offered(mona, er, cover_id, "Amal")
    while_it_runs(solver, lambda: answered(as_user("Amal"), cover_id, "accept"))

    solve = replanned(mona, er, solver, *under_way(SWAPPED))

    assert solve.replan_outcome == ReplanOutcome.OUTDATED
    latest = er.rosters.get().versions.first()
    assert latest.origin == VersionOrigin.COVER and latest.grid["Amal"]["2026-06-15"] == "N"
    assert Cover.objects.get().state == CoverState.TAKEN
