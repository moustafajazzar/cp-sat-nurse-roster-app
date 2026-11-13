"""A nurse on probation on an approved roster: she proposes no swap and is no colleague in one; a swap, a
cover or a manager's edit that breaks the following rule is refused or shown; she is never listed for a cover; the Day view counts
her toward nothing; the request form offers her day shifts only; and a night pin of hers is refused.

The roster is one the real solver found for `probation_scene`'s June, with every cell pinned here so that no machine's search
can give another: the solver, in this process, only confirms it breaks no hard rule. Its rows, a dot for a day off, day 1 first:

    Amal  N..D..D.N.NNN....NNNN.N.NNNN.N
    Badr  ..D.NNNN.NNN.NNNN..D.D....D.DD
    Dana  NNNN..N..DDDD.D.N.N..N.N....N.
    Emad  DD..DD.DD....D.DDDD.D.DDDD.D..
    Faten ...D.DD.DD.DDD.DDDD.D..DDD.D..

Amal, her preceptor, works mostly nights: D on the 4th and the 7th, where Faten is beside her. Faten fills her other hours on
days of her own, which the rule allows while she misses none of Amal's. On the 5th Amal and Faten are both off and Emad works
the only counted D. So Amal on D on the 5th would be a day Faten could follow and is off, while she works the 6th on her own:
the rule's second form, "once one is left off, she works no other free day"."""
import datetime as dt

import pytest

from conftest import post
from covers.judging import CoverMonth
from hospital.models import Shift
from requesting.mine_api import requestable_shifts
from rosters.models import Roster
from rosters.tests.test_day import counts, row as unit_row
from rosters.tests.test_review import approve, carla, submit  # noqa: F401
from solves import running
from solves.input import assemble
from solves.tests.conftest import started
from solves.tests.probation_scene import add_faten, assign, by_row, holds_the_skill, real_job
from swaps.tests.conftest import MONTH, cell, nurse, ok, propose, refusals, url  # noqa: F401

pytestmark = pytest.mark.django_db
JUNE, JULY = dt.date(2026, 6, 1), dt.date(2026, 7, 1)


ROWS = {
    "Amal": "N..D..D.N.NNN....NNNN.N.NNNN.N",
    "Badr": "..D.NNNN.NNN.NNNN..D.D....D.DD",
    "Dana": "NNNN..N..DDDD.D.N.N..N.N....N.",
    "Emad": "DD..DD.DD....D.DDDD.D.DDDD.D..",
    "Faten": "...D.DD.DD.DDD.DDDD.D..DDD.D..",
}


@pytest.fixture(autouse=True)
def june(before_june, er, make_user, mona, carla, solver):
    faten = add_faten(er, make_user)
    holds_the_skill("Amal")
    assign(mona, er, faten.pk, "Amal", "2026-05")
    solve_input = assemble(er, JUNE).input
    for row in solve_input["nurses"]:
        row["pinned"] = {f"2026-06-{day:02d}": "" if code == "." else code for day, code in enumerate(ROWS[row["id"]], start=1)}
    solver.script([real_job(solve_input)])
    running.run(started(mona, er)["id"])
    ok(submit(mona, er))
    ok(approve(carla, er))
    grid = Roster.objects.get(unit=er, month=JUNE).grid
    assert {hospital_id: by_row(grid, hospital_id) for hospital_id in ROWS} == ROWS
    return faten


AMAL_ON_THE_5TH = [cell("Amal", 5, "D"), cell("Emad", 5)]
FOLLOWING = ("This swap cannot work on 6 June 2026: Faten Ali is on probation and works beside her preceptor, Amal, on their "
             "day shifts.")


def test_she_cannot_propose_a_swap_and_is_told_why(nurse):
    faten = nurse("Faten")
    said = ("You are on probation in June 2026, and a nurse on probation takes no part in a swap: you work beside your preceptor, "
            "and the roster is planned around that.")

    assert ok(faten.get("/api/swaps/compose", {"month": MONTH}))["refusal"] == said
    assert refusals(propose(faten, cell("Faten", 10), cell("Badr", 10, "D")), 409) == [said]


