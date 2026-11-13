"""A nurse on probation's preceptor: assigning and changing it on her Rostering attributes row, the list it offers,
each refusal, the endings, the notifications and the audit entries. Today is 11 November 2026 unless a test moves it. Layan
starts on 15 December 2026 with the 3 months her probation stands at, so it covers December to March and ends on 31 March 2027,
counted by hand."""
import datetime as dt

import pytest

from accounts.models import Role, User
from audit.models import AuditEntry
from conftest import post
from hospital.catalogue import seed_catalogue
from hospital.models import Skill
from notifications.models import Event, Notification
from notifications.reading import Names, text_of
from nurses.models import NurseProfile, NurseSkill, PreceptorAssignment
from nurses.tests.conftest import NURSES, added, make_unit
from rosters.models import Roster, RosterState
from solves.models import Solve

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def catalogue(db):
    seed_catalogue()


@pytest.fixture(autouse=True)
def today(on_day):
    on_day(2026, 11, 11)
    return on_day


@pytest.fixture
def mona(client_of):
    return client_of("mona", Role.UNIT_MANAGER)


@pytest.fixture
def er(mona):
    return make_unit("ER", User.objects.get(username="mona"))


@pytest.fixture
def omar(client_of):
    return client_of("omar", Role.UNIT_MANAGER)


@pytest.fixture
def icu(omar):
    return make_unit("ICU", User.objects.get(username="omar"))


@pytest.fixture
def precepting(db):
    return Skill.objects.create(name="Preceptorship", expires=True, required_to_work=False, lets_a_nurse_precept=True)


def nurse(cno, unit, hospital_id, full_name, start="2026-01-10", **fields) -> NurseProfile:
    return NurseProfile.objects.get(pk=added(cno, unit, hospital_id, full_name=full_name, start_date=start, **fields)["nurse"]["id"])


def holds(nurse_profile, skill, expiry: dt.date | None = dt.date(2027, 6, 30)) -> None:
    NurseSkill.objects.create(nurse=nurse_profile, skill=skill, expiry=expiry, recorded_by=User.objects.get(username="chief"))


@pytest.fixture
def layan(cno, er):
    return nurse(cno, er, "h1", "Layan Alharbi", start="2026-12-15", on_probation=True)


@pytest.fixture
def amal(cno, er, precepting):
    amal = nurse(cno, er, "h2", "Amal Hassan")
    holds(amal, precepting)
    return amal


def row(client, unit, nurse_id) -> dict:
    response = client.get(f"/api/units/{unit.pk}/nurses")
    assert response.status_code == 200, response.content
    return next(each for each in response.json() if each["id"] == str(nurse_id))


def preceptors(client, unit, nurse_id) -> list[tuple]:
    return [(each["hospital_id"], each["from_month"], each["to_month"]) for each in row(client, unit, nurse_id)["probation"]["preceptors"]]


def choices(client, unit, nurse_id, month: str) -> dict:
    response = client.get(f"/api/units/{unit.pk}/nurses/{nurse_id}/preceptor-choices", {"month": month})
    assert response.status_code == 200, response.content
    return response.json()


def offered(client, unit, nurse_id, month: str) -> list[str]:
    return [each["hospital_id"] for each in choices(client, unit, nurse_id, month)["nurses"]]


def assign(client, unit, nurse_id, preceptor, month: str):
    return client.put(f"/api/units/{unit.pk}/nurses/{nurse_id}/preceptor", {"preceptor_id": str(preceptor.pk), "from_month": month},
                      content_type="application/json")


def assigned(client, unit, nurse_id, preceptor, month: str) -> dict:
    response = assign(client, unit, nurse_id, preceptor, month)
    assert response.status_code == 200, response.content
    return response.json()


def refusal(response) -> str:
    assert response.status_code == 422, response.content
    return response.json()["detail"][0]["msg"]


def told(username: str) -> list[str]:
    notes = list(Notification.objects.filter(user__username=username).order_by("created_at", "pk"))
    names = Names(notes)
    return [text_of(note, names) for note in notes if note.event in (Event.PRECEPTOR_ASSIGNED, Event.PRECEPTOR_LEAVING)]


