"""Whether a cell of a unit's month can hold a change, asked of `rosters.cells` alone: each test builds the month in the
database, asks and reads the answer. The month is June 2026 of the four-nurse sample as ER, which works Day (D) and Night (N), its
night shift; Amal may work either and Emad only Day. No test here has a roster unless it says so: a pin, and so the question,
exists without one."""
import datetime as dt

import pytest

from accounts.models import User
from hospital.models import LeaveType, Shift, ShiftRename
from nurses.models import Leave, NurseProfile, Placement
from nurses.tests.conftest import make_unit
from rosters.cells import COVER, EDIT, REQUEST, SWAP, Answer, MonthCells, Reason
from rosters.models import PinEnded, PinnedCell, PinOrigin, Roster, RosterState
from rosters.pins import end
from rosters.tests.conftest import approved_roster
from rosters.tests.test_review import holding
from solves.tests.conftest import nurse_in
from solves.tests.probation_scene import add_faten

pytestmark = pytest.mark.django_db

JUNE = dt.date(2026, 6, 1)


@pytest.fixture(autouse=True)
def _scene(before_june):
    pass


def day(number: int) -> dt.date:
    return JUNE.replace(day=number)


def profile(hospital_id: str) -> NurseProfile:
    return NurseProfile.objects.get(user__username=hospital_id)


def manager() -> User:
    return User.objects.get(username="mona")


def test_a_cell_nothing_stands_on_may_hold_a_shift_or_a_day_off(er):
    cells = MonthCells(er, JUNE)

    assert cells.may_hold("Amal", day(3), EDIT, "N") == Answer()
    assert cells.may_hold("Amal", day(3), EDIT, None) == Answer()


@pytest.mark.parametrize("state", [None, RosterState.DRAFT, RosterState.APPROVED])
def test_the_answers_are_the_same_with_no_roster_a_draft_and_a_roster_in_force(er, state):
    if state is not None:
        approved_roster(er)
        Roster.objects.update(state=state)
    pinned(er, "Amal", 3, "N", PinOrigin.REQUEST)

    cells = MonthCells(er, JUNE)

    assert cells.may_hold("Amal", day(3), EDIT, "D") == Answer(reason=Reason("held", origin="request"))
    assert cells.may_hold("Emad", day(4), SWAP, "N") == Answer(reason=Reason("shift_not_allowed", shift="N"))
    assert cells.may_hold("Badr", day(4), COVER, "N") == Answer()


