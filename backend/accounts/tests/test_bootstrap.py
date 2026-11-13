import pytest
from django.core.management import CommandError, call_command

from accounts.models import Role, User
from audit.models import AuditEntry

pytestmark = pytest.mark.django_db


def test_first_start_creates_the_system_admin_with_a_forced_change(monkeypatch):
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "one-time-password")

    call_command("bootstrap")

    admin = User.objects.get()
    assert admin.username == "admin" and admin.role == Role.SYSTEM_ADMIN
    assert admin.must_change_password and admin.is_active
    assert admin.check_password("one-time-password")
    # The key comes from PostgreSQL's uuidv7().
    assert admin.pk.version == 7


def test_the_creation_is_audited_without_the_password(monkeypatch):
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "one-time-password")

    call_command("bootstrap")

    entry = AuditEntry.objects.get()
    assert entry.action == "user.created" and entry.actor is None and entry.target == "admin"
    assert "one-time-password" not in str(entry.after)


def test_the_variable_is_ignored_on_later_starts(monkeypatch):
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "one-time-password")
    call_command("bootstrap")
    admin = User.objects.get()
    admin.set_password("what-they-chose-later")
    admin.must_change_password = False
    admin.save()

    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "a-different-password")
    call_command("bootstrap")
    monkeypatch.delenv("INITIAL_ADMIN_PASSWORD")
    call_command("bootstrap")

    admin = User.objects.get()
    assert admin.check_password("what-they-chose-later") and not admin.must_change_password
    assert AuditEntry.objects.count() == 1


def test_first_start_without_the_variable_stops_rather_than_inventing_a_password(monkeypatch):
    monkeypatch.delenv("INITIAL_ADMIN_PASSWORD", raising=False)

    with pytest.raises(CommandError, match="INITIAL_ADMIN_PASSWORD"):
        call_command("bootstrap")

    assert not User.objects.exists()