def approve(unit, month: dt.date) -> None:
    solve = Solve.objects.create(unit=unit, month=month, requested_by=unit.manager, input={}, time_limit_seconds=60)
    Roster.objects.create(unit=unit, month=month, solve=solve, grid={}, state=RosterState.APPROVED)


# ---- assigning --------------------------------------------------------------------------------------------------------------
def test_a_preceptor_is_assigned_from_a_month_and_precepts_her_to_her_probations_last_month(mona, er, layan, amal):
    answer = assigned(mona, er, layan.pk, amal, "2026-12")

    assert [(each["hospital_id"], each["from_month"], each["to_month"]) for each in answer["probation"]["preceptors"]] == [
        ("h2", "2026-12", "2027-03")]
    assert answer["probation"]["preceptor_months"] == ["2026-12", "2027-01", "2027-02", "2027-03"]


def test_both_nurses_are_told_once(mona, er, layan, amal):
    assigned(mona, er, layan.pk, amal, "2026-12")
    assigned(mona, er, layan.pk, amal, "2026-12")  # The same choice again changes nothing and tells no one.

    assert told("h1") == ["From December 2026 your preceptor is Amal Hassan. You work beside them on their day shifts."]
    assert told("h2") == ["From December 2026 you precept Layan Alharbi, a nurse on probation, who works beside you on your day "
                          "shifts."]
    assert told("mona") == []


def test_the_assignment_is_audited_with_its_values(mona, er, layan, amal, cno, precepting):
    badr = nurse(cno, er, "h3", "Badr Omar")
    holds(badr, precepting)
    AuditEntry.objects.all().delete()

    assigned(mona, er, layan.pk, amal, "2026-12")
    assigned(mona, er, layan.pk, badr, "2027-02")

    first, second = AuditEntry.objects.order_by("pk")
    assert (first.action, first.target, first.actor.username) == ("nurse.preceptor_assigned", "h1", "mona")
    assert first.before == {"preceptor": None}
    assert first.after == {"preceptor": "h2", "from_month": "2026-12", "until": None}
    assert second.before == {"preceptor": "h2", "from_month": "2026-12", "until": None}
    assert second.after == {"preceptor": "h3", "from_month": "2027-02", "until": None}


def test_a_change_frees_the_earlier_preceptor_from_that_month_and_tells_all_three_once(mona, er, layan, amal, cno, precepting):
    badr = nurse(cno, er, "h3", "Badr Omar")
    holds(badr, precepting)
    huda = nurse(cno, er, "h4", "Huda Saad", start="2026-12-01", on_probation=True)
    assigned(mona, er, layan.pk, amal, "2026-12")
    assert "h2" not in offered(mona, er, huda.pk, "2027-02")

    assigned(mona, er, layan.pk, badr, "2027-02")

    assert preceptors(mona, er, layan.pk) == [("h2", "2026-12", "2027-01"), ("h3", "2027-02", "2027-03")]
    assert "h2" in offered(mona, er, huda.pk, "2027-02") and "h2" not in offered(mona, er, huda.pk, "2027-01")
    assert told("h1")[-1] == "From February 2027 your preceptor is Badr Omar. You work beside them on their day shifts."
    assert told("h3") == ["From February 2027 you precept Layan Alharbi, a nurse on probation, who works beside you on your day "
                          "shifts."]
    assert told("h2")[-1] == "From February 2027 you no longer precept Layan Alharbi."
    assert len(told("h2")) == 2


def test_a_later_choice_replaces_one_waiting_from_that_month_or_later(mona, er, layan, amal, cno, precepting):
    badr = nurse(cno, er, "h3", "Badr Omar")
    holds(badr, precepting)
    assigned(mona, er, layan.pk, amal, "2027-02")

    assigned(mona, er, layan.pk, badr, "2027-01")

    assert preceptors(mona, er, layan.pk) == [("h3", "2027-01", "2027-03")]
    assert PreceptorAssignment.objects.filter(nurse=layan).count() == 1


