from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.db import IntegrityError, connection, transaction
from django.test import Client

from accounts.api import MANAGED_ROLES
from accounts.models import LoginAttempts, Role, User
from accounts.provisioning import generate_password
from audit.models import AuditEntry
from conftest import PASSWORD, post
from notifications.models import PushSubscription

pytestmark = pytest.mark.django_db

USERS = "/api/users"
NEW = {"username": "fatimah", "full_name": "Fatimah Al-Otaibi", "role": "cno"}


@pytest.fixture
def admin(signed_in):
    return signed_in(Role.SYSTEM_ADMIN, username="admin")


def create(client, **fields):
    return post(client, USERS, NEW | fields)


def login(username, password):
    client = Client()
    return client, post(client, "/api/auth/login", {"username": username, "password": password})


def act(client, user_id, action):
    return client.post(f"{USERS}/{user_id}/{action}")


def last_entry():
    return AuditEntry.objects.first()


class TestCreate:
    @pytest.mark.parametrize("role", ["cno", "deputy_cno", "unit_manager"])
    def test_the_system_admin_creates_each_role(self, admin, role):
        r = create(admin, role=role)

        assert r.status_code == 201
        user = r.json()["user"]
        assert (user["username"], user["full_name"], user["role"]) == ("fatimah", "Fatimah Al-Otaibi", role)
        assert user["is_active"] is True and user["must_change_password"] is True

    def test_the_generated_password_logs_in_and_forces_a_change(self, admin):
        password = create(admin).json()["generated_password"]

        client, r = login("fatimah", password)

        assert r.status_code == 200 and r.json()["must_change_password"] is True
        assert client.get("/api/auth/session").json()["user"]["must_change_password"] is True
        changed = post(client, "/api/auth/change-password",
                       {"current_password": password, "new_password": "a-brand-new-passphrase"})
        assert changed.status_code == 200 and changed.json()["must_change_password"] is False

    def test_the_password_is_shown_once_and_never_stored_in_clear(self, admin):
        r = create(admin)
        password = r.json()["generated_password"]

        assert "no-store" in r["Cache-Control"]
        assert password not in admin.get(USERS).content.decode()
        assert User.objects.get(username="fatimah").password != password
        assert password not in str(list(AuditEntry.objects.values()))

    def test_generated_passwords_pass_the_policy_and_differ(self, make_user):
        user = make_user("fatimah", full_name="Fatimah Al-Otaibi")
        passwords = {generate_password() for _ in range(50)}

        assert len(passwords) == 50
        for password in passwords:
            validate_password(password, user)

    def test_it_is_audited(self, admin):
        create(admin)

        entry = last_entry()
        assert (entry.action, entry.actor.username, entry.target) == ("user.created", "admin", "fatimah")
        assert entry.before is None
        assert entry.after == {"username": "fatimah", "full_name": "Fatimah Al-Otaibi", "role": "cno",
                               "must_change_password": True}

    @pytest.mark.parametrize("role", ["staff_nurse", "system_admin", "king", ""])
    def test_a_nurse_or_a_second_admin_is_not_the_system_admins_to_create(self, admin, role):
        assert create(admin, role=role).status_code == 422
        assert not User.objects.filter(username="fatimah").exists()

    @pytest.mark.parametrize("fields", [
        {"username": ""}, {"username": "   "}, {"username": "has space"}, {"username": "nul\x00byte"},
        {"username": "x" * 151}, {"full_name": ""}, {"full_name": "   "}, {"full_name": "nul\x00byte"},
        {"full_name": "x" * 201},
    ])
    def test_impossible_fields_are_a_validation_error_not_a_crash(self, admin, fields):
        assert create(admin, **fields).status_code == 422

    def test_the_name_is_trimmed(self, admin):
        assert create(admin, full_name="  Fatimah  ").json()["user"]["full_name"] == "Fatimah"


