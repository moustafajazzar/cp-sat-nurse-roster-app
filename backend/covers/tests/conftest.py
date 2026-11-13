"""Covers are opened on an approved roster, so these tests use the swap tests' scene: the solver's four-nurse sample as ER, its
June 2026 approved as the real optimal roster of `rosters/tests/fixtures/sound_rosters.json`, from 15 May, with the scripted
solver and the real worker's code. Every check of a grid is the solver's own code.

That June, as the sample's rules have it: Day (D) wants exactly one nurse every day and Night (N) one or two; Emad works only Day;
a Night followed by a Day is forbidden; at most four shifts in a row. Neither a team leader nor both genders is required until a
test asks for it (Amal and Emad lead teams; Badr and Emad are men).

        1 2 3 4 5 6 7 8 9 10 ...
  Amal  N N N N . N N N . .
  Badr  . D . . . D . . D .
  Dana  . . . N N . . . N N
  Emad  D . D D D . D D . D

So on the 9th Badr works the only Day and Dana the only Night; Amal and Emad are off."""
import pytest

from conftest import post
from hospital.models import LeaveType, Shift
from nurses.models import NurseProfile
from rosters.tests.conftest import before_june, catalogue, er, mona, solver  # noqa: F401
from rosters.tests.test_review import carla, dina  # noqa: F401
from swaps.tests.conftest import MONTH, in_force, nurse, ok, on_day, refusals, url  # noqa: F401


def profile(hospital_id) -> NurseProfile:
    return NurseProfile.objects.get(user__username=hospital_id)


def record_leave(client, unit, hospital_id, first, last=None):
    return post(client, url(unit, "/leave"), {
        "nurse_id": str(profile(hospital_id).pk), "leave_type_id": str(LeaveType.objects.get(name="Sick").pk),
        "first": f"{MONTH}-{first:02d}", "last": f"{MONTH}-{(last or first):02d}"})


def covers_of(client, unit, month=MONTH) -> list[dict]:
    return ok(client.get(url(unit, "/covers"), {"month": month}))["covers"]


def cover_page(client, unit, cover_id) -> dict:
    return ok(client.get(url(unit, f"/covers/{cover_id}")))


def open_cover(client, unit, day, shift, month=MONTH):
    return post(client, url(unit, "/covers"), {"date": f"{month}-{day:02d}" if isinstance(day, int) else day, "shift": shift})


def opened(client, unit, day, shift) -> dict:
    return ok(open_cover(client, unit, day, shift), 201)


def withdraw(client, unit, cover_id):
    return post(client, url(unit, f"/covers/{cover_id}/withdraw"), {})


def rules_require(unit, **rules):
    unit.rules = unit.rules | rules
    unit.save()


def day_shift() -> Shift:
    return Shift.objects.get(code="D")


def offer(client, unit, cover_id, *hospital_ids, note=""):
    return post(client, url(unit, f"/covers/{cover_id}/offer"), {"nurses": list(hospital_ids), "note": note})


def offered(client, unit, cover_id, *hospital_ids, note="") -> dict:
    return ok(offer(client, unit, cover_id, *hospital_ids, note=note))


def my_covers(client, month=MONTH) -> list[dict]:
    return ok(client.get("/api/covers/mine", {"month": month} if month else {}))["covers"]


def answer(client, cover_id, what):
    """`accept` or `decline`, by a nurse offered it."""
    return post(client, f"/api/covers/mine/{cover_id}/{what}", {})


def answered(client, cover_id, what) -> dict:
    return ok(answer(client, cover_id, what))


def wider_day(unit):
    """Day may hold two nurses, so a Day worked by one has room for one more."""
    unit.rules["headcounts"][0].update(weekday_max=2, weekend_max=2)
    unit.save()


def badrs_day(client, unit) -> str:
    """Badr's leave on the 9th, and the cover it opens for Day there."""
    ok(record_leave(client, unit, "Badr", 9), 201)
    return covers_of(client, unit)[0]["id"]


def the_25th(client, unit) -> str:
    wider_day(unit)
    return opened(client, unit, 25, "D")["cover"]["id"]
