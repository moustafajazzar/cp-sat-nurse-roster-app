"""The nightly backup: a `pg_dump` of the whole database in its custom format, which
`deploy/restore.sh` gives to `pg_restore`, into the `backups` volume at `BACKUPS_DIR`.

The files are the record: what is listed, what is the last backup and what is kept are read from the directory, never from a
table, so a restored database cannot disagree with the disk. A backup holds everything, password hashes and every nurse's
leave included, so it reaches nobody but the System Admin (`ops.api`), and only `web` and `worker` mount the volume. Everything
but the sessions: their table goes in empty, since a session's key works as a login as it stands."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from django.conf import settings
from django.utils import timezone

from ops.models import TechnicalSettings

logger = logging.getLogger(__name__)

_PREFIX = "nurse-roster-"
_STAMP = "%Y%m%dT%H%M%SZ"
# The name says when, in UTC, so it means one moment whatever the hospital's clock does. Only these are listed, served or removed.
_NAME = re.compile(r"^nurse-roster-(\d{8}T\d{6}Z)\.dump$")
# What `take` writes to until a backup is whole. One a killed backup left behind goes after a day, never one still being written.
_PARTIAL = re.compile(r"^\.nurse-roster-\d{8}T\d{6}Z\.dump\.part$")
_PARTIAL_KEPT = dt.timedelta(days=1)


class BackupFailed(Exception):
    """`pg_dump` did not finish; its own words, which name no row."""


@dataclass(frozen=True)
class Backup:
    name: str
    size_bytes: int
    taken_at: dt.datetime


def name_for(moment: dt.datetime) -> str:
    return f"{_PREFIX}{moment.astimezone(dt.UTC).strftime(_STAMP)}.dump"


def _directory() -> Path:
    return Path(settings.BACKUPS_DIR)


def _taken_at(name: str) -> dt.datetime | None:
    match = _NAME.match(name)
    return dt.datetime.strptime(match[1], _STAMP).replace(tzinfo=dt.UTC) if match else None


def list_backups() -> list[Backup]:
    """Every finished backup, newest first. A file being written has another name until it is whole."""
    found = []
    try:
        entries = list(os.scandir(_directory()))
    except OSError:
        return []
    for entry in entries:
        taken_at = _taken_at(entry.name)
        if taken_at is not None and entry.is_file(follow_symlinks=False):
            found.append(Backup(entry.name, entry.stat().st_size, timezone.localtime(taken_at)))
    return sorted(found, key=lambda backup: backup.taken_at, reverse=True)


def newest(kept: list[Backup] | None = None) -> Backup | None:
    """The newest of `kept`, as `list_backups` orders them, or of every backup when not given."""
    kept = list_backups() if kept is None else kept
    return kept[0] if kept else None


def file_of(name: str) -> Path | None:
    """The file of a finished backup by that name, or None: nothing else in or outside the directory is ever served."""
    if not _NAME.match(name):
        return None
    path = _directory() / name
    return path if path.is_file() and not path.is_symlink() else None


def last_due(now: dt.datetime, at: dt.time) -> dt.datetime:
    """The latest moment, at or before `now`, at which the hospital's clock read `at`."""
    zone = timezone.get_current_timezone()
    today = timezone.localtime(now, zone).date()
    due = dt.datetime.combine(today, at, tzinfo=zone)
    return due if due <= now else dt.datetime.combine(today - dt.timedelta(days=1), at, tzinfo=zone)


def is_due(now: dt.datetime) -> bool:
    """None since the backup time last came round, so a worker that was down catches up once. With none at all, one is due at
    once: a new installation has a backup from its first hour."""
    latest = newest()
    return latest is None or latest.taken_at < last_due(now, TechnicalSettings.load().backup_time)


def dump_database(out: BinaryIO) -> None:
    """The whole database, in `pg_dump`'s custom format, but the sessions' rows. The worker's image carries the client of the
    server's version."""
    database = settings.DATABASES["default"]
    # A session is a working login, no part of the record: its key read from a backup would sign its holder in as that user.
    # The table goes in empty, so a restore signs everyone out.
    command = ["pg_dump", "--format=custom", "--no-password", "--exclude-table-data=django_session",
               "--host", database["HOST"], "--port", str(database["PORT"]),
               "--username", database["USER"], "--dbname", database["NAME"]]
    finished = subprocess.run(command, stdout=out, stderr=subprocess.PIPE, env={**os.environ, "PGPASSWORD": database["PASSWORD"]},
                              check=False)
    if finished.returncode != 0:
        raise BackupFailed(finished.stderr.decode(errors="replace").strip() or f"pg_dump ended with {finished.returncode}")


def take(now: dt.datetime) -> Backup:
    """A backup now, written under a name nothing lists and given its own once whole. Readable by its owner only: the worker
    and `web`, which run as the same user."""
    name = name_for(now)
    final = _directory() / name
    partial = _directory() / f".{name}.part"
    try:
        with open(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
            dump_database(out)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(partial, final)  # never over another: one taken in the same second stays as it was
        except FileExistsError as e:
            raise BackupFailed(f"{name} exists already.") from e
    finally:
        partial.unlink(missing_ok=True)
    backup = Backup(name, final.stat().st_size, timezone.localtime(now))
    logger.info("backup taken", extra={"backup": name, "size_bytes": backup.size_bytes})
    return backup


def remove_expired(now: dt.datetime, *, days: int) -> list[str]:
    """Backups taken more than `days` ago, except the newest, which stays whatever its age: a backup that keeps failing must
    not leave none. Also a half-written file a killed backup left, a day on."""
    removed = []
    for backup in list_backups()[1:]:
        if backup.taken_at < now - dt.timedelta(days=days):
            (_directory() / backup.name).unlink(missing_ok=True)
            removed.append(backup.name)
    for entry in os.scandir(_directory()):
        if _PARTIAL.match(entry.name) and entry.stat().st_mtime < (now - _PARTIAL_KEPT).timestamp():
            Path(entry.path).unlink(missing_ok=True)
            removed.append(entry.name)
    if removed:
        logger.info("backups removed", extra={"backups": removed})
    return removed


def back_up_if_due(now: dt.datetime | None = None) -> Backup | None:
    """The job's work: a backup if one is due, then the old ones go. A failed backup removes nothing."""
    now = now or timezone.now()
    if not is_due(now):
        return None
    backup = take(now)
    remove_expired(now, days=TechnicalSettings.load().backup_retention_days)
    return backup
