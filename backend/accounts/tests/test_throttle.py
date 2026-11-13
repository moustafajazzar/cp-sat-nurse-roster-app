"""The throttle on failed logins from one address, driven as a client drives it:
through the login and Change password, from the address the request comes from."""
from datetime import datetime, timedelta

import pytest
from django.contrib.auth.hashers import MD5PasswordHasher
from django.test import Client
from django.utils import timezone

from accounts.models import LoginAttempts, Role
from audit.models import AuditEntry
from conftest import PASSWORD, post

pytestmark = pytest.mark.django_db

ADDRESS = "10.1.2.3"


def login(username="someone", password=PASSWORD, address=ADDRESS, client=None, headers=None):
    return (client or Client()).post("/api/auth/login", {"username": username, "password": password},
                                     content_type="application/json", REMOTE_ADDR=address, headers=headers)


def fail(times, address=ADDRESS, username=None, headers=None):
    """A different name each time unless one is given, as a client escaping the lockout would send."""
    response = None
    for n in range(times):
        response = login(username or f"guess{n}", "wrong-password", address, headers=headers)
    return response


@pytest.fixture
def hashes(monkeypatch):
    """How many times the password hasher ran, as the audit's harness counted them: a check and an encode alike."""
    calls = []
    for name in ("verify", "encode"):
        original = getattr(MD5PasswordHasher, name)

        def counted(self, *args, _original=original, **kwargs):
            calls.append(1)
            return _original(self, *args, **kwargs)
        monkeypatch.setattr(MD5PasswordHasher, name, counted)
    return calls


@pytest.fixture
def clock(monkeypatch):
    """Moves the server's clock on by the given time."""
    def move(by: timedelta):
        moved_to = timezone.now() + by
        monkeypatch.setattr(timezone, "now", lambda: moved_to)
    return move


def throttled_until(response) -> datetime:
    return datetime.fromisoformat(response.json()["throttled_until"])


class TestTheLimit:
    def test_the_fiftieth_failure_is_still_answered_and_the_fifty_first_login_is_429(self, make_user):
        make_user()
        assert fail(49).status_code == 401
        assert fail(1).status_code == 401

        r = login()

        assert r.status_code == 429
        assert r.json()["detail"] == "Too many failed logins from this network."
        until = throttled_until(r)
        assert timedelta(minutes=14, seconds=50) < until - timezone.now() <= timedelta(minutes=15)
        assert 890 < int(r.headers["Retry-After"]) <= 900

    def test_the_time_is_on_the_hospitals_clock(self):
        fail(50)

        assert login().json()["throttled_until"].endswith("+03:00")

    def test_forty_nine_failures_do_not_throttle(self, make_user):
        make_user()
        fail(49)

        assert login().status_code == 200

    def test_a_throttled_login_hashes_nothing_and_touches_no_name(self, make_user, hashes):
        make_user()
        LoginAttempts.objects.create(username="someone", failures=2)
        fail(50, username="nobody")
        assert hashes, "an unthrottled refusal hashes, or this test could not fail"
        hashes.clear()

        assert login().status_code == 429
        assert login("never-tried", "wrong-password").status_code == 429

        assert hashes == []
        assert LoginAttempts.objects.get(username="someone").failures == 2
        assert not LoginAttempts.objects.filter(username="never-tried").exists()

    def test_another_address_is_not_affected(self, make_user):
        make_user()
        fail(50)

        assert login(address="10.1.2.4").status_code == 200

    def test_the_windows_end_lifts_it(self, make_user, clock):
        make_user()
        fail(50)
        assert login().status_code == 429

        clock(timedelta(minutes=15, seconds=1))

        assert login().status_code == 200

    def test_a_failure_after_the_window_starts_a_new_one_counting_one(self, clock):
        fail(49)
        clock(timedelta(minutes=15, seconds=1))

        fail(49)
        assert login("x", "wrong-password").status_code == 401  # the fiftieth of the new window
        assert login("x", "wrong-password").status_code == 429

    def test_a_success_neither_counts_nor_clears(self, make_user):
        make_user()
        fail(49)
        for _ in range(3):
            assert login().status_code == 200

        fail(1)

        assert login().status_code == 429

    def test_a_refusal_while_the_name_is_locked_counts(self, make_user):
        make_user()
        assert fail(5, username="someone").status_code == 423
        assert fail(45, username="someone").status_code == 423

        assert login("other", "wrong-password").status_code == 429

    def test_a_request_with_no_address_is_never_throttled(self, make_user):
        make_user()
        fail(60, address="")

        assert login(address="").status_code == 200


