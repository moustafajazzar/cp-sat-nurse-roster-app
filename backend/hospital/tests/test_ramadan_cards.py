"""The Ramadan cards: a whole Ramadan read from the months as they stand, and confirmed in one go."""
from datetime import date, timedelta

import pytest
from nurse_roster.calendar import month_info

from accounts.models import Role
from audit.models import AuditEntry
from hospital.models import FixedRamadanMonth

pytestmark = pytest.mark.django_db

CARDS = "/api/hospital/ramadans"
MONTHS = "/api/hospital/ramadan"
# Umm al-Qura: Ramadan 1448 runs from 8 February to 8 March 2027 (29 days), 1451 from 5 January to 3 February 2030,
# and 1452 from 26 December 2030 to 23 January 2031.


def cards(client, year: int) -> list[dict]:
    response = client.get(CARDS, {"year": year})
    assert response.status_code == 200, response.content
    return response.json()


def card(client, year: int, hijri_year: int) -> dict:
    return next(row for row in cards(client, year) if row["hijri_year"] == hijri_year)


def confirm(client, hijri_year: int, first: str, last: str, **extra):
    return client.put(f"{CARDS}/{hijri_year}", {"first": first, "last": last, **extra}, content_type="application/json")


def months(client, year: int) -> dict[str, dict]:
    return {row["month"]: row for row in client.get(MONTHS, {"year": year}).json()}


def run(first: str, last: str) -> list[str]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def refused_fields(response) -> set[tuple]:
    assert response.status_code == 422, response.content
    return {tuple(error["loc"][2:]) for error in response.json()["detail"]}


CALENDAR_1448 = {"hijri_year": 1448, "first": "2027-02-08", "last": "2027-03-08", "days": 29, "source": "derived",
                 "calendar_first": "2027-02-08", "calendar_last": "2027-03-08"}


# ---- the cards of a year -------------------------------------------------------------------------
def test_a_year_with_one_ramadan_shows_it_as_the_calendar_gives_it(signed_in):
    assert cards(signed_in(Role.CNO), 2027) == [CALENDAR_1448]


def test_a_year_with_two_lists_both_in_date_order_and_one_across_new_year_on_both_years(signed_in):
    client = signed_in(Role.CNO)

    in_2030 = cards(client, 2030)
    in_2031 = cards(client, 2031)

    assert [(row["hijri_year"], row["first"], row["last"], row["days"]) for row in in_2030] == [
        (1451, "2030-01-05", "2030-02-03", 30), (1452, "2030-12-26", "2031-01-23", 29)]
    assert in_2031[0] == in_2030[1]
    assert [row["hijri_year"] for row in in_2031] == [1452, 1453]


def test_every_year_the_calendar_covers_holds_one_or_two_and_never_none(signed_in):
    """A Hijri year is eleven days shorter than a Gregorian one, so no Gregorian year falls between two Ramadans."""
    client = signed_in(Role.CNO)

    counts = {len(cards(client, year)) for year in range(1925, 2077)}

    assert counts == {1, 2}


@pytest.mark.parametrize("year", [1925, 2026, 2030, 2031, 2076])
def test_the_calendar_days_on_a_card_are_the_solvers_month_by_month(signed_in, year):
    """The month derivation is the solver's own: each card's calendar days are its months' Ramadan days."""
    client = signed_in(Role.CNO)
    solver = set()
    for number in range(1, 13):
        info = month_info(year, number)
        solver |= {info.iso(day) for day in info.ramadan_days}

    days = set()
    for row in cards(client, year):
        days |= {day for day in run(row["calendar_first"], row["calendar_last"]) if day.startswith(f"{year}-")}

    assert days == solver


def test_the_card_reads_the_months_as_they_stand(signed_in):
    client = signed_in(Role.CNO)
    client.put(f"{MONTHS}/2027-03", {"first": "2027-03-01", "last": "2027-03-09"}, content_type="application/json")

    assert card(client, 2027, 1448) == {**CALENDAR_1448, "last": "2027-03-09", "days": 30, "source": "fixed"}


def test_a_neighbouring_month_holding_fixed_days_continues_the_card(signed_in):
    client = signed_in(Role.CNO)
    client.put(f"{MONTHS}/2027-01", {"dates": ["2027-01-31"]}, content_type="application/json")

    assert card(client, 2027, 1448) == {**CALENDAR_1448, "first": "2027-01-31", "days": 30, "source": "fixed"}


def test_fixed_days_in_a_neighbouring_month_that_do_not_reach_the_edge_are_not_this_ramadans(signed_in):
    client = signed_in(Role.CNO)
    client.put(f"{MONTHS}/2027-01", {"dates": ["2027-01-30"]}, content_type="application/json")
    client.put(f"{MONTHS}/2027-04", {"dates": ["2027-04-02"]}, content_type="application/json")

    assert card(client, 2027, 1448) == CALENDAR_1448


