"""The nightly backup: due at the System Admin's backup time on the hospital's clock, kept for their number of days,
listed on the health page and taken only by the System Admin. `pg_dump` itself is the compose-level test's
(`deploy/tests/operations.sh`): here `pg_dump` is replaced, since the suite's PostgreSQL client may be older than the server."""
import datetime as dt
import os
from zoneinfo import ZoneInfo

import pytest
from django.test import Client

from accounts.models import Role
from audit.models import AuditEntry
from conftest import CROSS_SITE, SERVED_SITES, from_site
from ops import backups
from ops.models import TechnicalSettings

pytestmark = pytest.mark.django_db

# The real one, before the suite's `no_real_backups` puts a stand-in in its place for every test.
REAL_DUMP_DATABASE = backups.dump_database
RIYADH = ZoneInfo("Asia/Riyadh")
NON_ADMINS = [Role.CNO, Role.DEPUTY_CNO, Role.UNIT_MANAGER, Role.STAFF_NURSE]


def at(day, hour, minute=0):
    """A moment on the hospital's clock: 6 November 2026 at `hour`:`minute` is `at(6, hour, minute)`."""
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=RIYADH)


@pytest.fixture
def backups_dir(settings):
    """The test's own, from the suite's `no_real_backups`."""
    return settings.BACKUPS_DIR


@pytest.fixture
def dump_calls(monkeypatch):
    """Every call to `pg_dump`'s stand-in, as the file it wrote. Each writes a few bytes, as `pg_dump` writes the database."""
    calls = []

    def dump(out):
        out.write(b"PGDMP-custom-format")
        calls.append(out.name)
    monkeypatch.setattr(backups, "dump_database", dump)
    return calls


def backup_from(backups_dir, moment, size=10):
    """A backup taken at `moment`, as the job names it."""
    path = backups_dir / backups.name_for(moment)
    path.write_bytes(b"x" * size)
    return path


def names(backups_dir):
    return sorted(path.name for path in backups_dir.iterdir())


class TestTheNightlyBackup:
    def test_its_name_says_when_in_utc(self):
        assert backups.name_for(at(6, 2, 0)) == "nurse-roster-20261105T230000Z.dump"

    def test_the_first_is_taken_at_once(self, backups_dir, dump_calls):
        taken = backups.back_up_if_due(at(6, 14, 5))

        assert taken.name == "nurse-roster-20261106T110500Z.dump"
        assert names(backups_dir) == [taken.name]
        assert (backups_dir / taken.name).read_bytes() == b"PGDMP-custom-format"

    def test_none_is_due_before_the_backup_time_once_one_was_taken_since_the_last(self, backups_dir, dump_calls):
        backup_from(backups_dir, at(5, 2, 0))

        assert backups.back_up_if_due(at(6, 1, 55)) is None
        assert dump_calls == []

    def test_one_is_due_once_the_backup_time_has_passed(self, backups_dir, dump_calls):
        backup_from(backups_dir, at(5, 2, 0))

        taken = backups.back_up_if_due(at(6, 2, 0))

        assert taken.taken_at == at(6, 2, 0)

    def test_it_follows_the_system_admins_time(self, backups_dir, dump_calls):
        TechnicalSettings.objects.update_or_create(pk=1, defaults={"backup_time": dt.time(3, 30)})
        backup_from(backups_dir, at(5, 3, 30))

        assert backups.back_up_if_due(at(6, 3, 25)) is None
        assert backups.back_up_if_due(at(6, 3, 30)) is not None

    def test_a_worker_that_was_down_catches_up_once(self, backups_dir, dump_calls):
        backup_from(backups_dir, at(4, 2, 0))

        assert backups.back_up_if_due(at(6, 9, 0)) is not None
        assert backups.back_up_if_due(at(6, 9, 5)) is None
        assert len(dump_calls) == 1

    def test_the_file_is_written_whole_or_not_at_all(self, backups_dir, monkeypatch):
        def broken(out):
            out.write(b"half a backup")
            raise backups.BackupFailed("pg_dump: error: connection to server failed")
        monkeypatch.setattr(backups, "dump_database", broken)
        kept = backup_from(backups_dir, at(1, 2, 0))

        with pytest.raises(backups.BackupFailed):
            backups.back_up_if_due(at(6, 2, 0))

        assert names(backups_dir) == [kept.name]

    def test_only_the_system_admin_and_the_worker_can_read_one(self, backups_dir, dump_calls):
        taken = backups.back_up_if_due(at(6, 2, 0))

        assert os.stat(backups_dir / taken.name).st_mode & 0o777 == 0o600


    def test_one_taken_in_the_same_second_is_never_overwritten(self, backups_dir, dump_calls):
        earlier = backup_from(backups_dir, at(6, 2, 0), size=3)

        with pytest.raises(backups.BackupFailed, match="exists already"):
            backups.take(at(6, 2, 0))

        assert names(backups_dir) == [earlier.name] and earlier.read_bytes() == b"xxx"


