"""Swaps and re-plans, mid-June: a re-plan's version expires a swap whose cell it changes, as any version does, and a swap
approved while a re-plan runs makes the re-plan end `outdated`, with nothing applied."""
import pytest

from notifications.tests.test_events import as_user
from rosters.models import VersionOrigin
from rosters.tests.test_replan import SWAPPED, in_force, mid_june, replanned, under_way, while_it_runs  # noqa: F401
from solves.models import ReplanOutcome
from swaps.models import Swap, SwapState
from swaps.tests.conftest import cell, decided, proposed
from swaps.tests.test_swaps import awaiting

pytestmark = pytest.mark.django_db

# From the 12th: Dana works Night on the 15th and Amal is off. The scripted re-plan (`SWAPPED`) puts Amal on Day on the 15th.
ON_THE_15TH = [cell("Dana", 15), cell("Amal", 15, "N")]


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


def test_a_re_plans_version_changing_one_of_its_cells_expires_it(mona, er, solver, in_force):
    proposed(as_user("Dana"), *ON_THE_15TH)

    solve = replanned(mona, er, solver)

    assert solve.replan_outcome == ReplanOutcome.VERSION
    assert er.rosters.get().versions.first().origin == VersionOrigin.REPLAN
    swap = Swap.objects.get()
    assert (swap.state, swap.expired_why) == (SwapState.EXPIRED, "cell_changed")


def test_a_swap_approved_while_a_re_plan_runs_makes_it_end_outdated_with_nothing_applied(mona, er, solver, in_force):
    swap = awaiting(as_user, ON_THE_15TH, proposer="Dana")
    while_it_runs(solver, lambda: decided(mona, er, swap["id"], "approve"))

    solve = replanned(mona, er, solver, *under_way(SWAPPED))

    assert solve.replan_outcome == ReplanOutcome.OUTDATED
    assert Swap.objects.get().state == SwapState.APPROVED
    latest = er.rosters.get().versions.first()
    assert latest.origin == VersionOrigin.SWAP and latest.grid["Amal"]["2026-06-15"] == "N"