def test_a_manager_with_a_nurse_profile_may_be_the_preceptor(mona, er, layan, cno, precepting):
    response = post(cno, f"{NURSES}/managers", {"user_id": str(User.objects.get(username="mona").pk), "unit_id": str(er.pk),
                                                 "gender": "F", "start_date": "2025-03-01"})
    assert response.status_code == 201, response.content
    holds(NurseProfile.objects.get(user__username="mona"), precepting)

    assert "mona" in offered(mona, er, layan.pk, "2026-12")
    assigned(mona, er, layan.pk, NurseProfile.objects.get(user__username="mona"), "2026-12")
    assert preceptors(mona, er, layan.pk) == [("mona", "2026-12", "2027-03")]


# ---- the list and each refusal ----------------------------------------------------------------------------------------------
def test_the_list_offers_only_nurses_who_may_precept_her(mona, er, icu, layan, amal, cno, precepting, today):
    retired = Skill.objects.create(name="Old course", expires=False, required_to_work=False, lets_a_nurse_precept=True,
                                   active=False)
    unmarked = Skill.objects.create(name="ICU", expires=False, required_to_work=False)
    holds(nurse(cno, er, "h3", "Badr Omar"), unmarked, None)  # no marked skill
    holds(nurse(cno, er, "h4", "Dana Ali"), precepting, dt.date(2026, 11, 10))  # expired yesterday
    holds(nurse(cno, er, "h5", "Eman Noor"), precepting, dt.date(2026, 11, 11))  # counts until today
    holds(nurse(cno, er, "h6", "Fadi Karim"), retired, None)  # a retired skill is tracked no more
    holds(nurse(cno, icu, "h7", "Ghada Sami"), precepting)  # another unit
    holds(nurse(cno, er, "h8", "Hind Zaki", start="2026-10-01", on_probation=True), precepting)  # on probation to 31 Dec
    holds(nurse(cno, er, "h9", "Iman Fawzi"), precepting)
    sara = nurse(cno, er, "h10", "Sara Adel", start="2026-11-01", on_probation=True)
    assigned(mona, er, sara.pk, NurseProfile.objects.get(user__username="h9"), "2026-11")  # precepts Sara to January

    assert offered(mona, er, layan.pk, "2026-12") == ["h2", "h5"]
    assert offered(mona, er, layan.pk, "2027-01") == ["h2", "h5", "h8"]  # Hind's probation reaches December only…
    assert offered(mona, er, layan.pk, "2027-02") == ["h2", "h5", "h8", "h9"]  # …and Iman precepts Sara to January.


@pytest.mark.parametrize(("who", "said"), [
    ("h3", "Badr Omar holds no skill that lets a nurse precept, unexpired today."),
    ("h7", "Ghada Sami is not a nurse of ER in December 2026."),
    ("h8", "Hind Zaki is on probation in December 2026, so she cannot precept."),
    ("h9", "Iman Fawzi already precepts Sara Adel in December 2026, and a preceptor has one nurse on probation at a time."),
    ("h1", "Layan Alharbi cannot precept herself."),
])
def test_each_refusal_is_said(mona, er, icu, layan, cno, precepting, who, said):
    nurse(cno, er, "h3", "Badr Omar")
    holds(nurse(cno, icu, "h7", "Ghada Sami"), precepting)
    holds(nurse(cno, er, "h8", "Hind Zaki", start="2026-10-01", on_probation=True), precepting)
    holds(nurse(cno, er, "h9", "Iman Fawzi"), precepting)
    sara = nurse(cno, er, "h10", "Sara Adel", start="2026-11-01", on_probation=True)
    assigned(mona, er, sara.pk, NurseProfile.objects.get(user__username="h9"), "2026-11")
    holds(layan, precepting)

    assert refusal(assign(mona, er, layan.pk, NurseProfile.objects.get(user__username=who), "2026-12")) == said


