"""A roster version's files and its print sheet, for the unit's manager and for the CNO and the Deputy CNO, any version of the
month (`rosters.sheets` is the table they all are). Each file taken is audited, as the staff list is: who took which roster and
version, and nothing from the file. The sheet the print page reads is not, since it makes no file and shows only what the reader
can already read on the Roster page. A nurse's own sheet is `rosters.reading_api`'s.

A draft has no version, so it has no file: only what has been approved is printed or sent on."""
from __future__ import annotations

import datetime as dt
from typing import Literal
from uuid import UUID

from django.http import HttpRequest, HttpResponse
from django.utils.text import slugify
from ninja import Field, Query, Router
from ninja.errors import HttpError

from accounts.authz import for_a_file
from accounts.models import User
from audit.log import record
from nurses.spreadsheets import XLSX, csv_response, xlsx_response
from rosters import review
from rosters.api import reader
from rosters.models import Roster, RosterVersion
from rosters.sheets import RosterSheet, Sheet, as_schema, sheet_of
from solves.api import MonthFilter, chosen_month
from units.scope import HOSPITAL_WIDE, unit_in_scope

# `reader`, as for reading the roster: a nurse reads only their own unit's roster in force, on My roster and its print page.
router = Router(tags=["rosters"])
reader_for_files = for_a_file(reader)  # Not from another site.

_A_FILE = {"responses": {200: {"description": "A CSV file or an Excel workbook, as `format` asks.", "content": {
    "text/csv": {"schema": {"type": "string"}},
    XLSX: {"schema": {"type": "string", "format": "binary"}}}}}}


class VersionFilter(MonthFilter):
    version: int = Field(ge=1, description="The version's number. Any version of the month, not only the one in force.")


class ExportFilter(VersionFilter):
    format: Literal["csv", "xlsx"]


def version_for(user: User, unit_id: UUID, month: dt.date, number: int) -> RosterVersion:
    """Through the unit's scope, so another unit's manager is refused. The CNO and the Deputy CNO read a roster only while it is
    submitted or approved (`review.SEEN_BY_APPROVERS`); only an approved one has versions, and it never goes back, so the hold
    is a second guard rather than a case that happens."""
    unit = unit_in_scope(user, unit_id)
    roster = Roster.objects.select_related("unit").filter(unit=unit, month=month).first()
    seen = roster is not None and (user.role not in HOSPITAL_WIDE or roster.state in review.SEEN_BY_APPROVERS)
    version = roster.versions.filter(number=number).first() if seen else None
    if version is None:
        raise HttpError(404, f"This month's roster has no version {number}.")
    return version


def _filename(version: RosterVersion, extension: str) -> str:
    roster = version.roster
    return f"roster-{slugify(roster.unit.name, allow_unicode=True) or 'unit'}-{roster.month:%Y-%m}-v{version.number}.{extension}"


def _title(sheet: Sheet) -> str:
    roster = sheet.version.roster
    return f"{roster.unit.name} roster, {roster.month:%B %Y}, version {sheet.version.number}"


@router.get("/{unit_id}/roster/export", auth=reader_for_files, operation_id="exportRosterVersion", openapi_extra=_A_FILE)
def export_version(request: HttpRequest, unit_id: UUID, filters: Query[ExportFilter]) -> HttpResponse:
    month = chosen_month(filters.month)
    version = version_for(request.user, unit_id, month, filters.version)
    sheet = sheet_of(version)
    # Who took which roster and version, and nothing from the file: the System Admin reads the log, not the file.
    record("roster.exported", actor=request.user, target=version.roster.audit_target, request=request,
           after={"version": version.number, "format": filters.format})
    filename = _filename(version, filters.format)
    if filters.format == "csv":
        return csv_response(filename, sheet.table())
    # The names wide enough to read, a date as wide as YYYY-MM-DD; the header row and the two name columns stay in view.
    widths = {0: 14, 1: 28, **{2 + index: 11 for index in range(len(sheet.days))}}
    if sheet.any_on_probation:  # The last column, after the dates, the hours and a count a shift, names each preceptor.
        widths[2 + len(sheet.days) + 1 + len(sheet.shifts)] = 28
    return xlsx_response(filename, _title(sheet), "Roster", sheet.table(), frozen=(1, 2), widths=widths)


@router.get("/{unit_id}/roster/sheet", response=RosterSheet, auth=reader, operation_id="getRosterVersionSheet")
def version_sheet(request: HttpRequest, unit_id: UUID, filters: Query[VersionFilter]) -> RosterSheet:
    """What the print page shows: the same table as the files. Not audited, as it makes no file and shows only what the reader
    can already read on the Roster page."""
    return as_schema(sheet_of(version_for(request.user, unit_id, chosen_month(filters.month), filters.version)))
