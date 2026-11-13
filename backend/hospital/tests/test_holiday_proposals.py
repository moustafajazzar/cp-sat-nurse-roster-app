"""The holidays card: the Eids and the national days the calendar proposes for a year, and adding one."""
from datetime import date, timedelta

import pytest

from accounts.models import Role
from audit.models import AuditEntry
from hospital.models import FixedRamadanMonth, PublicHoliday

pytestmark = pytest.mark.django_db

PROPOSALS = "/api/hospital/holiday-proposals"
HOLIDAYS = "/api/hospital/public-holidays"
# Umm al-Qura. As Saudi Arabia announced them: Ramadan 1446 ran from 1 to 29 March 2025 (29 days) and Arafat Day was
# 5 June 2025; Ramadan 1447 from 18 February to 19 March 2026 (30 days), Arafat Day 26 May 2026. By the calendar's
# tables, not yet announced: Ramadan 1448 from 8 February to 8 March 2027 (29 days), Arafat Day 15 May 2027.


def proposals(client, year: int) -> list[dict]:
    response = client.get(PROPOSALS, {"year": year})
    assert response.status_code == 200, response.content
    return response.json()


def named(client, year: int, name: str) -> list[dict]:
    return [row for row in proposals(client, year) if row["name"] == name]


def add(client, year: int, name: str, first: str, length: int, **extra):
    return client.post(f"{PROPOSALS}?year={year}", {"name": name, "first": first, "length": length, **extra},
                       content_type="application/json")


def holidays(year: int) -> list[tuple[str, str]]:
    return [(row.date.isoformat(), row.name) for row in PublicHoliday.objects.filter(date__year=year).order_by("date")]


def run(first: str, last: str) -> list[date]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def fix(month: str, days: list[date]) -> None:
    FixedRamadanMonth.objects.update_or_create(month=month, defaults={"dates": days})


def refused_fields(response) -> set[tuple]:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"][2:]) for error in response.json()["detail"]}


# ---- the proposals of a year ---------------------------------------------------------------------
def test_a_year_proposes_the_four_in_date_order(signed_in):
    assert proposals(signed_in(Role.CNO), 2027) == [
        {"name": "Founding Day", "first": "2027-02-22", "length": 1},
        {"name": "Eid al-Fitr", "first": "2027-03-09", "length": 4},
        {"name": "Eid al-Adha", "first": "2027-05-15", "length": 4},
        {"name": "National Day", "first": "2027-09-23", "length": 1},
    ]


def test_eid_al_fitr_of_a_30_day_ramadan_starts_on_its_30th_day(signed_in):
    """The day after the 29th: 19 March 2026, the 30th day of Ramadan 1447, is the Eid's first day."""
    assert named(signed_in(Role.CNO), 2026, "Eid al-Fitr") == [{"name": "Eid al-Fitr", "first": "2026-03-19", "length": 4}]


def test_eid_al_adha_starts_on_arafat_day(signed_in):
    assert named(signed_in(Role.CNO), 2025, "Eid al-Adha") == [{"name": "Eid al-Adha", "first": "2025-06-05", "length": 4}]


def test_eid_al_fitr_follows_a_ramadan_the_cno_started_a_day_late(signed_in):
    fix("2027-02", run("2027-02-09", "2027-02-28"))
    fix("2027-03", run("2027-03-01", "2027-03-09"))

    assert named(signed_in(Role.CNO), 2027, "Eid al-Fitr")[0]["first"] == "2027-03-10"


def test_eid_al_fitr_of_a_ramadan_lengthened_to_30_days_still_follows_its_29th(signed_in):
    client = signed_in(Role.CNO)
    client.put("/api/hospital/ramadans/1448", {"first": "2027-02-07", "last": "2027-03-08"},
               content_type="application/json")

    assert named(client, 2027, "Eid al-Fitr")[0]["first"] == "2027-03-08"


def test_eid_al_fitr_of_a_ramadan_shortened_by_a_day_moves_with_it(signed_in):
    client = signed_in(Role.CNO)
    client.put("/api/hospital/ramadans/1447", {"first": "2026-02-19", "last": "2026-03-19"},
               content_type="application/json")

    assert named(client, 2026, "Eid al-Fitr")[0]["first"] == "2026-03-20"


def test_a_ramadan_with_fewer_than_29_days_in_force_proposes_no_eid_al_fitr(signed_in):
    client = signed_in(Role.CNO)
    client.put("/api/hospital/ramadans/1448", {"first": "2027-02-08", "last": "2027-03-07"},
               content_type="application/json")

    assert named(client, 2027, "Eid al-Fitr") == []
    assert [row["name"] for row in proposals(client, 2027)] == ["Founding Day", "Eid al-Adha", "National Day"]