def test_a_month_whose_roster_is_approved_is_refused_and_this_month_only_while_it_is_not(mona, er, amal, cno, today):
    sara = nurse(cno, er, "h10", "Sara Adel", start="2026-11-01", on_probation=True)
    assert assign(mona, er, sara.pk, amal, "2026-11").status_code == 200
    approve(er, dt.date(2026, 11, 1))

    said = refusal(assign(mona, er, sara.pk, amal, "2026-11"))

    assert said == ("The roster of November 2026 is approved, so a preceptor can be chosen from a later month only. To change who "
                    "works beside her in November 2026, edit the roster.")
    assert row(mona, er, sara.pk)["probation"]["preceptor_months"] == ["2026-12", "2027-01"]
    assert choices(mona, er, sara.pk, "2026-11")["refusal"] == said


def test_a_month_outside_her_probation_or_over_is_refused(mona, er, layan, amal):
    assert refusal(assign(mona, er, layan.pk, amal, "2027-04")) == ("Layan Alharbi is not on probation in April 2027: her "
                                                                    "probation ends on 31 March 2027.")
    assert refusal(assign(mona, er, layan.pk, amal, "2026-10")) == ("October 2026 is over. A preceptor is chosen from this month "
                                                                    "or later.")


def test_a_nurse_not_on_probation_has_no_preceptor_to_choose(mona, er, amal, cno):
    badr = nurse(cno, er, "h3", "Badr Omar")

    assert refusal(assign(mona, er, badr.pk, amal, "2026-12")) == "Badr Omar is not on probation."
    assert row(mona, er, badr.pk)["probation"] is None


def test_only_her_units_manager_assigns_and_the_cno_reads_it(mona, omar, er, icu, layan, amal, cno):
    assert assign(omar, icu, layan.pk, amal, "2026-12").status_code == 403
    assigned(mona, er, layan.pk, amal, "2026-12")

    assert preceptors(cno, er, layan.pk) == [("h2", "2026-12", "2027-03")]
    assert cno.put(f"/api/units/{er.pk}/nurses/{layan.pk}/preceptor", {"preceptor_id": str(amal.pk), "from_month": "2026-12"},
                   content_type="application/json").status_code == 403


# ---- endings ----------------------------------------------------------------------------------------------------------------
def test_it_ends_with_the_probation_and_an_extension_takes_it_on(mona, er, layan, amal, today):
    assigned(mona, er, layan.pk, amal, "2026-12")
    assert post(mona, f"/api/units/{er.pk}/nurses/{layan.pk}/probation/extend", {}).status_code == 200
    assert preceptors(mona, er, layan.pk) == [("h2", "2026-12", "2027-04")]

    today(2027, 1, 20)
    assert post(mona, f"/api/units/{er.pk}/nurses/{layan.pk}/probation/end", {}).status_code == 200

    assert preceptors(mona, er, layan.pk) == [("h2", "2027-01", "2027-01")]


def test_an_extension_that_would_give_her_preceptor_two_nurses_is_refused(mona, er, layan, amal, cno):
    assigned(mona, er, layan.pk, amal, "2026-12")
    huda = nurse(cno, er, "h4", "Huda Saad", start="2027-04-01", on_probation=True)
    assigned(mona, er, huda.pk, amal, "2027-04")

    said = refusal(post(mona, f"/api/units/{er.pk}/nurses/{layan.pk}/probation/extend", {}))

    assert said == ("Extended, Layan Alharbi's probation would reach April 2027, when her preceptor Amal Hassan precepts Huda "
                    "Saad. Choose Layan Alharbi another preceptor from that month first.")


