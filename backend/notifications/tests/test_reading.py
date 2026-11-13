"""Reading one's own notifications: the count the bell polls, the list, marking read. Nothing here can read another's."""
import datetime as dt

import pytest
from django.contrib.sessions.models import Session
from django.utils import timezone

from accounts.models import Role, User
from conftest import post
from notifications.models import Event, Notification
from notifications.sending import notify
from nurses.tests.conftest import make_unit

pytestmark = pytest.mark.django_db


@pytest.fixture
def mona(client_of):
    return client_of("mona", Role.UNIT_MANAGER, full_name="Mona Ali")


@pytest.fixture
def er(mona):
    return make_unit("ER", User.objects.get(username="mona"))


def tell(username, unit, event=Event.ROSTER_REJECTED, **payload) -> Notification:
    by = User.objects.get(username="mona")
    notify([User.objects.get(username=username)], event, unit_id=str(unit.pk), month="2026-06", by=str(by.pk), **payload)
    return Notification.objects.filter(user__username=username).first()


def items(client) -> list[dict]:
    response = client.get("/api/notifications")
    assert response.status_code == 200, response.content
    return response.json()["items"]


def unread(client) -> int:
    return client.get("/api/notifications/unread").json()["unread"]


class TestMarkingRead:
    def test_one_is_marked_read_and_the_count_falls(self, mona, er):
        first, second = tell("mona", er), tell("mona", er)
        assert unread(mona) == 2

        assert post(mona, f"/api/notifications/{first.pk}/read", {}).status_code == 204

        assert unread(mona) == 1
        assert {row["id"]: row["read"] for row in items(mona)} == {str(first.pk): True, str(second.pk): False}

    def test_all_are_marked_read(self, mona, er):
        tell("mona", er), tell("mona", er)

        assert post(mona, "/api/notifications/read-all", {}).status_code == 204

        assert unread(mona) == 0 and all(row["read"] for row in items(mona))

    def test_newest_first(self, mona, er):
        older, newer = tell("mona", er), tell("mona", er)
        Notification.objects.filter(pk=older.pk).update(created_at=timezone.now() - dt.timedelta(days=1))

        assert [row["id"] for row in items(mona)] == [str(newer.pk), str(older.pk)]


class TestOnlyOnesOwn:
    def test_another_users_cannot_be_read_or_marked(self, mona, er, client_of):
        note = tell("mona", er)
        omar = client_of("omar", Role.UNIT_MANAGER)

        assert items(omar) == [] and unread(omar) == 0
        assert post(omar, f"/api/notifications/{note.pk}/read", {}).status_code == 404
        assert post(omar, "/api/notifications/read-all", {}).status_code == 204
        assert Notification.objects.get(pk=note.pk).read_at is None

    @pytest.mark.parametrize("role", list(Role))
    def test_every_role_reads_its_own(self, role, client_of):
        client = client_of("someone", role)

        assert items(client) == [] and unread(client) == 0


class TestTheWords:
    def test_they_follow_a_rename_and_a_corrected_name(self, mona, er):
        tell("mona", er)
        er.name = "Emergency"
        er.save()
        User.objects.filter(username="mona").update(full_name="Mona Al-Harbi")

        assert items(mona)[0]["text"] == "Mona Al-Harbi rejected the Emergency roster for June 2026. The comment is with the draft."


class TestThePollIsNotActivity:
    def _expiry(self, client):
        return Session.objects.get(pk=client.cookies["sessionid"].value).expire_date

    def _push_back(self, client):
        earlier = timezone.now() + dt.timedelta(hours=1)
        Session.objects.filter(pk=client.cookies["sessionid"].value).update(expire_date=earlier)
        return earlier

    def test_the_bells_poll_does_not_move_the_idle_limit_and_reading_the_list_does(self, mona, er):
        tell("mona", er)
        earlier = self._push_back(mona)

        polled = mona.get("/api/notifications/unread")

        assert polled.status_code == 200 and polled.json() == {"unread": 1}
        assert "sessionid" not in polled.cookies and self._expiry(mona) == earlier
        assert mona.get("/api/notifications").status_code == 200
        assert self._expiry(mona) > earlier + dt.timedelta(hours=1)

    def test_a_refused_poll_is_not_activity_either(self, client_of):
        """A user who must change their password is refused everything else; an open tab must not keep that session alive."""
        forced = client_of("fresh", Role.STAFF_NURSE, must_change_password=True)
        earlier = self._push_back(forced)

        assert forced.get("/api/notifications/unread").status_code == 403
        assert self._expiry(forced) == earlier

    def test_a_session_past_its_limit_ends_the_poll(self, mona):
        Session.objects.filter(pk=mona.cookies["sessionid"].value).update(expire_date=timezone.now() - dt.timedelta(seconds=1))

        assert mona.get("/api/notifications/unread").status_code == 401