def test_a_ramadan_fixed_to_none_proposes_no_eid_al_fitr(signed_in):
    fix("2027-02", [])
    fix("2027-03", [])

    assert named(signed_in(Role.CNO), 2027, "Eid al-Fitr") == []


def test_the_29th_day_is_counted_over_the_days_in_force_not_from_the_first(signed_in):
    """A list with a gap: 8 February plus 28 days is 8 March, but the 29th day in force is 9 March."""
    fix("2027-02", [day for day in run("2027-02-08", "2027-02-28") if day != date(2027, 2, 10)])
    fix("2027-03", run("2027-03-01", "2027-03-09"))

    assert named(signed_in(Role.CNO), 2027, "Eid al-Fitr")[0]["first"] == "2027-03-10"


def test_a_neighbouring_months_fixed_days_count_toward_the_29(signed_in):
    """Ramadan 1446 is inside March 2025 by the calendar; one confirmed from 28 February ends a day earlier."""
    client = signed_in(Role.CNO)
    assert named(client, 2025, "Eid al-Fitr")[0]["first"] == "2025-03-30"
    client.put("/api/hospital/ramadans/1446", {"first": "2025-02-28", "last": "2025-03-28"},
               content_type="application/json")

    assert named(client, 2025, "Eid al-Fitr")[0]["first"] == "2025-03-29"


def test_an_eid_after_a_ramadan_across_new_year_is_proposed_on_its_own_year(signed_in):
    """Ramadan 1452 runs from 26 December 2030 to 23 January 2031."""
    client = signed_in(Role.CNO)

    assert named(client, 2031, "Eid al-Fitr") == [{"name": "Eid al-Fitr", "first": "2031-01-24", "length": 4}]
    assert all(row["first"].startswith("2030-") for row in proposals(client, 2030))


def test_an_eid_on_new_years_day_after_a_december_ramadan_is_proposed_on_the_new_year(signed_in):
    """Ramadan 1387 ran from 2 to 30 December 1967 by the calendar; started a day late, its 29th day is 31 December."""
    fix("1967-12", run("1967-12-03", "1967-12-31"))
    client = signed_in(Role.CNO)

    assert named(client, 1968, "Eid al-Fitr")[0]["first"] == "1968-01-01"
    # 1967's own Eid al-Fitr, of Ramadan 1386, is in January.
    assert [row["first"][:7] for row in named(client, 1967, "Eid al-Fitr")] == ["1967-01"]


def test_a_year_can_hold_two_of_one_eid(signed_in):
    """By the Umm al-Qura tables, which no announcement has reached yet: Ramadan 1454 begins on 4 December 2032 and 1455
    on 23 November 2033; Arafat Day falls in January and December 2039."""
    client = signed_in(Role.CNO)

    assert [row["first"] for row in named(client, 2033, "Eid al-Fitr")] == ["2033-01-02", "2033-12-22"]
    assert len(named(client, 2039, "Eid al-Adha")) == 2


def test_every_year_the_calendar_covers_answers(signed_in):
    client = signed_in(Role.CNO)

    for year in range(1925, 2077):
        assert {row["name"] for row in proposals(client, year)} >= {"Founding Day", "National Day"}, year


def test_a_proposal_is_gone_once_its_first_day_is_a_holiday_whatever_its_name(signed_in):
    PublicHoliday.objects.create(date=date(2027, 3, 9), name="Eid")
    PublicHoliday.objects.create(date=date(2027, 5, 16), name="Second day of the Eid")

    names = [row["name"] for row in proposals(signed_in(Role.CNO), 2027)]

    assert names == ["Founding Day", "Eid al-Adha", "National Day"]


def test_a_year_with_every_proposal_entered_answers_none(signed_in):
    for day in ["2027-02-22", "2027-03-09", "2027-05-15", "2027-09-23"]:
        PublicHoliday.objects.create(date=date.fromisoformat(day), name="Entered")

    assert proposals(signed_in(Role.CNO), 2027) == []


# ---- adding --------------------------------------------------------------------------------------
def test_adding_creates_one_holiday_a_day_under_the_name_and_the_line_goes(signed_in):
    client = signed_in(Role.CNO)

    response = add(client, 2027, "Eid al-Fitr", "2027-03-09", 4)

    assert response.status_code == 200, response.content
    assert response.json() == {"added": ["2027-03-09", "2027-03-10", "2027-03-11", "2027-03-12"], "skipped": []}
    assert holidays(2027) == [(day, "Eid al-Fitr") for day in ["2027-03-09", "2027-03-10", "2027-03-11", "2027-03-12"]]
    assert named(client, 2027, "Eid al-Fitr") == []