class TestCardinality:
    @pytest.mark.parametrize("role, says", [("cno", "CNO"), ("deputy_cno", "Deputy CNO")])
    def test_a_second_is_refused_and_says_who_holds_it(self, admin, role, says):
        assert create(admin, role=role).status_code == 201
        count = AuditEntry.objects.count()

        r = create(admin, username="another", role=role)

        assert r.status_code == 409
        assert r.json()["role"] == [f"fatimah is already the {says}. Deactivate that user first."]
        assert not User.objects.filter(username="another").exists()
        assert AuditEntry.objects.count() == count

    def test_any_number_of_unit_managers(self, admin):
        for name in ("one", "two", "three"):
            assert create(admin, username=name, role="unit_manager").status_code == 201

    def test_the_database_refuses_it_too(self, make_user):
        make_user("first", Role.CNO)

        with pytest.raises(IntegrityError), transaction.atomic():
            make_user("second", Role.CNO)

    def test_a_deactivated_cno_leaves_room_for_a_successor(self, admin):
        first = create(admin).json()["user"]["id"]
        act(admin, first, "deactivate")

        assert create(admin, username="successor").status_code == 201

    def test_reactivating_while_a_successor_holds_it_is_refused(self, admin):
        first = create(admin).json()["user"]["id"]
        act(admin, first, "deactivate")
        create(admin, username="successor")
        count = AuditEntry.objects.count()

        r = act(admin, first, "reactivate")

        assert r.status_code == 409
        assert r.json()["detail"] == "successor is already the CNO. Deactivate that user first."
        assert User.objects.get(pk=first).is_active is False
        assert AuditEntry.objects.count() == count

    @pytest.mark.django_db(transaction=True)
    def test_two_at_the_same_moment_make_one(self, admin):
        def attempt(name):
            try:
                return create(admin, username=name).status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(8) as pool:
            statuses = sorted(pool.map(attempt, [f"user{i}" for i in range(8)]))

        assert statuses == [201] + [409] * 7
        assert User.objects.filter(role=Role.CNO).count() == 1


class TestUsernames:
    def test_a_taken_username_is_refused(self, admin):
        create(admin)

        r = create(admin, role="unit_manager")

        assert r.status_code == 409 and r.json()["username"] == ["This username is taken."]

    def test_it_is_taken_in_any_case(self, admin):
        create(admin)

        assert create(admin, username="FATIMAH", role="unit_manager").status_code == 409

    def test_the_system_admins_own_username_is_taken(self, admin):
        assert create(admin, username="admin").status_code == 409


class TestList:
    def test_it_shows_the_managed_roles_only(self, admin, make_user):
        make_user("the-cno", Role.CNO)
        make_user("a-manager", Role.UNIT_MANAGER)
        make_user("a-nurse", Role.STAFF_NURSE)

        r = admin.get(USERS)

        assert r.status_code == 200
        assert [user["username"] for user in r.json()] == ["a-manager", "the-cno"]

    def test_the_last_login_is_in_hospital_time(self, admin, make_user):
        user = make_user("the-cno", Role.CNO)
        User.objects.filter(pk=user.pk).update(last_login=datetime(2026, 11, 5, 21, 30, tzinfo=UTC))

        assert admin.get(USERS).json()[0]["last_login"] == "2026-11-06T00:30:00+03:00"

    def test_it_holds_no_password(self, admin, make_user):
        make_user("the-cno", Role.CNO)

        assert "password\"" not in admin.get(USERS).content.decode().replace("must_change_password\"", "")


class TestDeactivate:
    def test_they_cannot_log_in_and_their_session_ends(self, admin, make_user):
        user = make_user("the-cno", Role.CNO)
        client, _ = login("the-cno", PASSWORD)

        r = act(admin, user.pk, "deactivate")

        assert r.status_code == 200 and r.json()["is_active"] is False
        assert client.get("/api/auth/session").json() == {"user": None}
        assert login("the-cno", PASSWORD)[1].status_code == 401

    def test_every_push_subscription_of_theirs_ends_and_nobody_elses(self, admin, make_user):
        user = make_user("the-cno", Role.CNO)
        other = make_user("the-deputy", Role.DEPUTY_CNO)
        for owner, device in [(user, "phone"), (user, "laptop"), (other, "phone-2")]:
            PushSubscription.objects.create(user=owner, endpoint=f"https://fcm.googleapis.com/fcm/send/{device}", p256dh="k",
                                            auth="a")

        act(admin, user.pk, "deactivate")

        assert list(PushSubscription.objects.values_list("user__username", flat=True)) == ["the-deputy"]
        assert not AuditEntry.objects.filter(action__startswith="push.").exists()

    def test_only_their_sessions_end(self, admin, make_user):
        user = make_user("the-cno", Role.CNO)
        login("the-cno", PASSWORD)

        act(admin, user.pk, "deactivate")

        assert admin.get(USERS).status_code == 200
        assert Session.objects.count() == 1

    def test_reactivating_lets_them_log_in_but_not_back_into_the_old_session(self, admin, make_user):
        user = make_user("the-cno", Role.CNO)
        client, _ = login("the-cno", PASSWORD)
        act(admin, user.pk, "deactivate")

        r = act(admin, user.pk, "reactivate")

        assert r.status_code == 200 and r.json()["is_active"] is True
        assert client.get("/api/auth/session").json() == {"user": None}
        assert login("the-cno", PASSWORD)[1].status_code == 200

    def test_both_are_audited_once(self, admin, make_user):
        user = make_user("the-cno", Role.CNO)

        act(admin, user.pk, "deactivate")
        entry = last_entry()
        assert (entry.action, entry.actor.username, entry.target) == ("user.deactivated", "admin", "the-cno")
        assert (entry.before, entry.after) == ({"is_active": True}, {"is_active": False})

        count = AuditEntry.objects.count()
        act(admin, user.pk, "deactivate")
        assert AuditEntry.objects.count() == count

        act(admin, user.pk, "reactivate")
        entry = last_entry()
        assert (entry.action, entry.target) == ("user.reactivated", "the-cno")
        assert (entry.before, entry.after) == ({"is_active": False}, {"is_active": True})

        count = AuditEntry.objects.count()
        act(admin, user.pk, "reactivate")
        assert AuditEntry.objects.count() == count