def test_the_preceptors_transfer_ends_it_with_her_last_month_and_tells_the_manager_once(mona, er, icu, layan, amal, cno):
    assigned(mona, er, layan.pk, amal, "2026-12")
    AuditEntry.objects.all().delete()

    response = post(cno, f"{NURSES}/{amal.pk}/transfer", {"unit_id": str(icu.pk), "from_month": "2027-02"})
    assert response.status_code == 200, response.content
    # A transfer to a third unit later moves nothing back.
    assert post(cno, f"{NURSES}/{amal.pk}/transfer", {"unit_id": str(er.pk), "from_month": "2027-02"}).status_code == 200

    assert preceptors(mona, er, layan.pk) == [("h2", "2026-12", "2027-01")]
    assert told("mona") == ["January 2027 is Amal Hassan's last month in ER, so Amal Hassan no longer precepts Layan Alharbi after "
                            "it. Choose another preceptor for Layan Alharbi."]
    entry = AuditEntry.objects.get(action="nurse.preceptor_ended")
    assert (entry.target, entry.actor.username) == ("h1", "chief")
    assert entry.before == {"preceptor": "h2", "from_month": "2026-12", "until": None}
    assert entry.after == {"preceptor": "h2", "from_month": "2026-12", "until": "2027-01"}


def test_the_preceptors_end_date_ends_it(mona, er, layan, amal, cno):
    assigned(mona, er, layan.pk, amal, "2026-12")

    assert cno.put(f"{NURSES}/{amal.pk}/end-date", {"end_date": "2027-02-14"}, content_type="application/json").status_code == 200

    assert preceptors(mona, er, layan.pk) == [("h2", "2026-12", "2027-02")]
    assert told("mona") == ["February 2027 is Amal Hassan's last month in ER, so Amal Hassan no longer precepts Layan Alharbi "
                            "after it. Choose another preceptor for Layan Alharbi."]


def test_her_own_transfer_ends_it_and_the_new_units_manager_chooses_there(mona, omar, er, icu, layan, amal, cno, precepting):
    assigned(mona, er, layan.pk, amal, "2026-12")
    karim = nurse(cno, icu, "h5", "Karim Saleh")
    holds(karim, precepting)

    assert post(cno, f"{NURSES}/{layan.pk}/transfer", {"unit_id": str(icu.pk), "from_month": "2027-02"}).status_code == 200

    assert preceptors(mona, er, layan.pk) == [("h2", "2026-12", "2027-01")]
    assert told("mona") == []  # Her own transfer is the CNO's: no preceptor left her.
    assert AuditEntry.objects.filter(action="nurse.preceptor_ended", target="h1").count() == 1
    assert refusal(assign(mona, er, layan.pk, amal, "2027-02")) == ("Layan Alharbi is not in ER in February 2027, so that unit's "
                                                                    "manager chooses her preceptor there.")
    assert offered(omar, icu, layan.pk, "2027-02") == ["h5"]
    assigned(omar, icu, layan.pk, karim, "2027-02")
    assert preceptors(omar, icu, layan.pk) == [("h2", "2026-12", "2027-01"), ("h5", "2027-02", "2027-03")]


def test_a_preceptors_skill_expiring_later_ends_nothing(mona, er, layan, amal, today):
    assigned(mona, er, layan.pk, amal, "2026-12")
    NurseSkill.objects.filter(nurse=amal).update(expiry=dt.date(2026, 12, 31))

    today(2027, 1, 5)

    assert preceptors(mona, er, layan.pk) == [("h2", "2027-01", "2027-03")]


# ---- the night-alone gap ------------------------------------------------------------------
def three_shifts(unit) -> None:
    unit.rules = unit.rules | {"shifts": ["D", "A", "N"],
                               "headcounts": [*unit.rules["headcounts"], {**unit.rules["headcounts"][0], "shift": "A"}]}
    unit.save()


def rules_body(unit, shifts) -> dict:
    rules = unit.rules | {"shifts": shifts, "headcounts": [row for row in unit.rules["headcounts"] if row["shift"] in shifts]}
    return {"rules": rules, "request_cap": 3, "preference_cap": 10, "default_time_limit": 60}