class TestEachReason:
    """One for each reason a cell cannot hold what is asked, with its facts."""

    def test_a_nurse_not_in_the_month(self, er, make_user):
        nurse_in(make_unit("ICU"), make_user, "Faris")

        cells = MonthCells(er, JUNE)

        assert cells.cannot_hold("Faris", day(3), "N") == Reason("not_in_month")
        assert cells.cannot_hold("Nobody", day(3), None) == Reason("not_in_month")

    def test_a_date_outside_her_dates(self, er):
        NurseProfile.objects.filter(user__username="Amal").update(end_date=day(20))

        cells = MonthCells(er, JUNE)

        assert cells.cannot_hold("Amal", day(21), None) == Reason("outside_dates")
        assert cells.cannot_hold("Amal", day(20), None) is None

    def test_a_day_of_leave(self, er):
        Leave.objects.create(nurse=profile("Amal"), date=day(3), leave_type=LeaveType.objects.first(), recorded_by=manager())

        cells = MonthCells(er, JUNE)

        assert cells.cannot_hold("Amal", day(3), None) == Reason("leave")
        assert cells.cannot_hold("Amal", day(3), "N") == Reason("leave")
        assert cells.cannot_hold("Amal", day(4), None) is None

    def test_a_shift_the_unit_does_not_work_named_as_it_was_asked(self, er):
        cells = MonthCells(er, JUNE)

        assert cells.cannot_hold("Amal", day(3), "A") == Reason("shift_not_worked", shift="A")
        assert cells.cannot_hold("Amal", day(3), "Zz") == Reason("shift_not_worked", shift="Zz")

    def test_a_shift_the_hospital_has_retired(self, er):
        Shift.objects.filter(code="N").update(active=False)

        assert MonthCells(er, JUNE).cannot_hold("Amal", day(3), "N") == Reason("shift_not_worked", shift="N")

    def test_a_shift_asked_by_a_code_it_once_held_is_the_shift_it_is_now(self, er):
        ShiftRename.objects.create(shift=Shift.objects.get(code="N"), old_code="NT", new_code="N", by=manager())

        cells = MonthCells(er, JUNE)

        assert cells.cannot_hold("Amal", day(3), "NT") is None
        assert cells.cannot_hold("Emad", day(3), "NT") == Reason("shift_not_allowed", shift="N")

    def test_a_shift_asked_as_the_row_a_cover_or_a_request_holds_is_judged_as_its_code_is(self, er):
        cells = MonthCells(er, JUNE)

        assert cells.cannot_hold("Amal", day(3), Shift.objects.get(code="N")) is None
        assert cells.cannot_hold("Emad", day(3), Shift.objects.get(code="N")) == Reason("shift_not_allowed", shift="N")
        assert cells.cannot_hold("Amal", day(3), Shift.objects.get(code="A")) == Reason("shift_not_worked", shift="A")

    def test_a_bar_with_its_skill_and_its_last_counted_day(self, er):
        holding("Amal", "BLS", day(15), required=True)

        cells = MonthCells(er, JUNE)

        assert cells.cannot_hold("Amal", day(16), "N") == Reason("barred", skill="BLS", last=day(15))
        assert cells.cannot_hold("Amal", day(15), "N") is None

    def test_a_day_off_is_still_hers_on_a_barred_day(self, er):
        holding("Amal", "BLS", day(15), required=True)

        assert MonthCells(er, JUNE).cannot_hold("Amal", day(16), None) is None

    def test_a_shift_that_is_not_one_of_her_allowed_shifts(self, er):
        cells = MonthCells(er, JUNE)

        assert cells.cannot_hold("Emad", day(3), "N") == Reason("shift_not_allowed", shift="N")
        assert cells.cannot_hold("Emad", day(3), "D") is None

    def test_the_night_shift_on_probation(self, er, make_user):
        add_faten(er, make_user)

        cells = MonthCells(er, JUNE)

        assert cells.cannot_hold("Faten", day(3), "N") == Reason("night_on_probation", shift="N")
        assert cells.cannot_hold("Faten", day(3), "D") is None
        assert cells.on_probation == {"Faten"}


    def test_a_pin_that_does_not_yield_with_its_origin(self, er):
        pinned(er, "Amal", 3, "N", PinOrigin.REQUEST)

        assert MonthCells(er, JUNE).may_hold("Amal", day(3), EDIT, "D") == Answer(reason=Reason("held", origin="request"))


def pinned(unit, hospital_id: str, number: int, code: str, origin: PinOrigin) -> PinnedCell:
    """A pin as its origin would have made it, written straight to the database."""
    shift = Shift.objects.get(code=code) if code else None
    return PinnedCell.objects.create(unit=unit, nurse=profile(hospital_id), date=day(number), shift=shift, origin=origin,
                                     pinned_by=manager())


REPLACES, REFUSED = "replaces", "refused"
# Which pin yields to which change as the app stands: {the origin asking: {the pin's origin: what becomes of the pin}}.
YIELDS = {
    EDIT: {PinOrigin.MANAGER: REPLACES, PinOrigin.REQUEST: REFUSED, PinOrigin.SWAP: REPLACES, PinOrigin.COVER: REPLACES},
    SWAP: {PinOrigin.MANAGER: REPLACES, PinOrigin.REQUEST: REFUSED, PinOrigin.SWAP: REPLACES, PinOrigin.COVER: REPLACES},
    COVER: {PinOrigin.MANAGER: REPLACES, PinOrigin.REQUEST: REFUSED, PinOrigin.SWAP: REPLACES, PinOrigin.COVER: REPLACES},
    REQUEST: {PinOrigin.MANAGER: REPLACES, PinOrigin.REQUEST: REPLACES, PinOrigin.SWAP: REFUSED, PinOrigin.COVER: REPLACES},
}
PAIRINGS = [(asking, origin, outcome) for asking, row in YIELDS.items() for origin, outcome in row.items()]


