import datetime as dt
from datetime import timedelta

import pytest
from django.contrib.sessions.models import Session
from django.test import Client
from django.utils import timezone

from accounts.models import LoginAttempts, Role, User
from audit.models import AuditEntry
from nurses.models import Gender, NurseProfile
from conftest import PASSWORD, post

pytestmark = pytest.mark.django_db


def login(client, username="someone", password=PASSWORD, **extra):
    return client.post("/api/auth/login", {"username": username, "password": password},
                       content_type="application/json", **extra)


def fail(times, username="someone"):
    for _ in range(times):
        response = login(Client(), username, "wrong-password")
    return response


class TestSession:
    def test_anonymous_session_has_no_user_and_sets_the_csrf_cookie(self):
        client = Client()

        r = client.get("/api/auth/session")

        assert r.status_code == 200 and r.json() == {"user": None}
        assert "csrftoken" in client.cookies

    def test_login_then_session_then_logout(self, make_user):
        make_user(full_name="Some One", role=Role.CNO)
        client = Client()

        r = login(client)
        assert r.status_code == 200
        me = {"username": "someone", "full_name": "Some One", "role": "cno", "must_change_password": False,
              "has_nurse_profile": False, "time_zone": "Asia/Riyadh"}
        assert r.json() == me
        assert client.get("/api/auth/session").json() == {"user": me}

        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/auth/session").json() == {"user": None}

    @pytest.mark.parametrize(("role", "profile"), [
        (Role.UNIT_MANAGER, True), (Role.UNIT_MANAGER, False), (Role.STAFF_NURSE, True),
        (Role.CNO, False), (Role.DEPUTY_CNO, False), (Role.SYSTEM_ADMIN, False),
    ])
    def test_me_says_whether_there_is_a_nurse_profile(self, make_user, role, profile):
        # Only a Unit Manager's header and Home act on it: a manager on their unit's rosters reads My roster and My month.
        user = make_user(role=role)
        if profile:
            NurseProfile.objects.create(user=user, gender=Gender.FEMALE, start_date=dt.date(2026, 1, 1))
        client = Client()

        assert login(client).json()["has_nurse_profile"] is profile
        assert client.get("/api/auth/session").json()["user"]["has_nurse_profile"] is profile

    def test_logout_needs_a_session(self):
        assert Client().post("/api/auth/logout").status_code == 401

    def test_login_refreshes_the_session_key(self, make_user):
        make_user()
        client = Client()
        client.get("/api/auth/session")
        client.session.save()
        anonymous_key = client.session.session_key

        login(client)

        assert client.session.session_key != anonymous_key


class TestCsrf:
    def test_login_without_the_token_is_refused(self, make_user):
        make_user()
        client = Client(enforce_csrf_checks=True)

        assert login(client).status_code == 403
        assert not AuditEntry.objects.exists()

    def test_login_with_the_token_works(self, make_user):
        make_user()
        client = Client(enforce_csrf_checks=True)
        client.get("/api/auth/session")

        r = login(client, headers={"X-CSRFToken": client.cookies["csrftoken"].value})

        assert r.status_code == 200

    def test_a_signed_in_write_without_the_token_is_refused(self, make_user):
        make_user()
        client = Client(enforce_csrf_checks=True)
        client.get("/api/auth/session")
        login(client, headers={"X-CSRFToken": client.cookies["csrftoken"].value})

        assert client.post("/api/auth/logout").status_code == 403
        assert client.get("/api/auth/session").json()["user"] is not None


class TestRefusals:
    def test_wrong_password_and_unknown_user_answer_the_same(self, make_user):
        make_user()

        wrong, unknown = login(Client(), password="nope"), login(Client(), username="nobody", password="nope")

        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()

    def test_an_inactive_user_cannot_log_in(self, make_user):
        make_user(is_active=False)

        assert login(Client()).status_code == 401

    def test_a_deactivated_users_session_ends(self, signed_in):
        client = signed_in(Role.STAFF_NURSE)
        User.objects.update(is_active=False)

        assert client.get("/api/auth/session").json() == {"user": None}

    @pytest.mark.parametrize("username", ["", "x" * 151, "nul\x00byte"])
    def test_impossible_usernames_are_a_validation_error_not_a_crash(self, username):
        assert login(Client(), username=username).status_code == 422


