"""The facts that set each reason the cell judgement classes as private (`rosters.cells.READERS`) on Emad's cell on the 9th of
the swap tests' scene, which the cover tests share. A swap and a cover each say every one of them in words of their own, so the
facts are set up once here and each origin's tests pair them with its words (`with_words`)."""
import datetime as dt

from accounts.models import Role, User
from conftest import post
from hospital.models import LeaveType
from notifications.tests.test_events import as_user
from nurses.models import NurseProfile, Placement
from requesting.tests.conftest import asked, decided, opened
from rosters.tests.test_pins import icu
from swaps.tests.conftest import MONTH, url


def profile(hospital_id) -> NurseProfile:
    return NurseProfile.objects.get(user__username=hospital_id)


def transfer(mona, unit):
    other = User.objects.create_user("omar", "correct-horse-battery", Role.UNIT_MANAGER)
    Placement.objects.create(nurse=profile("Emad"), unit=icu(other), from_month=dt.date(2026, 6, 1))


def end_date(mona, unit):
    NurseProfile.objects.filter(user__username="Emad").update(end_date=dt.date(2026, 6, 8))


def leave(mona, unit):
    response = post(mona, url(unit, "/leave"), {
        "nurse_id": str(profile("Emad").pk), "leave_type_id": str(LeaveType.objects.get(name="Sick").pk),
        "first": f"{MONTH}-09", "last": f"{MONTH}-09"})
    assert response.status_code == 201, response.content


def barred(mona, unit):
    """Emad's BLS, required to work, lasted to the 8th, recorded since the approval."""
    from rosters.tests.test_review import holding
    holding("Emad", "BLS", dt.date(2026, 6, 8), required=True)


def accepted_request(mona, unit):
    opened(mona, unit)
    decided(mona, unit, asked(as_user("Emad"), 9)["id"], "accept")


# {reason: (the fact that sets it, the shift asked of his cell)}. Emad works only Day, so Night is a shift he may not work.
PRIVATE_FACTS = {
    "not_in_month": (transfer, "D"),
    "outside_dates": (end_date, "D"),
    "leave": (leave, "D"),
    "barred": (barred, "D"),
    "shift_not_allowed": (None, "N"),
    "held": (accepted_request, "D"),
}


def with_words(words: dict[str, tuple[str, ...]]) -> dict[str, tuple]:
    """{reason: (the fact, the shift, *what an origin says of it)}, from `words` {reason: (what it says, ...)}. Every private
    reason must have its words, so an origin cannot leave one unsaid."""
    assert set(words) == set(PRIVATE_FACTS), set(words) ^ set(PRIVATE_FACTS)
    return {why: (*PRIVATE_FACTS[why], *words[why]) for why in PRIVATE_FACTS}