class TestResetPassword:
    def test_the_new_password_works_once_shown_and_forces_a_change(self, admin, make_user):
        user = make_user("the-cno", Role.CNO)

        r = act(admin, user.pk, "reset-password")

        assert r.status_code == 200 and "no-store" in r["Cache-Control"]
        assert r.json()["user"]["must_change_password"] is True
        assert login("the-cno", PASSWORD)[1].status_code == 401
        client, answer = login("the-cno", r.json()["generated_password"])
        assert answer.status_code == 200 and answer.json()["must_change_password"] is True
        assert client.get("/api/ops/health").status_code == 403

    def test_it_ends_their_sessions(self, admin, make_user):
        user = make_user("the-cno", Role.CNO)
        client, _ = login("the-cno", PASSWORD)

        act(admin, user.pk, "reset-password")

        assert client.get("/api/auth/session").json() == {"user": None}

    def test_it_lifts_a_lock(self, admin, make_user):
        user = make_user("the-cno", Role.CNO)
        for _ in range(5):
            login("the-cno", "wrong-password")
        assert LoginAttempts.objects.get(username="the-cno").locked_until is not None

        password = act(admin, user.pk, "reset-password").json()["generated_password"]

        assert login("the-cno", password)[1].status_code == 200

    def test_it_leaves_another_users_lock(self, admin, make_user):
        user = make_user("the-cno", Role.CNO)
        for _ in range(5):
            login("other", "wrong-password")

        act(admin, user.pk, "reset-password")

        assert LoginAttempts.objects.filter(username="other").exists()

    def test_it_is_audited_without_the_password(self, admin, make_user):
        user = make_user("the-cno", Role.CNO)

        password = act(admin, user.pk, "reset-password").json()["generated_password"]

        entry = last_entry()
        assert (entry.action, entry.actor.username, entry.target) == ("user.password_reset", "admin", "the-cno")
        assert password not in str(entry.after)