def test_a_ramadan_whose_months_are_fixed_to_none_has_no_first_or_last_day(signed_in):
    client = signed_in(Role.CNO)
    for month in ("2027-02", "2027-03"):
        client.put(f"{MONTHS}/{month}", {"dates": []}, content_type="application/json")

    assert card(client, 2027, 1448) == {**CALENDAR_1448, "first": None, "last": None, "days": 0, "source": "fixed"}


def test_a_list_with_a_gap_counts_its_days_between_the_first_and_the_last(signed_in):
    client = signed_in(Role.CNO)
    client.put(f"{MONTHS}/2027-03", {"dates": ["2027-03-01", "2027-03-08"]}, content_type="application/json")

    assert card(client, 2027, 1448) == {**CALENDAR_1448, "days": 21 + 2, "source": "fixed"}


@pytest.mark.parametrize("value", [1924, 2077, "soon"])
def test_a_year_outside_the_calendar_is_refused(signed_in, value):
    assert signed_in(Role.CNO).get(CARDS, {"year": value}).status_code == 422


# ---- confirming ------------------------------------------------------------------------------------
def test_confirming_the_calendars_own_dates_fixes_every_month_it_touches(signed_in):
    client = signed_in(Role.CNO)

    response = confirm(client, 1448, "2027-02-08", "2027-03-08")

    assert response.status_code == 200, response.content
    assert response.json() == {**CALENDAR_1448, "source": "fixed"}
    rows = dict(FixedRamadanMonth.objects.values_list("month", "dates"))
    assert {month: [day.isoformat() for day in days] for month, days in rows.items()} == {
        "2027-02": run("2027-02-08", "2027-02-28"), "2027-03": run("2027-03-01", "2027-03-08")}
    assert months(client, 2027)["2027-02"]["source"] == "fixed"


def test_a_shifted_confirmation_writes_each_months_share_as_fixing_it_by_hand_would(signed_in):
    client = signed_in(Role.CNO)

    response = confirm(client, 1448, "2027-02-09", "2027-03-09")

    assert response.json() == {**CALENDAR_1448, "first": "2027-02-09", "last": "2027-03-09", "source": "fixed"}
    by_month = months(client, 2027)
    assert by_month["2027-02"]["dates"] == run("2027-02-09", "2027-02-28")
    assert by_month["2027-03"]["dates"] == run("2027-03-01", "2027-03-09")
    assert by_month["2027-03"]["derived_dates"] == run("2027-03-01", "2027-03-08")
    assert by_month["2027-01"]["source"] == "derived"


def test_a_day_moved_into_a_month_the_calendar_gave_none_is_fixed_there(signed_in):
    client = signed_in(Role.CNO)

    confirm(client, 1448, "2027-02-07", "2027-03-07")

    assert months(client, 2027)["2027-01"]["source"] == "derived"
    confirm(client, 1448, "2027-01-31", "2027-03-01")
    assert months(client, 2027)["2027-01"] == {"month": "2027-01", "source": "fixed", "dates": ["2027-01-31"],
                                                  "derived_dates": []}


def test_a_month_the_confirmed_range_leaves_is_fixed_to_none(signed_in):
    client = signed_in(Role.CNO)

    response = confirm(client, 1448, "2027-01-31", "2027-02-28")

    assert response.json()["first"] == "2027-01-31" and response.json()["last"] == "2027-02-28"
    march = months(client, 2027)["2027-03"]
    assert march["source"] == "fixed" and march["dates"] == []


def test_a_confirmation_across_new_year_writes_both_years(signed_in):
    client = signed_in(Role.CNO)

    confirm(client, 1452, "2030-12-27", "2031-01-24")

    assert months(client, 2030)["2030-12"]["dates"] == run("2030-12-27", "2030-12-31")
    assert months(client, 2031)["2031-01"]["dates"] == run("2031-01-01", "2031-01-24")
    assert card(client, 2031, 1452) == card(client, 2030, 1452)


def test_confirming_again_overwrites_the_earlier_confirmation_in_every_month_touched(signed_in):
    client = signed_in(Role.CNO)
    confirm(client, 1448, "2027-01-31", "2027-03-01")

    response = confirm(client, 1448, "2027-02-09", "2027-03-09")

    assert response.json()["first"] == "2027-02-09" and response.json()["days"] == 29
    by_month = months(client, 2027)
    assert by_month["2027-01"] == {"month": "2027-01", "source": "fixed", "dates": [], "derived_dates": []}
    assert by_month["2027-03"]["dates"] == run("2027-03-01", "2027-03-09")


def test_one_audit_entry_per_month_whose_fixed_dates_change_and_none_for_one_unchanged(signed_in):
    client = signed_in(Role.CNO, username="the-cno")
    client.put(f"{MONTHS}/2027-02", {"first": "2027-02-09", "last": "2027-02-28"}, content_type="application/json")
    AuditEntry.objects.all().delete()

    confirm(client, 1448, "2027-02-09", "2027-03-09")
    confirm(client, 1448, "2027-02-09", "2027-03-09")

    (march,) = AuditEntry.objects.all()
    assert march.action == "ramadan_dates.changed" and march.target == "2027-03"
    assert march.actor.username == "the-cno"
    assert march.before == {"source": "derived", "dates": run("2027-03-01", "2027-03-08")}
    assert march.after == {"source": "fixed", "dates": run("2027-03-01", "2027-03-09")}