class TestWhichPinYields:
    @pytest.mark.parametrize("asking, origin, outcome", PAIRINGS)
    def test_each_pairing_of_the_origin_asking_and_the_pin_held(self, er, asking, origin, outcome):
        pin = pinned(er, "Amal", 3, "N", origin)
        expected = Answer(replaces=pin) if outcome == REPLACES else Answer(reason=Reason("held", origin=origin))

        cells = MonthCells(er, JUNE)

        assert cells.may_hold("Amal", day(3), asking, "D") == expected
        assert cells.may_hold("Amal", day(3), asking, None) == expected
        assert cells.may_change("Amal", day(3), asking) == expected

    @pytest.mark.parametrize("asking, origin, outcome", PAIRINGS)
    def test_a_pin_set_aside_still_holds_its_cell_in_each_pairing(self, er, asking, origin, outcome):
        """Set aside by its own shift, which she may no longer work, so the cell can still hold a Day or a day off."""
        pin = pinned(er, "Amal", 3, "N", origin)
        Placement.objects.filter(nurse=profile("Amal")).update(allowed_shifts=["D"])
        expected = Answer(replaces=pin) if outcome == REPLACES else Answer(reason=Reason("held", origin=origin))

        cells = MonthCells(er, JUNE)

        assert cells.set_aside(pin) == Reason("shift_not_allowed", shift="N")
        assert cells.may_hold("Amal", day(3), asking, "D") == expected
        assert cells.may_hold("Amal", day(3), asking, None) == expected
        assert cells.may_change("Amal", day(3), asking) == expected

    def test_a_pin_replaced_is_named_with_its_origin_and_what_it_holds(self, er):
        pinned(er, "Amal", 3, "N", PinOrigin.MANAGER)

        replaced = MonthCells(er, JUNE).may_hold("Amal", day(3), SWAP, None).replaces

        assert (replaced.origin, replaced.code) == ("manager", "N")

    def test_a_cell_with_no_pin_may_change(self, er):
        assert MonthCells(er, JUNE).may_change("Amal", day(3), EDIT) == Answer()

    def test_a_pin_ended_or_on_another_day_holds_nothing(self, er):
        pinned(er, "Amal", 4, "N", PinOrigin.REQUEST)
        ended = pinned(er, "Amal", 3, "N", PinOrigin.REQUEST)
        end([ended], PinEnded.CLEARED, manager())

        assert MonthCells(er, JUNE).may_hold("Amal", day(3), EDIT, "D") == Answer()

    def test_whether_the_change_may_be_made_at_all_asks_nothing_of_the_day_or_the_shift(self, er):
        """All a clear is asked: ending a pin can break nothing."""
        NurseProfile.objects.filter(user__username="Amal").update(end_date=day(2))
        pin = pinned(er, "Amal", 3, "N", PinOrigin.MANAGER)

        cells = MonthCells(er, JUNE)

        assert cells.may_change("Amal", day(3), EDIT) == Answer(replaces=pin)
        assert cells.may_hold("Amal", day(3), EDIT, "D") == Answer(reason=Reason("outside_dates"))


def left_for_another_unit(er, make_user) -> None:
    Placement.objects.create(nurse=profile("Amal"), unit=make_unit("ICU"), from_month=JUNE)


def ends_before_the_day(er, make_user) -> None:
    NurseProfile.objects.filter(user__username="Amal").update(end_date=day(10))


def on_leave_that_day(er, make_user) -> None:
    Leave.objects.create(nurse=profile("Amal"), date=day(20), leave_type=LeaveType.objects.first(), recorded_by=manager())


def unit_stops_working_night(er, make_user) -> None:
    er.rules["shifts"] = ["D"]
    er.save()


def barred_from_the_16th(er, make_user) -> None:
    holding("Amal", "BLS", day(15), required=True)


def allowed_only_day(er, make_user) -> None:
    Placement.objects.filter(nurse=profile("Amal")).update(allowed_shifts=["D"])


def put_on_probation(er, make_user) -> None:
    NurseProfile.objects.filter(user__username="Amal").update(on_probation=True, start_date=dt.date(2026, 4, 1))


# What comes to be so after Amal was pinned to Night on 20 June, and why that pin then counts for nothing.
ABOUT_THE_DAY = [(left_for_another_unit, Reason("not_in_month")), (ends_before_the_day, Reason("outside_dates")),
                 (on_leave_that_day, Reason("leave"))]
ABOUT_THE_SHIFT = [(unit_stops_working_night, Reason("shift_not_worked", shift="N")),
                   (barred_from_the_16th, Reason("barred", skill="BLS", last=dt.date(2026, 6, 15))),
                   (allowed_only_day, Reason("shift_not_allowed", shift="N")),
                   (put_on_probation, Reason("night_on_probation", shift="N"))]


