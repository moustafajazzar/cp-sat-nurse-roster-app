"""The sentences a manager reads for each reason a cell cannot hold a change, and how a bar's sentence opens for anyone."""
import datetime as dt

import pytest

from rosters.cell_words import bar_said, manager_said
from rosters.cells import EDIT, REQUEST, SWAP, Reason

JUNE = dt.date(2026, 6, 1)
THE_21ST = dt.date(2026, 6, 21)
BAR = Reason("barred", skill="BLS", last=dt.date(2026, 6, 15))

IN_FULL = [
    (Reason("not_in_month"), "Amal Saleh is not on this unit's roster for June 2026."),
    (Reason("outside_dates"), "21 June 2026 is outside Amal Saleh's dates, so there is nothing to pin."),
    (Reason("leave"), "Amal Saleh is on leave on 21 June 2026, and leave wins over a pin."),
    (Reason("shift_not_worked", shift="A"), "A is not one of this unit's shifts."),
    (BAR, "Amal Saleh's BLS, required to work, counted until 15 June 2026, so Amal Saleh can work no shift on 21 June 2026."),
    (Reason("shift_not_allowed", shift="N"), "N is not one of Amal Saleh's allowed shifts."),
    (Reason("night_on_probation", shift="N"),
     "Amal Saleh is on probation in June 2026, and a nurse on probation works no N, the night shift."),
]


@pytest.mark.parametrize("asking", [EDIT, REQUEST, SWAP])
@pytest.mark.parametrize("reason, sentence", IN_FULL)
def test_a_reason_reads_the_same_on_an_edit_a_requests_decision_and_a_swap(reason, sentence, asking):
    assert manager_said(reason, "Amal Saleh", THE_21ST, JUNE, asking) == sentence


@pytest.mark.parametrize("asking, sentence", [
    (EDIT, "This pin comes from an accepted request, on 21 June 2026, so change it there."),
    (SWAP, "An accepted request holds Amal Saleh's cell on 21 June 2026, so change it there."),
])
def test_a_requests_pin_sends_the_manager_to_the_request(asking, sentence):
    assert manager_said(Reason("held", origin="request"), "Amal Saleh", THE_21ST, JUNE, asking) == sentence


@pytest.mark.parametrize("origin, sentence", [
    ("swap", "Amal Saleh's approved swap holds this cell on 21 June 2026. To grant the request, edit the cell first, which "
             "replaces the swap's pin, then decide the request."),
    ("cover", "The cover Amal Saleh took holds this cell on 21 June 2026. To grant the request, edit the cell first, which "
              "replaces the cover's pin, then decide the request."),
])
def test_a_swaps_or_a_covers_pin_on_a_requests_decision_names_the_way_through(origin, sentence):
    assert manager_said(Reason("held", origin=origin), "Amal Saleh", THE_21ST, JUNE, REQUEST) == sentence


def test_a_bars_sentence_opens_with_whose_skill_it_is_and_its_last_counted_day():
    assert bar_said(BAR, "Your") == "Your BLS, required to work, counted until 15 June 2026"
    assert bar_said(BAR, "Amal's") == "Amal's BLS, required to work, counted until 15 June 2026"