def test_adding_skips_the_dates_already_holidays(signed_in):
    PublicHoliday.objects.create(date=date(2027, 3, 10), name="Entered by hand")
    client = signed_in(Role.CNO)

    response = add(client, 2027, "Eid al-Fitr", "2027-03-09", 4)

    assert response.json() == {"added": ["2027-03-09", "2027-03-11", "2027-03-12"], "skipped": ["2027-03-10"]}
    assert ("2027-03-10", "Entered by hand") in holidays(2027)


def test_adding_with_every_day_entered_adds_none(signed_in):
    PublicHoliday.objects.create(date=date(2027, 9, 23), name="National Day")

    response = add(signed_in(Role.CNO), 2027, "National Day", "2027-09-23", 1)

    assert response.json() == {"added": [], "skipped": ["2027-09-23"]}


def test_a_first_day_and_a_length_the_cno_changed_are_what_is_added(signed_in):
    """A circular that gives the Eid a day later and six days."""
    client = signed_in(Role.CNO)

    added = add(client, 2027, "Eid al-Adha", "2027-05-16", 6).json()["added"]

    assert added == [day.isoformat() for day in run("2027-05-16", "2027-05-21")]
    assert named(client, 2027, "Eid al-Adha") == [{"name": "Eid al-Adha", "first": "2027-05-15", "length": 4}]


def test_an_eid_running_into_january_adds_its_later_days_in_the_next_year(signed_in):
    """Arafat Day 1427 was 30 December 2006."""
    client = signed_in(Role.CNO)
    assert {"name": "Eid al-Adha", "first": "2006-12-30", "length": 4} in proposals(client, 2006)

    add(client, 2006, "Eid al-Adha", "2006-12-30", 4)

    assert holidays(2006)[-2:] == [("2006-12-30", "Eid al-Adha"), ("2006-12-31", "Eid al-Adha")]
    assert holidays(2007)[:2] == [("2007-01-01", "Eid al-Adha"), ("2007-01-02", "Eid al-Adha")]


def test_each_holiday_added_is_audited_as_a_holiday_created(signed_in):
    PublicHoliday.objects.create(date=date(2027, 3, 10), name="Entered by hand")
    client = signed_in(Role.CNO, username="the-cno")
    AuditEntry.objects.all().delete()

    add(client, 2027, "Eid al-Fitr", "2027-03-09", 3)

    entries = AuditEntry.objects.order_by("target")
    assert [(entry.action, entry.target, entry.after) for entry in entries] == [
        ("public_holiday.created", "2027-03-09", {"date": "2027-03-09", "name": "Eid al-Fitr"}),
        ("public_holiday.created", "2027-03-11", {"date": "2027-03-11", "name": "Eid al-Fitr"}),
    ]
    assert {entry.actor.username for entry in entries} == {"the-cno"}


def test_holidays_added_read_like_any_other(signed_in):
    client = signed_in(Role.CNO)
    add(client, 2027, "National Day", "2027-09-23", 1)

    assert [(row["date"], row["name"]) for row in client.get(HOLIDAYS, {"year": 2027}).json()] == [
        ("2027-09-23", "National Day")]


@pytest.mark.parametrize("changes, field", [
    ({"first": "2028-03-09"}, "first"),
    ({"first": "2026-12-31"}, "first"),
    ({"first": "2027-02-30"}, "first"),
    ({"length": 0}, "length"),
    ({"length": 15}, "length"),
    ({"name": "   "}, "name"),
    ({"name": "x" * 101}, "name"),
    ({"extra": 1}, "extra"),
])
def test_a_refusal_names_the_field_and_adds_nothing(signed_in, changes, field):
    body = {"name": "Eid al-Fitr", "first": "2027-03-09", "length": 4} | changes

    response = add(signed_in(Role.CNO), 2027, **body)

    assert refused_fields(response) == {(field,)}
    assert holidays(2027) == [] and holidays(2028) == []


def test_days_past_the_last_year_the_calendar_covers_are_refused(signed_in):
    response = add(signed_in(Role.CNO), 2076, "Late", "2076-12-30", 4)

    assert refused_fields(response) == {("length",)}
    assert holidays(2076) == []


@pytest.mark.parametrize("year", [1924, 2077, "soon"])
def test_a_year_outside_the_calendar_is_refused(signed_in, year):
    client = signed_in(Role.CNO)

    assert client.get(PROPOSALS, {"year": year}).status_code == 422
    assert add(client, year, "National Day", "2027-09-23", 1).status_code == 422


@pytest.mark.parametrize("role", [role for role in Role if role != Role.CNO])
def test_only_the_cno_reads_and_adds_them(signed_in, role):
    client = signed_in(role)

    assert client.get(PROPOSALS, {"year": 2027}).status_code == 403
    assert add(client, 2027, "National Day", "2027-09-23", 1).status_code == 403
    assert holidays(2027) == []
