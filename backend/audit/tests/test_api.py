from datetime import UTC, datetime

import pytest
from django.test import Client

from accounts.models import Role
from audit.log import record
from audit.models import AuditEntry
from conftest import post

pytestmark = pytest.mark.django_db

ENTRIES = "/api/audit/entries"


@pytest.fixture
def admin(signed_in):
    client = signed_in(Role.SYSTEM_ADMIN, username="admin")
    AuditEntry.objects.all().delete()  # the login of the fixture itself
    return client


def at(entry, *when):
    AuditEntry.objects.filter(pk=entry.pk).update(at=datetime(*when, tzinfo=UTC))
    return entry


def actions(client, **filters):
    r = client.get(ENTRIES, filters)
    assert r.status_code == 200, r.content
    return [entry["action"] for entry in r.json()["entries"]]


class TestEntries:
    def test_newest_first_with_every_field(self, admin, make_user):
        actor = make_user("the-cno", Role.CNO)
        record("first", actor=actor)
        client_entry = at(record("second", actor=actor, target="thing", before={"a": 1}, after={"a": 2}),
                          2026, 11, 5, 21, 30, 15)
        AuditEntry.objects.filter(pk=client_entry.pk).update(ip="10.1.2.3")

        body = admin.get(ENTRIES).json()

        assert [entry["action"] for entry in body["entries"]] == ["second", "first"]
        assert body["entries"][0] == {
            "id": client_entry.pk, "at": "2026-11-06T00:30:15+03:00", "actor": "the-cno", "action": "second",
            "target": "thing", "before": {"a": 1}, "after": {"a": 2}, "ip": "10.1.2.3",
        }
        assert body["older_than"] is None

    def test_the_system_has_no_actor(self, admin):
        record("user.created", target="admin")

        assert admin.get(ENTRIES).json()["entries"][0]["actor"] is None

    def test_a_failed_login_at_an_unknown_name_has_no_target_and_keeps_its_address(self, admin, make_user):
        make_user("fatimah", Role.CNO)
        for username in ("fatimah", "not-a-name"):
            Client().post("/api/auth/login", {"username": username, "password": "wrong-password"},
                          content_type="application/json", REMOTE_ADDR="10.1.2.3")

        unknown, real = admin.get(ENTRIES).json()["entries"]

        assert {key: unknown[key] for key in ("actor", "action", "target", "after", "ip")} == {
            "actor": None, "action": "auth.login_failed", "target": "",
            "after": {"reason": "wrong_credentials", "failures": 1}, "ip": "10.1.2.3"}
        assert (real["actor"], real["target"]) == ("fatimah", "fatimah")

    def test_the_real_actions_show_up(self, signed_in, make_user):
        client = signed_in(Role.SYSTEM_ADMIN, username="admin")
        post(client, "/api/users", {"username": "the-cno", "full_name": "The CNO", "role": "cno"})

        assert actions(client)[:2] == ["user.created", "auth.login"]


class TestFilters:
    def test_by_action(self, admin):
        record("auth.login")
        record("user.created")

        assert actions(admin, action="auth.login") == ["auth.login"]
        assert actions(admin, action="nothing.like.this") == []

    def test_by_user_finds_what_they_did_and_what_was_done_to_them(self, admin, make_user):
        fatimah = make_user("fatimah", Role.CNO)
        other = make_user("other", Role.UNIT_MANAGER)
        record("by-her", actor=fatimah, target="something")
        record("to-her", actor=other, target="fatimah")
        record("neither", actor=other, target="other")

        assert actions(admin, user="fatimah") == ["to-her", "by-her"]
        assert actions(admin, user="FATIMAH") == ["to-her", "by-her"]

    def test_by_a_username_nobody_has(self, admin):
        record("auth.login_failed", target="nobody")

        assert actions(admin, user="nobody") == ["auth.login_failed"]

    def test_a_part_of_a_username_is_not_that_user(self, admin, make_user):
        record("khalid", actor=make_user("khalid", Role.CNO))

        assert actions(admin, user="ali") == []

    def test_dates_are_inclusive_days_in_the_hospitals_time_zone(self, admin):
        # Asia/Riyadh is UTC+3, so the 5th there runs from 21:00 UTC on the 4th.
        at(record("before"), 2026, 11, 4, 20, 59, 59)
        at(record("first-moment"), 2026, 11, 4, 21, 0, 0)
        at(record("last-moment"), 2026, 11, 5, 20, 59, 59)
        at(record("after"), 2026, 11, 5, 21, 0, 0)

        assert actions(admin, date_from="2026-11-05", date_to="2026-11-05") == ["last-moment", "first-moment"]
        assert actions(admin, date_from="2026-11-05") == ["after", "last-moment", "first-moment"]
        assert actions(admin, date_to="2026-11-05") == ["last-moment", "first-moment", "before"]

    def test_filters_combine(self, admin, make_user):
        fatimah = make_user("fatimah", Role.CNO)
        at(record("wanted", actor=fatimah), 2026, 11, 5, 12)
        at(record("wanted", actor=fatimah), 2026, 11, 7, 12)
        at(record("other", actor=fatimah), 2026, 11, 5, 12)
        at(record("wanted"), 2026, 11, 5, 12)

        assert len(actions(admin, user="fatimah", action="wanted", date_from="2026-11-05", date_to="2026-11-05")) == 1

    def test_empty_filters_are_no_filters(self, admin):
        record("auth.login")

        assert actions(admin, user="", action="") == ["auth.login"]

    @pytest.mark.parametrize("params", [{"date_from": "not-a-date"}, {"date_to": "2026-13-01"}, {"user": "nul\x00byte"},
                                        {"action": "nul\x00byte"}, {"limit": 0}, {"limit": 201},
                                        {"older_than": "x"}])
    def test_nonsense_is_a_validation_error_not_a_crash(self, admin, params):
        assert admin.get(ENTRIES, params).status_code == 422


class TestPages:
    def test_older_than_walks_every_entry_once(self, admin):
        for number in range(5):
            record(str(number))

        first = admin.get(ENTRIES, {"limit": 2}).json()
        second = admin.get(ENTRIES, {"limit": 2, "older_than": first["older_than"]}).json()
        third = admin.get(ENTRIES, {"limit": 2, "older_than": second["older_than"]}).json()

        assert [[entry["action"] for entry in page["entries"]] for page in (first, second, third)] == [
            ["4", "3"], ["2", "1"], ["0"]]
        assert third["older_than"] is None

    def test_a_page_that_exactly_fills_is_the_last(self, admin):
        record("0")
        record("1")

        assert admin.get(ENTRIES, {"limit": 2}).json()["older_than"] is None

    def test_pages_keep_the_filters(self, admin):
        for number in range(3):
            record("wanted")
            record("other")

        first = admin.get(ENTRIES, {"limit": 2, "action": "wanted"}).json()
        second = admin.get(ENTRIES, {"limit": 2, "action": "wanted", "older_than": first["older_than"]}).json()

        assert [entry["action"] for entry in first["entries"] + second["entries"]] == ["wanted"] * 3


class TestOptions:
    def test_what_the_filters_can_choose_from(self, admin, make_user):
        make_user("fatimah", Role.CNO)
        record("user.created")
        record("auth.login")
        record("auth.login")

        r = admin.get("/api/audit/filter-options")

        assert r.status_code == 200
        assert r.json() == {"actions": ["auth.login", "user.created"], "users": ["admin", "fatimah"]}


def test_signed_out_cannot_read_it():
    assert Client().get(ENTRIES).status_code == 401