class TestTheAddress:
    """Behind Caddy every peer is Caddy: the address counted is the one Caddy worked out and sent in its own header."""
    CADDY = "172.18.0.5"

    def test_each_client_behind_caddy_has_a_throttle_of_its_own(self, make_user):
        make_user()
        fail(50, address=self.CADDY, headers={"X-Client-IP": "203.0.113.7"})

        assert login(address=self.CADDY, headers={"X-Client-IP": "203.0.113.7"}).status_code == 429
        assert login(address=self.CADDY, headers={"X-Client-IP": "203.0.113.8"}).status_code == 200

    def test_a_forwarded_for_header_does_not_move_a_client_out_of_its_throttle(self, make_user):
        make_user()
        fail(50, address=self.CADDY, headers={"X-Client-IP": "203.0.113.7", "X-Forwarded-For": "198.51.100.1"})

        assert login(address=self.CADDY,
                     headers={"X-Client-IP": "203.0.113.7", "X-Forwarded-For": "198.51.100.99"}).status_code == 429


class TestIPv6:
    def test_two_addresses_in_one_64_share_a_budget(self):
        fail(25, address="2001:db8:1:2::1")
        fail(25, address="2001:db8:1:2:ffff:ffff:ffff:9")

        assert login(address="2001:db8:1:2::77").status_code == 429

    def test_two_64s_do_not(self, make_user):
        make_user()
        fail(50, address="2001:db8:1:2::1")

        assert login(address="2001:db8:1:3::1").status_code == 200

    def test_an_ipv4_address_written_as_ipv6_is_that_ipv4_address(self, make_user):
        make_user()
        fail(50, address="::ffff:10.9.9.9")

        assert login(address="10.9.9.9").status_code == 429
        assert login(address="::ffff:10.9.9.10").status_code == 200


class TestAudit:
    def test_reaching_the_limit_is_one_entry_and_the_refusals_during_it_none(self):
        fail(50)
        r = login("anyone", "wrong-password")
        login("anyone-else", "wrong-password")

        throttled = AuditEntry.objects.get(action="auth.login_throttled")
        assert (throttled.actor, throttled.target, throttled.ip) == (None, "", ADDRESS)
        assert throttled.after["until"] == throttled_until(r).replace(microsecond=0).isoformat()
        assert AuditEntry.objects.filter(action="auth.login_failed").count() == 50
        assert AuditEntry.objects.count() == 51

    def test_the_ipv6_entry_names_the_address_itself(self):
        fail(50, address="2001:db8:1:2::1")

        assert AuditEntry.objects.get(action="auth.login_throttled").ip == "2001:db8:1:2::1"


class TestChangePassword:
    def change(self, client, current=PASSWORD, new="a-brand-new-passphrase"):
        return post(client, "/api/auth/change-password", {"current_password": current, "new_password": new})

    def signed_in(self, make_user):
        make_user(role=Role.CNO)
        client = Client(REMOTE_ADDR=ADDRESS)
        assert login(client=client).status_code == 200
        return client

    def test_it_is_refused_while_the_address_is_throttled_before_the_current_password_is_checked(self, make_user, hashes):
        client = self.signed_in(make_user)
        fail(50)
        hashes.clear()

        r = self.change(client)

        assert r.status_code == 429
        assert r.json()["detail"] == "Too many failed logins from this network."
        assert int(r.headers["Retry-After"]) > 0
        assert hashes == []
        assert not AuditEntry.objects.filter(action="auth.password_changed").exists()

    def test_a_wrong_current_password_counts_one_failure(self, make_user):
        client = self.signed_in(make_user)
        fail(49)

        assert self.change(client, current="not-it").status_code == 400

        assert login("x", "wrong-password").status_code == 429

    def test_a_right_current_password_counts_nothing(self, make_user):
        client = self.signed_in(make_user)
        fail(49)

        assert self.change(client).status_code == 200

        assert login("x", "wrong-password").status_code == 401