class TestRetention:
    def test_backups_older_than_the_days_kept_go_after_a_new_one(self, backups_dir, dump_calls):
        TechnicalSettings.objects.update_or_create(pk=1, defaults={"backup_retention_days": 2})
        old = backup_from(backups_dir, at(3, 2, 0))
        kept = backup_from(backups_dir, at(4, 2, 0))
        newer = backup_from(backups_dir, at(5, 2, 0))

        taken = backups.back_up_if_due(at(6, 2, 0))

        assert names(backups_dir) == sorted([kept.name, newer.name, taken.name])
        assert not old.exists()

    def test_the_newest_is_kept_whatever_its_age(self, backups_dir):
        only = backup_from(backups_dir, at(1, 2, 0))

        backups.remove_expired(at(6, 2, 0), days=1)

        assert names(backups_dir) == [only.name]

    def test_a_file_a_killed_backup_left_half_written_goes_a_day_on(self, backups_dir, dump_calls):
        stale = backups_dir / ".nurse-roster-20261101T230000Z.dump.part"
        fresh = backups_dir / ".nurse-roster-20261105T110000Z.dump.part"
        for path, age_days in ((stale, 1.1), (fresh, 0.5)):
            path.write_bytes(b"half")
            moment = at(6, 2, 0).timestamp() - age_days * 86400
            os.utime(path, (moment, moment))

        taken = backups.back_up_if_due(at(6, 2, 0))

        assert names(backups_dir) == sorted([fresh.name, taken.name])

    def test_what_is_not_a_backup_is_left_alone(self, backups_dir, dump_calls):
        TechnicalSettings.objects.update_or_create(pk=1, defaults={"backup_retention_days": 1})
        (backups_dir / "copied-off-site.dump").write_bytes(b"theirs")
        os.utime(backups_dir / "copied-off-site.dump", (0, 0))

        taken = backups.back_up_if_due(at(6, 2, 0))

        assert names(backups_dir) == sorted(["copied-off-site.dump", taken.name])


class TestTheHealthPage:
    def test_it_lists_the_backups_newest_first_with_sizes_and_dates(self, signed_in, backups_dir):
        backup_from(backups_dir, at(4, 2, 0), size=1000)
        backup_from(backups_dir, at(5, 2, 0), size=2048)
        (backups_dir / ".nurse-roster-20261106T000000Z.dump.part").write_bytes(b"not yet")
        (backups_dir / "notes.txt").write_bytes(b"not a backup")

        health = signed_in().get("/api/ops/health").json()

        assert health["backups"] == [
            {"name": "nurse-roster-20261104T230000Z.dump", "size_bytes": 2048, "taken_at": "2026-11-05T02:00:00+03:00"},
            {"name": "nurse-roster-20261103T230000Z.dump", "size_bytes": 1000, "taken_at": "2026-11-04T02:00:00+03:00"},
        ]
        assert health["last_backup"] == "2026-11-05T02:00:00+03:00"

    def test_with_none_yet(self, signed_in):
        health = signed_in().get("/api/ops/health").json()

        assert (health["backups"], health["last_backup"]) == ([], None)

    def test_it_says_when_and_for_how_long_they_are_taken(self, signed_in):
        health = signed_in().get("/api/ops/health").json()

        assert health["backup_schedule"] == {"time": "02:00", "retention_days": 30}


