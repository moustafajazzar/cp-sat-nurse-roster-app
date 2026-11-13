"""`deploy/reset-admin-password.sh` runs `manage.py reset_admin_password` on the server: for a System Admin who lost
the password, or who is locked out, since anyone can lock any account for 15 minutes with five wrong passwords."""
import io
import re

import pytest
from django.core.management import CommandError, call_command
from django.test import Client

from accounts.models import LoginAttempts, Role, User
from audit.models import AuditEntry
from conftest import PASSWORD, post

pytestmark = pytest.mark.django_db
NEW_PASSWORD = re.compile(r"^New password for admin: ([a-z0-9]{4}(?:-[a-z0-9]{4}){3})$", re.M)


@pytest.fixture
def admin(make_user):
    return make_user("admin", Role.SYSTEM_ADMIN, full_name="System Admin")


def reset(*arguments) -> str:
    out = io.StringIO()
    call_command("reset_admin_password", *arguments, stdout=out)
    return out.getvalue()


def log_in(password, username="admin"):
    return post(Client(), "/api/auth/login", {"username": username, "password": password})


def test_it_prints_a_new_password_once_with_a_forced_change(admin):
    printed = NEW_PASSWORD.search(reset())[1]

    assert log_in(PASSWORD).status_code == 401
    assert log_in(printed).status_code == 200
    assert User.objects.get(username="admin").must_change_password


def test_it_lifts_a_lock_from_wrong_passwords(admin):
    for _ in range(5):
        log_in("wrong-password")
    assert LoginAttempts.objects.get(username="admin").locked_until is not None

    printed = NEW_PASSWORD.search(reset())[1]

    assert not LoginAttempts.objects.filter(username="admin").exists()
    assert log_in(printed).status_code == 200


def test_it_ends_the_accounts_sessions(admin):
    client = Client()
    assert post(client, "/api/auth/login", {"username": "admin", "password": PASSWORD}).status_code == 200

    reset()

    assert client.get("/api/auth/session").json()["user"] is None


def test_it_is_audited_as_the_servers_and_without_the_password(admin):
    printed = NEW_PASSWORD.search(reset())[1]

    entry = AuditEntry.objects.get(action="user.password_reset")
    assert (entry.actor, entry.target, entry.after) == (None, "admin", {"must_change_password": True, "from": "the server"})
    assert printed not in str(entry.after)


def test_another_system_admin_by_name(make_user):
    make_user("it-lead", Role.SYSTEM_ADMIN)

    assert "New password for it-lead:" in reset("--username", "it-lead")


@pytest.mark.parametrize("role", [Role.CNO, Role.UNIT_MANAGER, Role.STAFF_NURSE])
def test_only_a_system_admin_account(make_user, role):
    make_user("admin", role)

    with pytest.raises(CommandError, match="not a System Admin"):
        reset()
    assert log_in(PASSWORD).status_code == 200


def test_an_unknown_username(admin):
    with pytest.raises(CommandError, match="No account is called 'nobody'"):
        reset("--username", "nobody")


def test_a_deactivated_account_is_refused(admin):
    User.objects.filter(username="admin").update(is_active=False)

    with pytest.raises(CommandError, match="deactivated"):
        reset()