class TestSetAside:
    def test_a_pin_its_cell_could_hold_now_counts(self, er):
        pin = pinned(er, "Amal", 20, "N", PinOrigin.MANAGER)

        assert MonthCells(er, JUNE).set_aside(pin) is None

    def test_the_months_pins_are_listed_by_nurse_and_date_without_those_ended_or_another_units(self, er, make_user):
        first, second = pinned(er, "Badr", 4, "N", PinOrigin.SWAP), pinned(er, "Amal", 20, "", PinOrigin.MANAGER)
        third = pinned(er, "Amal", 3, "N", PinOrigin.REQUEST)
        end([pinned(er, "Dana", 3, "N", PinOrigin.MANAGER)], PinEnded.CLEARED, manager())
        icu = make_unit("ICU")
        pinned(icu, nurse_in(icu, make_user, "Faris").user.username, 3, "", PinOrigin.MANAGER)

        assert MonthCells(er, JUNE).pins == [third, second, first]

    @pytest.mark.parametrize("comes_to_be, reason", ABOUT_THE_DAY + ABOUT_THE_SHIFT)
    def test_a_saved_pin_is_set_aside_for_the_reason_a_new_pin_on_its_cell_would_be_refused(self, er, make_user, comes_to_be,
                                                                                         reason):
        pin = pinned(er, "Amal", 20, "N", PinOrigin.MANAGER)
        comes_to_be(er, make_user)

        cells = MonthCells(er, JUNE)

        assert cells.set_aside(pin) == reason
        assert cells.cannot_hold("Amal", day(20), "N") == reason

    def test_a_pin_of_any_origin_is_set_aside_alike_whatever_holds_its_cell(self, er):
        pin = pinned(er, "Amal", 20, "N", PinOrigin.REQUEST)
        allowed_only_day(er, None)

        assert MonthCells(er, JUNE).set_aside(pin) == Reason("shift_not_allowed", shift="N")

    def test_a_pin_to_a_day_off_counts_on_a_barred_day(self, er):
        pin = pinned(er, "Amal", 20, "", PinOrigin.MANAGER)
        barred_from_the_16th(er, None)

        assert MonthCells(er, JUNE).set_aside(pin) is None

    @pytest.mark.parametrize("comes_to_be, reason", ABOUT_THE_DAY)
    def test_in_a_month_that_is_over_a_reason_about_the_day_still_sets_it_aside(self, er, make_user, comes_to_be, reason):
        pin = pinned(er, "Amal", 20, "N", PinOrigin.MANAGER)
        comes_to_be(er, make_user)

        assert MonthCells(er, JUNE).set_aside(pin, over=True) == reason

    @pytest.mark.parametrize("comes_to_be, reason", ABOUT_THE_SHIFT)
    def test_in_a_month_that_is_over_a_pin_to_a_shift_she_could_not_work_now_still_counts(self, er, make_user, comes_to_be,
                                                                                       reason):
        """Its shift was worked when it was set, and the month's close makes that fact."""
        pin = pinned(er, "Amal", 20, "N", PinOrigin.MANAGER)
        comes_to_be(er, make_user)

        assert MonthCells(er, JUNE).set_aside(pin, over=True) is None


class TestTheirOrder:
    """The day's reasons come before the shift's, and of the shift's the one that would stop any nurse comes first."""

    def test_the_day_is_said_before_the_shift(self, er):
        NurseProfile.objects.filter(user__username="Emad").update(end_date=day(20))

        assert MonthCells(er, JUNE).cannot_hold("Emad", day(21), "A") == Reason("outside_dates")

    def test_a_shift_not_worked_is_said_before_her_bar_and_her_bar_before_her_allowed_shifts(self, er):
        holding("Emad", "BLS", day(15), required=True)

        cells = MonthCells(er, JUNE)

        assert cells.cannot_hold("Emad", day(16), "A") == Reason("shift_not_worked", shift="A")
        assert cells.cannot_hold("Emad", day(16), "N") == Reason("barred", skill="BLS", last=day(15))

    def test_a_pin_that_does_not_yield_is_said_last(self, er):
        pinned(er, "Emad", 3, "D", PinOrigin.REQUEST)

        cells = MonthCells(er, JUNE)

        assert cells.may_hold("Emad", day(3), SWAP, "N") == Answer(reason=Reason("shift_not_allowed", shift="N"))
        assert cells.may_hold("Emad", day(3), SWAP, None) == Answer(reason=Reason("held", origin="request"))