def test_a_unit_dropping_a_shift_that_would_leave_her_the_night_shift_alone_is_refused(mona, er, layan):
    three_shifts(er)
    assert mona.put(f"/api/units/{er.pk}/nurses/{layan.pk}/allowed-shifts", {"allowed_shifts": ["A", "N"]},
                    content_type="application/json").status_code == 200

    response = mona.put(f"/api/units/{er.pk}/rules", rules_body(er, ["D", "N"]), content_type="application/json")

    assert refusal(response) == ("Layan Alharbi (h1) would have only N, the night shift, of these shifts, and a nurse on probation "
                                 "works no night shift. Change their allowed shifts first.")
    er.refresh_from_db()
    assert er.rules["shifts"] == ["D", "A", "N"]


def test_a_transfer_that_would_carry_her_the_night_shift_alone_empties_her_list_to_be_looked_at(mona, omar, er, icu, layan, cno):
    three_shifts(er)
    assert mona.put(f"/api/units/{er.pk}/nurses/{layan.pk}/allowed-shifts", {"allowed_shifts": ["A", "N"]},
                    content_type="application/json").status_code == 200

    assert post(cno, f"{NURSES}/{layan.pk}/transfer", {"unit_id": str(icu.pk), "from_month": "2027-01"}).status_code == 200

    there = row(omar, icu, layan.pk)
    assert (there["allowed_shifts"], there["allowed_shifts_to_check"]) == (None, True)


# ---- edge cases ---------------------------------------------------------------------------------------------------
def test_the_same_preceptor_chosen_from_an_earlier_month_is_saved(mona, er, layan, amal):
    assigned(mona, er, layan.pk, amal, "2027-02")

    assigned(mona, er, layan.pk, amal, "2026-12")

    assert preceptors(mona, er, layan.pk) == [("h2", "2026-12", "2027-03")]
    assert told("h2")[1] == ("From December 2026 you precept Layan Alharbi, a nurse on probation, who works beside you on your "
                             "day shifts.")  # the months gained: December and January


def test_a_preceptor_already_hers_that_month_is_not_told_again(mona, er, layan, amal, cno, precepting):
    badr = nurse(cno, er, "h3", "Badr Omar")
    holds(badr, precepting)
    assigned(mona, er, layan.pk, amal, "2026-12")
    assigned(mona, er, layan.pk, badr, "2027-03")

    assigned(mona, er, layan.pk, amal, "2027-01")

    # Amal precepts her in January already: what changes is March, which is what each of the three is told.
    assert preceptors(mona, er, layan.pk) == [("h2", "2026-12", "2027-03")]
    assert told("h2")[1:] == ["From March 2027 you no longer precept Layan Alharbi.",
                              "From March 2027 you precept Layan Alharbi, a nurse on probation, who works beside you on your day "
                              "shifts."]
    assert told("h3")[-1] == "From March 2027 you no longer precept Layan Alharbi."
    assert told("h1")[-1] == "From March 2027 your preceptor is Amal Hassan. You work beside them on their day shifts."


def test_a_leaving_already_recorded_ends_it_when_assigned(mona, er, icu, layan, amal, cno):
    assert post(cno, f"{NURSES}/{amal.pk}/transfer", {"unit_id": str(icu.pk), "from_month": "2027-02"}).status_code == 200

    assigned(mona, er, layan.pk, amal, "2026-12")

    assert preceptors(mona, er, layan.pk) == [("h2", "2026-12", "2027-01")]


def test_an_end_tells_the_preceptor_she_precepts_her_no_more(mona, er, layan, amal, today):
    assigned(mona, er, layan.pk, amal, "2026-12")
    today(2027, 1, 20)

    assert post(mona, f"/api/units/{er.pk}/nurses/{layan.pk}/probation/end", {}).status_code == 200

    assert told("h2")[-1] == "From February 2027 you no longer precept Layan Alharbi."


def test_a_transfer_after_her_probation_keeps_a_night_shift_list(mona, omar, er, icu, layan, cno):
    three_shifts(er)
    assert mona.put(f"/api/units/{er.pk}/nurses/{layan.pk}/allowed-shifts", {"allowed_shifts": ["A", "N"]},
                    content_type="application/json").status_code == 200

    assert post(cno, f"{NURSES}/{layan.pk}/transfer", {"unit_id": str(icu.pk), "from_month": "2027-04"}).status_code == 200

    there = row(omar, icu, layan.pk)
    assert (there["allowed_shifts"], there["allowed_shifts_to_check"]) == (["N"], False)