class TestLockout:
    def test_the_fifth_failure_locks_for_fifteen_minutes(self, make_user):
        make_user()

        assert fail(4).status_code == 401
        r = fail(1)

        assert r.status_code == 423
        locked_until = LoginAttempts.objects.get(username="someone").locked_until
        assert timedelta(minutes=14, seconds=50) < locked_until - timezone.now() <= timedelta(minutes=15)
        assert r.json()["locked_until"] is not None

    def test_four_failures_do_not_lock(self, make_user):
        make_user()
        fail(4)

        assert login(Client()).status_code == 200

    def test_while_locked_even_the_right_password_is_refused(self, make_user):
        make_user()
        fail(5)

        assert login(Client()).status_code == 423

    def test_more_failures_while_locked_do_not_extend_the_lock(self, make_user):
        make_user()
        fail(5)
        locked_until = LoginAttempts.objects.get(username="someone").locked_until

        fail(3)

        assert LoginAttempts.objects.get(username="someone").locked_until == locked_until

    def test_after_the_lock_the_right_password_works_and_the_count_restarts(self, make_user):
        make_user()
        fail(5)
        LoginAttempts.objects.update(locked_until=timezone.now() - timedelta(seconds=1))

        assert login(Client()).status_code == 200
        assert not LoginAttempts.objects.exists()

    def test_after_the_lock_a_new_failure_counts_from_one(self, make_user):
        make_user()
        fail(5)
        LoginAttempts.objects.update(locked_until=timezone.now() - timedelta(seconds=1))

        assert fail(1).status_code == 401
        assert LoginAttempts.objects.get().failures == 1

    def test_a_success_resets_the_count(self, make_user):
        make_user()
        fail(4)
        assert login(Client()).status_code == 200

        assert fail(4).status_code == 401

    def test_an_unknown_username_locks_the_same_so_names_cannot_be_probed(self):
        assert fail(5, username="nobody").status_code == 423

    def test_the_lock_says_too_many_failed_logins_and_names_no_account(self):
        assert fail(5, username="nobody").json()["detail"] == "Too many failed logins."

    def test_the_lock_is_sent_on_the_hospitals_clock(self, make_user):
        make_user()

        assert fail(5).json()["locked_until"].endswith("+03:00")

    def test_a_real_name_and_an_unknown_one_get_the_same_answers(self, make_user, monkeypatch):
        make_user()
        now = timezone.now()
        monkeypatch.setattr(timezone, "now", lambda: now)

        def answers(username):
            return [(r.status_code, r.json()) for r in (fail(1, username) for _ in range(6))]

        assert answers("someone") == answers("nobody")

    def test_one_users_lock_leaves_another_alone(self, make_user):
        make_user()
        make_user("other")
        fail(5)

        assert login(Client(), username="other").status_code == 200


