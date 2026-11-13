"""The files about nurses: the nurse import (a template, a preview and a confirm) and the staff list, the CNO's; the skills
import (the same three), the CNO's and the Deputy CNO's, since both record what a nurse holds.
Reading a file and judging a nurse import is `nurses.importing`, judging a skills import `nurses.skills_importing`.
Who belongs where is `nurses.membership`."""
from __future__ import annotations

import datetime as dt
from collections import Counter

from django.db import transaction
from django.http import HttpRequest, HttpResponse
from ninja import Field, File, Form, Router, Schema, Status
from ninja.errors import HttpError
from ninja.files import UploadedFile

from accounts.authz import SignedIn, for_a_file
from accounts.models import Role, User
from accounts.provisioning import UsernameTaken, create_user, new_passwords
from audit.log import record
from hospital.api import refuse
from hospital.models import HospitalSettings, SkillKind
from nurses import importing, membership, skills_importing
from nurses.api import new_profile, probation_of, with_units
from nurses.models import NurseProfile
from nurses.skills import record_anew
from nurses.spreadsheets import csv_response
from units.models import Unit

router = Router(tags=["nurses"])
cno = SignedIn(Role.CNO)
cno_for_files = for_a_file(cno)  # Not from another site.
recorders = SignedIn(Role.CNO, Role.DEPUTY_CNO)
recorders_for_files = for_a_file(recorders)

STAFF_LIST_COLUMNS = (*importing.COLUMNS, "Probation's last day", "End date", "Role", "Status", "Transfers to come")
_A_CSV_FILE = {"responses": {200: {"description": "A CSV file.", "content": {"text/csv": {"schema": {"type": "string"}}}}}}


class ImportProblem(Schema):
    column: str | None
    message: str


class ImportRow(Schema):
    line: int
    hospital_id: str
    full_name: str
    gender: str
    unit: str
    start_date: str
    on_probation: str = Field(description="As the file has it, or Yes or No once the line has no problem.")
    problems: list[ImportProblem]


class ImportPreview(Schema):
    rows: list[ImportRow]
    token: str | None


class SheetRow(Schema):
    hospital_id: str
    full_name: str
    unit: str
    generated_password: str


class PasswordSheet(Schema):
    """Holds passwords, so it asks not to be stored."""
    rows: list[SheetRow]


class _ChangedSincePreview(Exception):
    pass


def _lines_of(file: UploadedFile, layout: importing.Layout = importing.NURSE_FILE) -> list[importing.Line]:
    try:
        return importing.read_file(file.read(), layout)
    except importing.FileRefused as refusal:
        refuse((("file",), str(refusal)))


@router.get("/import/template", auth=cno_for_files, operation_id="downloadNurseImportTemplate", openapi_extra=_A_CSV_FILE)
def import_template(request: HttpRequest) -> HttpResponse:
    return csv_response("nurse-import-template.csv", [importing.COLUMNS])


@router.post("/import/preview", response=ImportPreview, auth=cno, operation_id="previewNurseImport")
def preview_import(request: HttpRequest, response: HttpResponse, file: File[UploadedFile]) -> importing.Preview:
    """Every row's problems. It saves nothing and keeps nothing: confirming sends the file again."""
    response["Cache-Control"] = "no-store"
    return importing.check(_lines_of(file))