def test_she_is_no_colleague_in_a_swap(nurse):
    badr = nurse("Badr")

    assert "Faten" not in [row["hospital_id"] for row in ok(badr.get("/api/swaps/compose", {"month": MONTH}))["nurses"]]
    assert refusals(propose(badr, cell("Badr", 10, "D"), cell("Faten", 10))) == [
        "Faten Ali is on probation in June 2026, so a swap cannot change their cells."]


def test_a_swap_that_breaks_the_following_rule_is_refused_in_what_my_roster_shows(nurse):
    said = refusals(propose(nurse("Amal"), *AMAL_ON_THE_5TH))

    assert FOLLOWING in said
    assert not any("following:" in sentence for sentence in said)  # the solver's words are the manager's


def test_a_managers_edit_that_breaks_it_shows_the_violation(mona, er):
    edits = [{"hospital_id": "Amal", "date": "2026-06-05", "action": "shift", "shift": "D"},
             {"hospital_id": "Emad", "date": "2026-06-05", "action": "off"}]

    answer = ok(post(mona, url(er, "/roster/check"), {"month": MONTH, "edits": edits}))

    following = [row["text"] for row in answer["violations"] if row["rule"] == "following"]
    assert "following: Faten works D on 2026-06-06, when her preceptor Amal is off, while she is off on 2026-06-05, when Amal " \
           "works D" in following


def test_a_night_pin_of_hers_is_refused(mona, er):
    edits = [{"hospital_id": "Faten", "date": "2026-06-20", "action": "shift", "shift": "N"}]

    answer = ok(post(mona, url(er, "/roster/check"), {"month": MONTH, "edits": edits}))

    assert [row["reason"] for row in answer["refusals"]] == [
        "Faten Ali is on probation in June 2026, and a nurse on probation works no N, the night shift."]


def test_she_is_never_listed_for_a_cover_and_one_that_breaks_the_rule_is_refused(mona, er):
    roster = Roster.objects.get(unit=er, month=JUNE)
    month = CoverMonth(roster, dt.date(2026, 6, 5), Shift.objects.get(code="D"))

    assert month.exclusion("Faten") == "on_probation"
    assert month.refusal_for("Faten") == ["You are on probation in June 2026, and a nurse on probation takes no cover: a shift is "
                                          "fully staffed without her."]
    _, broken = month.with_her("Amal")
    assert "following" in {row["rule"] for row in broken}
    # The broken rule is Faten's, a colleague's, so Amal reads it as the unit's rules on that day, as a swap's proposer would.
    assert "You cannot take this cover: the shifts on 5 June 2026 would break the unit's rules." in month.refusal_for("Amal")


def test_the_cover_list_leaves_her_out(mona, er):
    from covers.tests.conftest import cover_page, record_leave

    assert record_leave(mona, er, "Emad", 5).status_code == 201
    cover = next(row for row in ok(mona.get(url(er, "/covers"), {"month": MONTH}))["covers"] if row["date"] == "2026-06-05")

    listed = [row["hospital_id"] for row in cover_page(mona, er, cover["id"])["check"]["nurses"]]

    # Badr works N that day and Dana rests after a night; Faten and Amal are left out as the test above says why.
    assert "Faten" not in listed and "Amal" not in listed


def test_the_day_view_counts_her_toward_nothing_and_lists_her(carla):
    answer = ok(carla.get("/api/rosters/day", {"date": "2026-06-04"}))

    assert counts(answer)["D"] == "1 of 1-1"  # Amal; Faten beside her counts toward nothing
    on_d = next(shift for shift in unit_row(answer)["figures"]["shifts"] if shift["code"] == "D")
    assert [person["hospital_id"] for person in on_d["nurses"]] == ["Amal", "Faten"]


def test_the_request_form_offers_her_day_shifts_only_while_she_is_on_probation(er, june):
    assert [shift.code for shift in requestable_shifts(june, JUNE, er)] == ["D"]
    assert [shift.code for shift in requestable_shifts(june, JULY, er)] == ["D", "N"]