class TestAudit:
    def test_a_login_is_audited_with_actor_and_ip(self, make_user):
        user = make_user()

        login(Client(), REMOTE_ADDR="10.1.2.3")

        entry = AuditEntry.objects.get()
        assert (entry.action, entry.actor, entry.target, entry.ip) == ("auth.login", user, "someone", "10.1.2.3")

    def test_the_ip_is_the_one_caddy_worked_out(self, make_user):
        make_user()

        login(Client(), REMOTE_ADDR="172.18.0.2", headers={"X-Client-IP": "10.9.8.7"})

        assert AuditEntry.objects.get().ip == "10.9.8.7"

    def test_a_forwarded_for_header_changes_nothing_behind_caddy(self, make_user):
        make_user()

        login(Client(), REMOTE_ADDR="172.18.0.2",
              headers={"X-Client-IP": "10.9.8.7", "X-Forwarded-For": "203.0.113.9, 198.51.100.1"})

        assert AuditEntry.objects.get().ip == "10.9.8.7"

    def test_a_forwarded_for_header_changes_nothing_without_caddy(self, make_user):
        make_user()

        login(Client(), REMOTE_ADDR="172.18.0.2", headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.1"})

        assert AuditEntry.objects.get().ip == "172.18.0.2"

    def test_a_nonsense_client_address_header_falls_back_to_the_peer(self, make_user):
        make_user()

        login(Client(), REMOTE_ADDR="172.18.0.2", headers={"X-Client-IP": "not an ip"})

        assert AuditEntry.objects.get().ip == "172.18.0.2"

    def test_a_failed_login_is_audited_without_the_password(self, make_user):
        user = make_user()

        login(Client(), password="the-wrong-guess")

        entry = AuditEntry.objects.get()
        assert (entry.action, entry.actor, entry.target) == ("auth.login_failed", user, "someone")
        assert entry.after == {"reason": "wrong_credentials", "failures": 1}
        assert "the-wrong-guess" not in str(entry.after)

    def test_a_failure_with_an_unknown_username_keeps_nothing_that_was_typed(self):
        # A password typed into the username box must not end up where the System Admin reads it.
        login(Client(), username="my-secret-pass", REMOTE_ADDR="10.1.2.3")

        entry = AuditEntry.objects.get()
        assert (entry.action, entry.actor, entry.target, entry.ip) == ("auth.login_failed", None, "", "10.1.2.3")
        assert entry.after == {"reason": "wrong_credentials", "failures": 1}
        assert "my-secret-pass" not in str(entry.after)

    def test_a_refusal_while_an_unknown_username_is_locked_names_nothing_either(self):
        fail(5, username="nobody")
        AuditEntry.objects.all().delete()

        assert login(Client(), username="nobody", REMOTE_ADDR="10.1.2.3").status_code == 423

        entry = AuditEntry.objects.get()
        assert (entry.action, entry.actor, entry.target, entry.ip) == ("auth.login_failed", None, "", "10.1.2.3")
        assert entry.after == {"reason": "locked"}

    def test_a_refusal_while_a_real_name_is_locked_names_it(self, make_user):
        user = make_user()
        fail(5)
        AuditEntry.objects.all().delete()

        login(Client())

        entry = AuditEntry.objects.get()
        assert (entry.actor, entry.target) == (user, "someone")

    def test_a_deactivated_account_still_has_its_name(self, make_user):
        user = make_user(is_active=False)

        login(Client())

        entry = AuditEntry.objects.get()
        assert (entry.action, entry.actor, entry.target) == ("auth.login_failed", user, "someone")

    def test_a_name_differing_only_in_case_is_not_the_account(self, make_user):
        make_user()

        login(Client(), username="SomeOne", password="wrong-password")

        entry = AuditEntry.objects.get()
        assert (entry.actor, entry.target) == (None, "")

    def test_a_refusal_while_locked_is_audited_too(self, make_user):
        make_user()
        fail(5)
        AuditEntry.objects.all().delete()

        login(Client())

        assert AuditEntry.objects.get().after == {"reason": "locked"}

    def test_logout_is_audited(self, signed_in):
        client = signed_in()

        client.post("/api/auth/logout")

        assert AuditEntry.objects.first().action == "auth.logout"


class TestSessionExpiry:
    def test_the_session_lasts_twelve_hours(self, signed_in):
        client = signed_in()

        assert client.session.get_expiry_age() == 12 * 60 * 60
        remaining = Session.objects.get().expire_date - timezone.now()
        assert timedelta(hours=11, minutes=59) < remaining <= timedelta(hours=12)

    def test_activity_pushes_the_expiry_forward(self, signed_in):
        client = signed_in()
        Session.objects.update(expire_date=timezone.now() + timedelta(minutes=5))

        assert client.get("/api/auth/session").json()["user"] is not None

        assert Session.objects.get().expire_date - timezone.now() > timedelta(hours=11, minutes=59)

    def test_after_twelve_idle_hours_the_session_is_gone(self, signed_in):
        client = signed_in()
        Session.objects.update(expire_date=timezone.now() - timedelta(seconds=1))

        assert client.get("/api/auth/session").json() == {"user": None}
        assert client.get("/api/ops/technical-settings").status_code == 401


class TestForcedChange:
    def change(self, client, current=PASSWORD, new="a-brand-new-passphrase"):
        return post(client, "/api/auth/change-password", {"current_password": current, "new_password": new})

    def test_login_reports_it_and_other_endpoints_refuse_until_done(self, signed_in):
        client = signed_in(must_change_password=True)

        assert client.get("/api/auth/session").json()["user"]["must_change_password"] is True
        assert client.get("/api/ops/technical-settings").status_code == 403
        assert client.get("/api/ops/health").status_code == 403

    def test_changing_clears_it_keeps_the_session_and_opens_the_rest(self, signed_in):
        client = signed_in(must_change_password=True)

        r = self.change(client)

        assert r.status_code == 200 and r.json()["must_change_password"] is False
        assert client.get("/api/ops/technical-settings").status_code == 200

    def test_the_new_password_is_the_one_that_works(self, signed_in):
        client = signed_in(must_change_password=True)
        self.change(client)
        client.post("/api/auth/logout")

        assert login(Client()).status_code == 401
        assert login(Client(), password="a-brand-new-passphrase").status_code == 200

    @pytest.mark.parametrize("role", list(Role))
    def test_every_role_changes_their_own_when_they_choose(self, signed_in, role):
        client = signed_in(role)

        assert self.change(client).status_code == 200
        client.post("/api/auth/logout")
        assert login(Client(), password="a-brand-new-passphrase").status_code == 200

    def test_another_session_of_theirs_ends(self, signed_in):
        client = signed_in()
        other = Client()
        assert login(other).status_code == 200

        self.change(client)

        assert other.get("/api/auth/session").json() == {"user": None}

    def test_the_change_is_audited_without_either_password(self, signed_in):
        client = signed_in(must_change_password=True)

        self.change(client)

        entry = AuditEntry.objects.first()
        assert entry.action == "auth.password_changed" and entry.after == {"forced": True}

    def test_logging_out_is_allowed_before_the_change(self, signed_in):
        assert signed_in(must_change_password=True).post("/api/auth/logout").status_code == 204

    def test_it_needs_a_session(self):
        assert self.change(Client()).status_code == 401

    def test_the_current_password_must_be_right(self, signed_in):
        client = signed_in(must_change_password=True)

        r = self.change(client, current="not-it")

        assert r.status_code == 400 and r.json()["current_password"]
        assert client.get("/api/auth/session").json()["user"]["must_change_password"] is True

    @pytest.mark.parametrize("new, says", [
        ("short1!", "at least 10"),
        ("password123", "too common"),
        ("someone123", "too similar"),
        (PASSWORD, "must differ"),
    ])
    def test_the_policy(self, signed_in, new, says):
        client = signed_in(must_change_password=True)

        r = self.change(client, new=new)

        assert r.status_code == 400
        assert says in " ".join(r.json()["new_password"])
        assert client.get("/api/auth/session").json()["user"]["must_change_password"] is True

    def test_similarity_covers_the_full_name(self, signed_in):
        client = signed_in(must_change_password=True, full_name="Fatimah Al-Otaibi")

        assert self.change(client, new="fatimah-al-otaibi").status_code == 400

    def test_digits_alone_are_allowed_if_long_and_uncommon(self, signed_in):
        # The agreed policy is length, common passwords and similarity, nothing more.
        assert self.change(signed_in(), new="8302957164028").status_code == 200