def _create(request: HttpRequest, nurses: list[importing.NewNurse]) -> list[SheetRow]:
    """Hashes first, holding no lock, since it is the slow part. Then one transaction, in the order every other change
    takes its locks: users, then units. A hospital ID taken meanwhile is the constraint's to catch, and a unit changed
    meanwhile shows under its lock, and a CNO deactivated meanwhile adds nobody. Users go in by hospital ID, so that
    two imports of the same IDs wait for each other instead of deadlocking on the unique index."""
    passwords = dict(zip((nurse.hospital_id for nurse in nurses), new_passwords(len(nurses))))
    with transaction.atomic():
        if not User.objects.filter(pk=request.user.pk, role=Role.CNO, is_active=True).exists():
            raise HttpError(403, "Your role may not do this.")
        try:
            users = {nurse.hospital_id: create_user(request, username=nurse.hospital_id, full_name=nurse.full_name,
                                                    role=Role.STAFF_NURSE, password=passwords[nurse.hospital_id])[0]
                     for nurse in sorted(nurses, key=lambda nurse: nurse.hospital_id.lower())}
        except UsernameTaken:
            raise _ChangedSincePreview from None
        previewed = {nurse.unit.pk: nurse.unit for nurse in nurses}
        locked = {unit.pk: unit for unit in Unit.objects.select_for_update().filter(pk__in=previewed).order_by("pk")}
        if any(pk not in locked or locked[pk].archived or locked[pk].name != unit.name for pk, unit in previewed.items()):
            raise _ChangedSincePreview
        record("nurses.imported", actor=request.user, request=request,
               after={"count": len(nurses), "units": Counter(nurse.unit.name for nurse in nurses)})
        for nurse in nurses:
            new_profile(request, users[nurse.hospital_id], locked[nurse.unit.pk], nurse.gender, nurse.start_date,
                        on_probation=nurse.on_probation)
    return [SheetRow(hospital_id=nurse.hospital_id, full_name=nurse.full_name, unit=nurse.unit.name,
                     generated_password=passwords[nurse.hospital_id].plain) for nurse in nurses]


@router.post("/import/confirm", response={201: PasswordSheet, 409: ImportPreview}, auth=cno, operation_id="confirmNurseImport")
def confirm_import(request: HttpRequest, response: HttpResponse, file: File[UploadedFile], token: Form[str]) -> Status:
    """`token` is the preview's. The file is judged again from the start: with any problem, or when it no longer means
    what was previewed, nothing is saved and the answer is 409 with the preview as it now stands."""
    response["Cache-Control"] = "no-store"
    lines = _lines_of(file)
    preview = importing.check(lines)
    if preview.token is None or preview.token != token:
        return Status(409, preview)
    try:
        return Status(201, PasswordSheet(rows=_create(request, [row.nurse for row in preview.rows])))
    except _ChangedSincePreview:
        return Status(409, importing.check(lines))


# ---- the skills import -----------------------------------------------------------------------------------------------
class SkillImportRow(Schema):
    line: int
    hospital_id: str
    skill: str
    expiry: str = Field(description="As the file has it.")
    identifier: str = Field(description="As the file has it, trimmed when it is one; empty in a file of three columns.")
    issue_date: str = Field(description="As the file has it; empty in a file of three columns.")
    full_name: str = Field(description="The nurse's, when the hospital ID is hers; empty otherwise.")
    kind: SkillKind | None = Field(description="The catalogue's, when the skill is one; the page groups the rows by it.")
    change: skills_importing.SkillImportChange | None = Field(
        description="What confirming does with a row that has no problem: records the skill, renews the one she holds with the "
                    "row's expiry, identifier and issue date, or nothing, since the file says what is already recorded.")
    held_expiry: dt.date | None = Field(description="The expiry a renewal replaces.")
    held_identifier: str | None = Field(description="The identifier a renewal replaces, or the one recorded.")
    held_issue_date: dt.date | None = Field(description="The issue date a renewal replaces, or the one recorded.")
    problems: list[ImportProblem]


class SkillImportPreview(Schema):
    rows: list[SkillImportRow]
    token: str | None


class SkillImportDone(Schema):
    recorded: int
    renewed: int
    unchanged: int


@router.get("/skills-import/template", auth=recorders_for_files, operation_id="downloadSkillsImportTemplate", openapi_extra=_A_CSV_FILE)
def skills_import_template(request: HttpRequest) -> HttpResponse:
    return csv_response("skills-import-template.csv", [skills_importing.COLUMNS])


@router.post("/skills-import/preview", response=SkillImportPreview, auth=recorders, operation_id="previewSkillsImport")
def preview_skills_import(request: HttpRequest, response: HttpResponse, file: File[UploadedFile]) -> skills_importing.Preview:
    """Every row's problems, and what each row would do. It saves nothing and keeps nothing: confirming sends the file again."""
    response["Cache-Control"] = "no-store"
    return skills_importing.check(_lines_of(file, skills_importing.SKILLS_FILE))