# ---- a transfer with a preceptor in each unit, and other edge cases -------------------------------------------------------
@pytest.fixture
def due_in_icu(mona, omar, er, icu, layan, amal, cno, precepting):
    """Layan transfers to ICU from February, where Karim is to precept her; Amal precepts her in ER until then."""
    karim = nurse(cno, icu, "h5", "Karim Saleh")
    holds(karim, precepting)
    assert post(cno, f"{NURSES}/{layan.pk}/transfer", {"unit_id": str(icu.pk), "from_month": "2027-02"}).status_code == 200
    assigned(omar, icu, layan.pk, karim, "2027-02")
    assigned(mona, er, layan.pk, amal, "2026-12")
    return karim


def test_a_choice_in_her_unit_leaves_the_unit_she_joins_its_own(mona, er, layan, due_in_icu):
    assert preceptors(mona, er, layan.pk) == [("h2", "2026-12", "2027-01"), ("h5", "2027-02", "2027-03")]
    assert told("h5")[-1].startswith("From February 2027 you precept")  # and never told of a release


def test_choosing_again_a_preceptor_whose_months_end_with_a_leaving_changes_nothing(mona, er, layan, due_in_icu):
    AuditEntry.objects.all().delete()
    notes = Notification.objects.count()

    assigned(mona, er, layan.pk, NurseProfile.objects.get(user__username="h2"), "2027-01")

    assert not AuditEntry.objects.exists() and Notification.objects.count() == notes


def test_her_own_end_date_ends_it_at_assigning(mona, er, layan, amal, cno):
    assert cno.put(f"{NURSES}/{layan.pk}/end-date", {"end_date": "2027-01-20"}, content_type="application/json").status_code == 200

    assigned(mona, er, layan.pk, amal, "2026-12")

    assert preceptors(mona, er, layan.pk) == [("h2", "2026-12", "2027-01")]


def test_an_end_tells_each_preceptor_under_their_own_unit(mona, er, icu, layan, due_in_icu, today):
    today(2027, 1, 5)

    assert post(mona, f"/api/units/{er.pk}/nurses/{layan.pk}/probation/end", {}).status_code == 200

    released = Notification.objects.filter(user__username="h5", payload__role="released").get()
    assert (released.payload["unit_id"], released.payload["month"]) == (str(icu.pk), "2027-02")
    assert told("h5")[-1] == "From February 2027 you no longer precept Layan Alharbi."


def test_a_choice_that_replaces_a_plan_logs_the_plan(mona, er, layan, amal, cno, precepting):
    badr = nurse(cno, er, "h3", "Badr Omar")
    holds(badr, precepting)
    assigned(mona, er, layan.pk, badr, "2027-02")
    AuditEntry.objects.all().delete()

    assigned(mona, er, layan.pk, amal, "2027-01")

    entry = AuditEntry.objects.get()
    assert entry.before == {"preceptor": None, "replaced": [{"preceptor": "h3", "from_month": "2027-02", "until": None}]}


def test_an_untick_ends_it_for_good_so_a_later_tick_brings_no_preceptor_back(mona, er, layan, amal, cno):
    assigned(mona, er, layan.pk, amal, "2026-12")
    details = {"gender": "F", "start_date": "2026-12-15"}

    assert cno.put(f"{NURSES}/{layan.pk}/details", details | {"on_probation": False}, content_type="application/json").status_code == 200
    assert AuditEntry.objects.filter(action="nurse.preceptor_ended", target="h1").exists()
    assert told("h2")[-1] == "From December 2026 you no longer precept Layan Alharbi."
    assert cno.put(f"{NURSES}/{layan.pk}/details", details | {"on_probation": True}, content_type="application/json").status_code == 200

    assert preceptors(mona, er, layan.pk) == []
