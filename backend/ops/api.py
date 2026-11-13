from datetime import datetime, time
from typing import Annotated, Literal

from django.db import transaction
from django.http import FileResponse, Http404, HttpRequest
from ninja import Router, Schema
from pydantic import Field

from accounts.authz import SignedIn, for_a_file
from accounts.models import Role
from audit.log import record
from ops import backups
from ops.models import TechnicalSettings
from ops.public_health import HeldAnswer
from ops.status import database_reachable, database_size_bytes, disk_usage, push_state, queue_state, solver_state

router = Router()
system_admin = SignedIn(Role.SYSTEM_ADMIN)
system_admin_for_files = for_a_file(system_admin)  # Not from another site.


class SolverHealth(Schema):
    reachable: bool
    version: str | None = None
    workers: int | None = None


class PublicSolverHealth(Schema):
    reachable: bool


class Health(Schema):
    """Anyone's to read, so it says only what the compose health check and an install check need: the solver's version and
    thread count are the System Admin's (`/api/ops/health`)."""
    status: str
    database: bool
    solver: PublicSolverHealth


def _solver() -> SolverHealth:
    health = solver_state()
    if health is None:
        return SolverHealth(reachable=False)
    return SolverHealth(reachable=True, version=health.get("version"), workers=health.get("workers"))


def _health_now() -> Health:
    database, solver = database_reachable(), _solver()
    return Health(status="ok" if database and solver.reachable else "degraded", database=database,
                  solver=PublicSolverHealth(reachable=solver.reachable))


# Held five seconds in this process; `/api/ops/health` and `/metrics` are never held.
public_health = HeldAnswer(_health_now)


@router.get("/health", response=Health, auth=None, operation_id="getHealth")
def health(request) -> Health:
    """Always 200 while this process is alive; the body says what it can reach."""
    return public_health()


class Disk(Schema):
    total_bytes: int
    free_bytes: int


class Queue(Schema):
    waiting: int
    running: int
    failed: int = Field(description="Failed jobs kept: those of the last 30 days (`ops.queue.ENDED_JOBS_KEPT`).")
    workers: int


class BackupRow(Schema):
    name: str = Field(description="The file's name, which `GET /api/ops/backups/{name}` takes and `deploy/restore.sh` restores.")
    size_bytes: int
    taken_at: datetime


class BackupSchedule(Schema):
    time: str = Field(description="HH:MM on the hospital's clock.")
    retention_days: int


class PushHealth(Schema):
    sent: int = Field(description="Sends a push service accepted in the last 24 hours, one a push subscription a try.")
    failed: int = Field(description="Sends that came to nothing in the last 24 hours, every try again counted: attempts, not "
                                    "jobs or pushes.")
    last_failure: Literal["unreachable", "refused", "gone"] | None = Field(
        description="Why the last of those failed: the push service could not be reached (or answered 429 or 5xx), refused "
                    "the push, or said the subscription is gone. Null when none failed in the last 24 hours.")


class InstallationHealth(Schema):
    database: bool
    solver: SolverHealth
    database_size_bytes: int
    disk: Disk | None
    queue: Queue
    push: PushHealth
    last_backup: datetime | None = Field(description="When the newest backup was taken. Null before the first.")
    backups: list[BackupRow] = Field(description="Every backup kept, newest first.")
    backup_schedule: BackupSchedule


@router.get("/ops/health", response=InstallationHealth, auth=system_admin, operation_id="getInstallationHealth")
def installation_health(request) -> InstallationHealth:
    kept = backups.list_backups()
    newest = backups.newest(kept)
    technical = TechnicalSettings.load()
    return InstallationHealth(database=database_reachable(), solver=_solver(),
                              database_size_bytes=database_size_bytes(), disk=disk_usage(), queue=queue_state(), push=push_state(),
                              last_backup=newest.taken_at if newest else None,
                              backups=[BackupRow(name=b.name, size_bytes=b.size_bytes, taken_at=b.taken_at) for b in kept],
                              backup_schedule=BackupSchedule(time=technical.backup_time.strftime("%H:%M"),
                                                             retention_days=technical.backup_retention_days))


_A_BACKUP_FILE = {"responses": {200: {"description": "The backup, in `pg_dump`'s custom format.", "content": {
    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}}}


@router.get("/ops/backups/{name}", auth=system_admin_for_files, operation_id="downloadBackup", openapi_extra=_A_BACKUP_FILE)
def download_backup(request: HttpRequest, name: str) -> FileResponse:
    """A backup holds everything: password hashes and every nurse's leave. So only the System Admin takes one, each taking is
    logged with its name and nothing else, and no copy is kept by a browser's cache."""
    path = backups.file_of(name)
    if path is None:
        raise Http404("No such backup.")
    response = FileResponse(path.open("rb"), as_attachment=True, filename=name, content_type="application/octet-stream")
    response["Cache-Control"] = "no-store"
    record("backup.downloaded", actor=request.user, target=name, request=request)
    return response


ClockTime = Annotated[str, Field(pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$", examples=["02:00"])]


class TechnicalSettingsSchema(Schema):
    time_limit_cap_seconds: int = Field(ge=1, le=86400)
    backup_time: ClockTime
    backup_retention_days: int = Field(ge=1, le=3650)
    close_time: ClockTime


def _as_dict(row: TechnicalSettings) -> dict:
    return {"time_limit_cap_seconds": row.time_limit_cap_seconds, "backup_time": row.backup_time.strftime("%H:%M"),
            "backup_retention_days": row.backup_retention_days, "close_time": row.close_time.strftime("%H:%M")}


@router.get("/ops/technical-settings", response=TechnicalSettingsSchema, auth=system_admin,
            operation_id="getTechnicalSettings")
def get_technical_settings(request) -> dict:
    return _as_dict(TechnicalSettings.load())


@router.put("/ops/technical-settings", response=TechnicalSettingsSchema, auth=system_admin,
            operation_id="updateTechnicalSettings")
def update_technical_settings(request, body: TechnicalSettingsSchema) -> dict:
    with transaction.atomic():
        row = TechnicalSettings.load(for_update=True)
        before = _as_dict(row)
        after = body.model_dump()
        if after != before:
            row.time_limit_cap_seconds = body.time_limit_cap_seconds
            row.backup_time = time.fromisoformat(body.backup_time)
            row.backup_retention_days = body.backup_retention_days
            row.close_time = time.fromisoformat(body.close_time)
            row.save()
            record("technical_settings.changed", actor=request.user, target="technical settings",
                   before=before, after=after, request=request)
    return after