class TestDownload:
    def url(self, name):
        return f"/api/ops/backups/{name}"

    def test_the_system_admin_takes_one(self, signed_in, backups_dir):
        path = backup_from(backups_dir, at(5, 2, 0), size=4096)

        r = signed_in().get(self.url(path.name))

        assert r.status_code == 200
        assert b"".join(r.streaming_content) == path.read_bytes()
        assert r["Content-Disposition"] == f'attachment; filename="{path.name}"'
        assert r["Cache-Control"] == "no-store"
        assert r["Content-Type"] == "application/octet-stream"

    def test_each_taking_is_audited_with_its_name_only(self, signed_in, backups_dir):
        path = backup_from(backups_dir, at(5, 2, 0))

        signed_in().get(self.url(path.name))

        entry = AuditEntry.objects.get(action="backup.downloaded")
        assert (entry.actor.username, entry.target, entry.before, entry.after) == ("someone", path.name, None, None)

    def test_not_from_another_site_nor_does_it_say_whether_the_name_exists(self, signed_in, backups_dir):
        """A backup's name is predictable, so a page on another site could aim a System Admin's browser at it."""
        path = backup_from(backups_dir, at(5, 2, 0))
        admin = signed_in()

        for name in (path.name, "nurse-roster-20261104T230000Z.dump"):
            response = admin.get(self.url(name), **from_site("cross-site"))
            assert (response.status_code, response.json()["detail"]) == (403, CROSS_SITE), name
        assert not AuditEntry.objects.filter(action="backup.downloaded").exists()

    @pytest.mark.parametrize("site", SERVED_SITES)
    def test_from_the_apps_own_pages_or_a_browser_that_does_not_say(self, signed_in, backups_dir, site):
        path = backup_from(backups_dir, at(5, 2, 0))

        assert signed_in().get(self.url(path.name), **from_site(site)).status_code == 200
        assert AuditEntry.objects.filter(action="backup.downloaded").count() == 1

    @pytest.mark.parametrize("role", NON_ADMINS)
    def test_no_other_role_may(self, signed_in, backups_dir, role):
        path = backup_from(backups_dir, at(5, 2, 0))

        r = signed_in(role).get(self.url(path.name))

        assert r.status_code == 403
        assert not AuditEntry.objects.filter(action="backup.downloaded").exists()

    def test_nor_anyone_signed_out(self, backups_dir):
        path = backup_from(backups_dir, at(5, 2, 0))

        assert Client().get(self.url(path.name)).status_code == 401

    @pytest.mark.parametrize("name", [
        "nurse-roster-20261104T230000Z.dump",  # none by that name
        ".nurse-roster-20261105T230000Z.dump.part",  # one still being written
        "notes.txt",
        "..%2Fsecret.dump",
        "%2E%2E",
    ])
    def test_only_a_backup_that_is_there_and_finished(self, signed_in, backups_dir, name):
        backup_from(backups_dir, at(6, 2, 0))
        (backups_dir / ".nurse-roster-20261105T230000Z.dump.part").write_bytes(b"not yet")
        (backups_dir / "notes.txt").write_bytes(b"not a backup")
        (backups_dir.parent / "secret.dump").write_bytes(b"outside")  # what "..%2Fsecret.dump" would reach

        assert signed_in().get(self.url(name)).status_code == 404


def test_a_backup_carries_no_session_that_works(monkeypatch):
    """A session is a working login, no part of the record: the table goes in empty, so a key read from a backup signs nobody
    in, and a restore signs everyone out. `pg_dump` itself is not run here (the module's docstring says why): its argv is."""
    import subprocess
    ran = []
    monkeypatch.setattr(backups.subprocess, "run",
                        lambda command, **_: ran.append(command) or subprocess.CompletedProcess(command, 0, b"", b""))

    with open(os.devnull, "wb") as out:
        REAL_DUMP_DATABASE(out)

    [command] = ran
    assert command[0] == "pg_dump"
    assert "--exclude-table-data=django_session" in command
    assert not any(part.startswith(("--exclude-table=", "-T")) for part in command)  # the table itself stays, empty


def test_the_job_runs_under_a_lock_of_its_own():
    from ops.tasks import backup
    assert (backup.lock, backup.queueing_lock) == ("backup", "backup")


def test_a_backup_on_demand_whatever_is_due(backups_dir):
    """`manage.py backup`, which `deploy/restore.sh` runs first, so the database as it stood can be had back."""
    import io

    from django.core.management import call_command
    backup_from(backups_dir, dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1))
    out = io.StringIO()

    call_command("backup", stdout=out)

    assert len(backups.list_backups()) == 2
    assert out.getvalue().strip() == backups.list_backups()[0].name