class TestCorrectName:
    @staticmethod
    def correct(client, user_id, **body):
        return client.put(f"{USERS}/{user_id}/full-name", {"full_name": "Fatimah Saleh"} | body,
                          content_type="application/json")

    @pytest.mark.parametrize("role", MANAGED_ROLES)
    def test_the_system_admin_corrects_each_managed_role(self, admin, make_user, role):
        user = make_user("the-user", role, full_name="Fatimah Salih")

        r = self.correct(admin, user.pk, full_name="  Fatimah Saleh ")

        assert r.status_code == 200
        assert (r.json()["username"], r.json()["full_name"], r.json()["role"]) == ("the-user", "Fatimah Saleh", role)
        user.refresh_from_db()
        assert user.full_name == "Fatimah Saleh"

    def test_it_is_audited_with_the_name_before_and_after(self, admin, make_user):
        user = make_user("the-cno", Role.CNO, full_name="Fatimah Salih")

        self.correct(admin, user.pk)

        entry = last_entry()
        assert (entry.action, entry.actor.username, entry.target) == ("user.name_corrected", "admin", "the-cno")
        assert entry.before == {"full_name": "Fatimah Salih"} and entry.after == {"full_name": "Fatimah Saleh"}

    def test_a_save_with_no_change_writes_nothing(self, admin, make_user):
        user = make_user("the-cno", Role.CNO, full_name="Fatimah Saleh")
        count = AuditEntry.objects.count()

        r = self.correct(admin, user.pk, full_name=" Fatimah Saleh ")

        assert r.status_code == 200 and r.json()["full_name"] == "Fatimah Saleh"
        assert AuditEntry.objects.count() == count

    def test_a_deactivated_user_is_corrected_and_stays_deactivated(self, admin, make_user):
        user = make_user("the-cno", Role.CNO, full_name="Fatimah Salih", is_active=False)

        r = self.correct(admin, user.pk)

        assert r.status_code == 200 and r.json()["is_active"] is False
        user.refresh_from_db()
        assert user.full_name == "Fatimah Saleh" and user.is_active is False

    def test_nothing_else_about_the_user_changes(self, admin, signed_in, make_user):
        client = signed_in(Role.CNO, username="the-cno", full_name="Fatimah Salih")
        user = User.objects.get(username="the-cno")
        password = user.password

        assert self.correct(admin, user.pk).status_code == 200

        user.refresh_from_db()
        assert (user.username, user.role, user.password, user.must_change_password) == ("the-cno", Role.CNO, password, False)
        assert client.get("/api/auth/session").json()["user"]["full_name"] == "Fatimah Saleh"

    @pytest.mark.parametrize("name", ["", "   ", "x" * 201, "a\x00b", None])
    def test_a_name_that_could_not_be_created_is_refused_on_its_field(self, admin, make_user, name):
        user = make_user("the-cno", Role.CNO, full_name="Fatimah Salih")
        count = AuditEntry.objects.count()

        r = self.correct(admin, user.pk, full_name=name)

        assert r.status_code == 422
        assert [error["loc"][2:] for error in r.json()["detail"]] == [["full_name"]]
        user.refresh_from_db()
        assert user.full_name == "Fatimah Salih"
        assert AuditEntry.objects.count() == count

    def test_the_username_cannot_be_changed_here(self, admin, make_user):
        user = make_user("the-cno", Role.CNO, full_name="Fatimah Salih")

        r = self.correct(admin, user.pk, username="someone-else")

        assert r.status_code == 422
        assert [error["loc"][2:] for error in r.json()["detail"]] == [["username"]]
        user.refresh_from_db()
        assert (user.username, user.full_name) == ("the-cno", "Fatimah Salih")

    @pytest.mark.parametrize("role", [Role.STAFF_NURSE, Role.SYSTEM_ADMIN])
    def test_a_nurse_or_the_system_admin_is_not_found_here(self, signed_in, make_user, role):
        client = signed_in(Role.SYSTEM_ADMIN, username="admin", full_name="As It Was")
        user = (make_user("a-nurse", Role.STAFF_NURSE, full_name="As It Was") if role == Role.STAFF_NURSE
                else User.objects.get(username="admin"))
        count = AuditEntry.objects.count()

        assert self.correct(client, user.pk).status_code == 404
        user.refresh_from_db()
        assert user.full_name == "As It Was"
        assert AuditEntry.objects.count() == count

    def test_an_unknown_id_is_not_found(self, admin):
        assert self.correct(admin, uuid4()).status_code == 404

    @pytest.mark.parametrize("role", [Role.CNO, Role.DEPUTY_CNO, Role.UNIT_MANAGER, Role.STAFF_NURSE])
    def test_no_other_role_may(self, signed_in, make_user, role):
        client = signed_in(role, username="caller")
        user = make_user("the-manager", Role.UNIT_MANAGER, full_name="Mona Ali")

        assert self.correct(client, user.pk).status_code == 403
        user.refresh_from_db()
        assert user.full_name == "Mona Ali"


class TestWhoIsManaged:
    @pytest.mark.parametrize("action", ["deactivate", "reactivate", "reset-password"])
    @pytest.mark.parametrize("role", [Role.STAFF_NURSE, Role.SYSTEM_ADMIN])
    def test_a_nurse_or_the_system_admin_is_not_found_here(self, signed_in, make_user, action, role):
        client = signed_in(Role.SYSTEM_ADMIN, username="admin")
        user = make_user("a-nurse", Role.STAFF_NURSE) if role == Role.STAFF_NURSE else User.objects.get(username="admin")
        count = AuditEntry.objects.count()

        assert act(client, user.pk, action).status_code == 404
        user.refresh_from_db()
        assert user.is_active is True and user.must_change_password is False
        assert AuditEntry.objects.count() == count

    @pytest.mark.parametrize("action", ["deactivate", "reactivate", "reset-password"])
    def test_an_unknown_id_is_not_found(self, admin, action):
        assert act(admin, uuid4(), action).status_code == 404

    def test_a_malformed_id_is_refused(self, admin):
        assert act(admin, "not-a-uuid", "deactivate").status_code in (404, 422)