def _save_skills(request: HttpRequest, lines: list[importing.Line], token: str) -> SkillImportDone:
    """Under the locks every skill write takes, in their order: the catalogue's, then the nurses' (`nurses.skills_api`).
    The file is judged again under them, so nothing can change between that judgement and the writes."""
    with transaction.atomic():
        if not User.objects.filter(pk=request.user.pk, role__in=recorders.roles, is_active=True).exists():
            raise HttpError(403, "Your role may not do this.")
        HospitalSettings.load(for_update=True)
        list(skills_importing.nurses_named(lines).select_for_update(of=("self", "user"), no_key=True)
             .select_related("user").order_by("pk"))
        preview = skills_importing.check(lines)
        if preview.token is None or preview.token != token:
            raise _ChangedSincePreview
        meanings = [row.meaning for row in preview.rows]
        for meaning in meanings:
            if meaning.change == skills_importing.SkillImportChange.NONE:
                continue
            record_anew(meaning.nurse, meaning.skill, request.user, expiry=meaning.kept_expiry, ending=meaning.held,
                        identifier=meaning.identifier, issue_date=meaning.issue_date)
        changes = Counter(meaning.change for meaning in meanings)
        # Who and how many rows, and nothing from the file, not even whose skills: the System Admin reads the log, not the file.
        record("nurse.skills_imported", actor=request.user, request=request, after={"count": len(meanings)})
    done = skills_importing.SkillImportChange
    return SkillImportDone(recorded=changes[done.RECORD], renewed=changes[done.RENEW], unchanged=changes[done.NONE])


@router.post("/skills-import/confirm", response={200: SkillImportDone, 409: SkillImportPreview}, auth=recorders,
             operation_id="confirmSkillsImport")
def confirm_skills_import(request: HttpRequest, response: HttpResponse, file: File[UploadedFile], token: Form[str]) -> Status:
    """`token` is the preview's. The file is judged again from the start: with any problem, or when it no longer means
    what was previewed, nothing is saved and the answer is 409 with the preview as it now stands."""
    response["Cache-Control"] = "no-store"
    lines = _lines_of(file, skills_importing.SKILLS_FILE)
    preview = skills_importing.check(lines)
    if preview.token is None or preview.token != token:
        return Status(409, preview)
    try:
        return Status(200, _save_skills(request, lines, token))
    except _ChangedSincePreview:
        return Status(409, skills_importing.check(lines))


# ---- the staff list -------------------------------------------------------------------------------------------------
def _staff_list_row(nurse: NurseProfile) -> list[str]:
    current, *coming = membership.from_now_on(nurse)
    user = nurse.user
    on_probation = probation_of(nurse)  # Only one not over yet: "who is on probation and until when".
    return [user.username, user.full_name, nurse.gender, current.unit.name, nurse.start_date.isoformat(),
            "Yes" if on_probation else "", on_probation.last_day.isoformat() if on_probation else "",
            nurse.end_date.isoformat() if nurse.end_date else "", user.get_role_display(),
            "Active" if user.is_active else "Deactivated",
            "; ".join(f"{placement.unit.name} from {placement.from_month:%Y-%m}" for placement in coming)]


@router.get("/export", auth=cno_for_files, operation_id="exportStaffList", openapi_extra=_A_CSV_FILE)
def export_staff_list(request: HttpRequest) -> HttpResponse:
    """Every nurse the CNO's list shows, deactivated ones too, by name. Its first columns are the import's."""
    rows = [_staff_list_row(nurse) for nurse in with_units(NurseProfile.objects.all())]
    # That it was taken and how many it held, and nothing from the file: the System Admin reads the log, not the file.
    record("nurses.exported", actor=request.user, request=request, after={"count": len(rows)})
    return csv_response(f"staff-list-{membership.today():%Y-%m-%d}.csv", [STAFF_LIST_COLUMNS, *rows])