def test_the_card_months_and_the_months_table_agree_after_any_confirmation(signed_in):
    client = signed_in(Role.CNO)
    confirm(client, 1448, "2027-02-10", "2027-03-10")
    days = [day for row in months(client, 2027).values() for day in row["dates"]]

    after = card(client, 2027, 1448)

    assert (after["first"], after["last"], after["days"]) == (days[0], days[-1], len(days))


@pytest.mark.parametrize("first, last, field", [
    ("2027-03-08", "2027-02-08", ("last",)),            # backwards
    ("2027-02-08", "2027-03-15", ("last",)),            # 36 days
    ("1924-12-31", "2027-03-08", ("first",)),           # before the calendar
    ("2027-02-08", "2077-01-01", ("last",)),            # after it
    ("2027-02-30", "2027-03-08", ("first",)),
    ("tomorrow", "2027-03-08", ("first",)),
])
def test_a_refusal_names_the_field_and_writes_nothing(signed_in, first, last, field):
    client = signed_in(Role.CNO)

    assert refused_fields(confirm(client, 1448, first, last)) == {field}
    assert not FixedRamadanMonth.objects.exists()
    assert not AuditEntry.objects.filter(action="ramadan_dates.changed").exists()


@pytest.mark.parametrize("first, last", [("2026-02-18", "2026-03-19"), ("2027-03-09", "2027-04-06"),
                                         ("2027-01-10", "2027-02-07")])
def test_days_that_do_not_touch_the_calendars_ramadan_are_refused_as_a_wrong_year_or_month(signed_in, first, last):
    # 2026's are Ramadan 1447's: confirming them as 1448 would overwrite 1447's months and empty 1448's.
    response = confirm(signed_in(Role.CNO), 1448, first, last)

    assert refused_fields(response) == {("first",)}
    assert response.json()["detail"][0]["msg"].endswith(
        "These days do not touch Ramadan 1448, which the calendar puts at 8 February – 8 March 2027.")
    assert not FixedRamadanMonth.objects.exists()


def test_the_refusal_names_both_years_of_a_ramadan_across_new_year(signed_in):
    response = confirm(signed_in(Role.CNO), 1452, "2031-12-15", "2032-01-13")

    assert response.json()["detail"][0]["msg"].endswith("at 26 December 2030 – 23 January 2031.")


def test_a_ramadan_inside_one_month_confirmed_two_months_back_is_read_and_overwritten_whole(signed_in):
    # Umm al-Qura's Ramadan 1446 is 1 to 29 March 2025, inside one month: 35 days that touch it can begin in January.
    client = signed_in(Role.CNO)

    response = confirm(client, 1446, "2025-01-26", "2025-03-01")

    assert (response.json()["first"], response.json()["last"], response.json()["days"]) == ("2025-01-26", "2025-03-01", 35)
    assert card(client, 2025, 1446) == response.json()
    again = confirm(client, 1446, "2025-03-01", "2025-03-29").json()
    assert (again["first"], again["days"]) == ("2025-03-01", 29)
    assert months(client, 2025)["2025-01"]["dates"] == [] and months(client, 2025)["2025-02"]["dates"] == []


def test_one_day_of_overlap_is_enough(signed_in):
    response = confirm(signed_in(Role.CNO), 1448, "2027-03-08", "2027-04-05")

    assert response.status_code == 200 and response.json()["first"] == "2027-03-08"


def test_thirty_five_days_is_the_longest_a_confirmation_may_be(signed_in):
    response = confirm(signed_in(Role.CNO), 1448, "2027-02-08", "2027-03-14")

    assert response.status_code == 200 and response.json()["days"] == 35


def test_a_field_missing_or_one_too_many_is_refused(signed_in):
    client = signed_in(Role.CNO)

    assert refused_fields(client.put(f"{CARDS}/1448", {"first": "2027-02-08"}, content_type="application/json")) == {("last",)}
    assert refused_fields(confirm(client, 1448, "2027-02-08", "2027-03-08", dates=[])) == {("dates",)}


@pytest.mark.parametrize("hijri_year", [1342, 1500, 1501, 99999])
def test_a_ramadan_outside_the_calendar_is_not_found(signed_in, hijri_year):
    # 1500's Ramadan is in 2077, which the calendar's whole years do not reach.
    assert confirm(signed_in(Role.CNO), hijri_year, "2027-02-08", "2027-03-08").status_code == 404


# ---- who --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", [role for role in Role if role != Role.CNO])
def test_only_the_cno_reads_the_cards_and_confirms(signed_in, role):
    client = signed_in(role)

    assert client.get(CARDS, {"year": 2027}).status_code == 403
    assert confirm(client, 1448, "2027-02-08", "2027-03-08").status_code == 403
    assert not FixedRamadanMonth.objects.exists()
