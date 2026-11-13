from datetime import timedelta

import httpx
import pytest
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.utils import timezone

from accounts.models import Role
from audit.models import AuditEntry
from conftest import post
from notifications.models import PushAttempt
from ops.models import TechnicalSettings

pytestmark = pytest.mark.django_db

NON_ADMINS = [Role.CNO, Role.DEPUTY_CNO, Role.UNIT_MANAGER, Role.STAFF_NURSE]
DEFAULTS = {"time_limit_cap_seconds": 300, "backup_time": "02:00", "backup_retention_days": 30, "close_time": "00:30"}
CHANGED = {"time_limit_cap_seconds": 600, "backup_time": "03:15", "backup_retention_days": 14, "close_time": "01:00"}
SETTINGS = "/api/ops/technical-settings"
HEALTH = "/api/ops/health"


def put(client, body, path=SETTINGS):
    return client.put(path, body, content_type="application/json")


class TestTechnicalSettings:
    def test_the_agreed_defaults(self, signed_in):
        r = signed_in().get(SETTINGS)

        assert r.status_code == 200 and r.json() == DEFAULTS

    def test_the_system_admin_changes_them(self, signed_in):
        client = signed_in()

        r = put(client, CHANGED)

        assert r.status_code == 200 and r.json() == CHANGED
        assert client.get(SETTINGS).json() == CHANGED
        assert TechnicalSettings.objects.count() == 1

    def test_a_change_is_audited_with_before_and_after(self, signed_in):
        client = signed_in()

        put(client, CHANGED)

        entry = AuditEntry.objects.first()
        assert entry.action == "technical_settings.changed" and entry.actor.username == "someone"
        assert entry.before == DEFAULTS and entry.after == CHANGED

    def test_saving_with_no_change_writes_no_audit_entry(self, signed_in):
        client = signed_in()
        count = AuditEntry.objects.count()

        put(client, DEFAULTS)

        assert AuditEntry.objects.count() == count

    @pytest.mark.parametrize("field, bad", [
        ("time_limit_cap_seconds", 0), ("time_limit_cap_seconds", -5), ("time_limit_cap_seconds", 86401),
        ("backup_retention_days", 0), ("backup_retention_days", 3651),
        ("backup_time", "24:00"), ("backup_time", "2:00"), ("backup_time", "02:00:00"), ("backup_time", "02:00Z"),
        ("close_time", "00:60"), ("close_time", ""),
    ])
    def test_nonsense_is_refused(self, signed_in, field, bad):
        client = signed_in()

        assert put(client, DEFAULTS | {field: bad}).status_code == 422
        assert client.get(SETTINGS).json() == DEFAULTS

    @pytest.mark.parametrize("role", NON_ADMINS)
    def test_another_role_can_neither_read_nor_change_them(self, signed_in, role):
        client = signed_in(role)

        assert client.get(SETTINGS).status_code == 403
        assert put(client, CHANGED).status_code == 403
        assert TechnicalSettings.load().time_limit_cap_seconds == 300

    def test_without_a_session_neither(self):
        assert Client().get(SETTINGS).status_code == 401
        assert put(Client(), CHANGED).status_code == 401

    def test_there_can_only_be_one_row(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            TechnicalSettings.objects.create(id=2)


@pytest.fixture
def solver_up(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, headers, timeout: httpx.Response(
        200, json={"version": "0.1.0", "workers": 8}, request=httpx.Request("GET", url)))


def attempted(*outcomes, hours_ago=0):
    """Sends that came out so, in this order, that long ago."""
    for outcome in outcomes:
        attempt = PushAttempt.objects.create(outcome=outcome)
        PushAttempt.objects.filter(pk=attempt.pk).update(at=timezone.now() - timedelta(hours=hours_ago))


class TestInstallationHealth:
    def test_it_reports_all_of_it(self, signed_in, solver_up, settings, tmp_path):
        settings.BACKUPS_DIR = tmp_path

        r = signed_in().get(HEALTH)

        assert r.status_code == 200
        body = r.json()
        assert body["database"] is True
        assert body["solver"] == {"reachable": True, "version": "0.1.0", "workers": 8}
        assert body["database_size_bytes"] > 1_000_000
        assert body["disk"]["total_bytes"] >= body["disk"]["free_bytes"] > 0
        assert body["queue"] == {"waiting": 0, "running": 0, "failed": 0, "workers": 0}
        assert body["last_backup"] is None
        assert body["push"] == {"sent": 0, "failed": 0, "last_failure": None}

    def test_a_solver_that_is_down_is_reported_not_a_failure(self, signed_in, monkeypatch):
        def refuse(url, headers, timeout):
            raise httpx.ConnectError("no route")
        monkeypatch.setattr(httpx, "get", refuse)

        r = signed_in().get(HEALTH)

        assert r.status_code == 200 and r.json()["solver"]["reachable"] is False

    def test_a_missing_backups_volume_means_the_disk_is_unknown(self, signed_in, solver_up, settings, tmp_path):
        settings.BACKUPS_DIR = tmp_path / "absent"

        assert signed_in().get(HEALTH).json()["disk"] is None

    def test_the_queue_is_counted_from_procrastinates_tables(self, signed_in, solver_up):
        with connection.cursor() as cursor:
            for status, n in [("todo", 2), ("doing", 1), ("failed", 3), ("succeeded", 4)]:
                for _ in range(n):
                    cursor.execute("INSERT INTO procrastinate_jobs (queue_name, task_name, status) VALUES "
                                   "('default', 'a_task', %s)", [status])
            cursor.execute("INSERT INTO procrastinate_workers (last_heartbeat) VALUES (%s), (%s)",
                           [timezone.now(), timezone.now() - timedelta(minutes=5)])

        queue = signed_in().get(HEALTH).json()["queue"]

        assert queue == {"waiting": 2, "running": 1, "failed": 3, "workers": 1}

    def test_pushes_are_counted_by_attempt_over_the_last_24_hours(self, signed_in, solver_up):
        """Each send to one subscription is a row, a try again included: three sent, and four that failed, one of them just
        inside the day; the two a little over a day old are counted nowhere."""
        attempted("sent", "sent", "refused", "unreachable", "gone", "sent")
        attempted("unreachable", hours_ago=23)
        attempted("sent", "refused", hours_ago=25)

        assert signed_in().get(HEALTH).json()["push"] == {"sent": 3, "failed": 4, "last_failure": "gone"}

    @pytest.mark.parametrize("outcome", ["unreachable", "refused", "gone"])
    def test_the_last_failure_is_said_in_one_of_three_fixed_words(self, signed_in, solver_up, outcome):
        attempted("gone", "refused", "unreachable", hours_ago=2)
        attempted(outcome, hours_ago=1)
        attempted("sent")

        assert signed_in().get(HEALTH).json()["push"]["last_failure"] == outcome

    def test_a_failure_more_than_a_day_old_is_no_longer_the_last(self, signed_in, solver_up):
        attempted("refused", hours_ago=25)
        attempted("sent")

        assert signed_in().get(HEALTH).json()["push"] == {"sent": 1, "failed": 0, "last_failure": None}

    def test_the_push_block_says_nothing_but_two_counts_and_a_word(self, signed_in, solver_up):
        attempted("sent", "unreachable")

        assert set(signed_in().get(HEALTH).json()["push"]) == {"sent", "failed", "last_failure"}

    def test_the_public_health_answer_says_nothing_of_pushes(self, solver_up):
        attempted("sent", "unreachable")

        assert set(Client().get("/api/health").json()) == {"status", "database", "solver"}

    @pytest.mark.parametrize("role", NON_ADMINS)
    def test_another_role_cannot_read_it(self, signed_in, role):
        assert signed_in(role).get(HEALTH).status_code == 403

    def test_without_a_session_neither(self):
        assert Client().get(HEALTH).status_code == 401

    def test_writing_to_it_is_not_a_thing(self, signed_in):
        assert post(signed_in(), HEALTH, {}).status_code == 405
