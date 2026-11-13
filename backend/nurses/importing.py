"""Reading an import file, for every import (`read_file`), and judging every row of a nurse import against the rules adding
one nurse holds. Nothing here saves anything: `nurses.files_api` does, after checking what is confirmed is what was previewed.
The skills import's rows are judged in `nurses.skills_importing`.

How a large file behaves: a password hash takes about half a second on purpose, and the passwords may be kept nowhere,
so the worker cannot make them and hand them back later. Confirming therefore stays one long request, in one of
gunicorn's threads, which no timeout ends (deploy/compose.yaml); `MAX_ROWS` keeps the wait to minutes even on one
core. A hospital with more nurses imports them unit by unit. If that request dies before the transaction, nothing is
saved. If its answer is lost after the transaction, the nurses are saved and their passwords are gone: they are reset
one at a time, as the page says."""
from __future__ import annotations

import codecs
import csv
import datetime as dt
import hashlib
import io
import json
from dataclasses import dataclass, field

from django.db.models.functions import Lower
from pydantic import TypeAdapter, ValidationError

from accounts.api import FullName, Username
from accounts.models import User
from hospital.catalogue_api import FIRST_YEAR, LAST_YEAR, IsoDate
from nurses.models import Gender
from units.models import Unit

COLUMNS = ("Hospital ID", "Full name", "Gender", "Unit", "Start date", "On probation")
OPTIONAL_COLUMNS = 1  # How many of the last columns a file may leave out: "On probation", which came after the others.
MAX_ROWS = 500
MAX_BYTES = 1024 * 1024  # Far more than 500 rows need. Under FILE_UPLOAD_MAX_MEMORY_SIZE, so this message can be given.

_DELIMITERS = ",;\t"  # Excel uses the list separator of its locale, and a tab for "Unicode Text".
_GENDERS = {"f": Gender.FEMALE, "m": Gender.MALE}
# Empty means no. Excel writes TRUE and FALSE for a cell it took for a truth value.
_ON_PROBATION = {"": False, "no": False, "n": False, "false": False, "yes": True, "y": True, "true": True}
HOSPITAL_ID = TypeAdapter(Username)
_FULL_NAME = TypeAdapter(FullName)
_START_DATE = TypeAdapter(IsoDate)


class FileRefused(Exception):
    """The file as a whole cannot be read, so there are no rows to report on."""


@dataclass(frozen=True)
class Line:
    number: int
    cells: list[str]
    header_width: int | None = None  # Set when the cells do not match the header, which named this many columns.


@dataclass(frozen=True)
class Problem:
    column: str | None  # One of `Row`'s fields, or None for the whole line.
    message: str


@dataclass(frozen=True)
class NewNurse:
    hospital_id: str
    full_name: str
    gender: str
    unit: Unit
    start_date: dt.date
    on_probation: bool = False


@dataclass
class Row:
    """What the file says, for the page to show, and the nurse it means when there is no problem with it."""
    line: int
    hospital_id: str = ""
    full_name: str = ""
    gender: str = ""
    unit: str = ""
    start_date: str = ""
    on_probation: str = ""
    problems: list[Problem] = field(default_factory=list)
    nurse: NewNurse | None = None


@dataclass(frozen=True)
class Preview:
    rows: list[Row]
    token: str | None  # None when any row has a problem, so there is nothing to confirm.


def _decoded(raw: bytes) -> str:
    """UTF-8, with or without Excel's byte order mark, or UTF-16 with one. Any other encoding is refused, not guessed:
    the same bytes are valid Windows-1252 and Windows-1256 alike, and a wrong guess saves names nobody can read."""
    try:
        return raw.decode("utf-16" if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig")
    except UnicodeDecodeError:
        raise FileRefused("This is not a CSV file in UTF-8. In Excel, choose Save As and then “CSV UTF-8”.") from None


@dataclass(frozen=True)
class Layout:
    """What one kind of import file holds: its columns, and how its refusals name a line of it. Every import reads its
    file through `read_file`, under the same encodings and limits; `nurses.skills_importing` has the other one."""
    columns: tuple[str, ...]
    a_line: str  # "nurse": "one nurse per line".
    lines: str  # "nurses": "more than 500 nurses".
    split_by: str  # How to split a file over the limit, ending the sentence: "Split it, by unit for example, and …".
    quote_hint: str  # What to quote when a line's cells do not match the columns.
    optional: int = 0  # How many of the last columns a file may leave out, which then read as empty.
    together: bool = False  # Whether those columns are left out all together or not at all, as a file of an older template.

    def headers(self) -> list[tuple[str, ...]]:
        """The headers a file may have, the fullest first."""
        left_outs = (0, self.optional) if self.together else range(self.optional + 1)
        return [self.columns[:len(self.columns) - left_out] for left_out in left_outs]


NURSE_FILE = Layout(COLUMNS, "nurse", "nurses", ", by unit for example,", "a name that has a comma in it", OPTIONAL_COLUMNS)


def _trimmed(cells: list[str], columns: tuple[str, ...]) -> list[str]:
    """Excel pads every line to the longest one, so blank cells past the last column say nothing."""
    cells = list(cells)
    while len(cells) > len(columns) and not cells[-1].strip():
        cells.pop()
    return cells


def _is_blank(cells: list[str]) -> bool:
    return not any(cell.strip() for cell in cells)


def _is_the_header(cells: list[str], columns: tuple[str, ...]) -> bool:
    return [cell.strip().casefold() for cell in _trimmed(cells, columns)] == [column.casefold() for column in columns]


def _reader_after_header(text: str, layout: Layout):
    """The reader past the header, and the columns the header named."""
    columns = layout.columns
    left_out = columns[len(columns) - layout.optional:]
    if layout.together and layout.optional > 1:
        optional = f" {', '.join(left_out[:-1])} and {left_out[-1]} may be left out together."
    elif layout.optional > 1:
        optional = f" The last {layout.optional} may be left out."
    else:
        optional = f" The last, {columns[-1]}, may be left out." if layout.optional else ""
    wrong_columns = FileRefused(f"The first line must name exactly these columns: {', '.join(columns)}.{optional} "
                                "Download the template to start from.")
    for delimiter in _DELIMITERS:
        # Strict, or an unclosed quote would swallow the lines after it without a word.
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        try:
            first = next((cells for cells in reader if not _is_blank(cells)), None)
        except csv.Error:
            continue
        if first is None:
            raise FileRefused("This file is empty. Download the template to start from.")
        named = next((header for header in layout.headers() if _is_the_header(first, header)), None)
        if named is not None:
            return reader, named
    raise wrong_columns


def read_file(raw: bytes, layout: Layout = NURSE_FILE) -> list[Line]:
    """The file's lines below the header, blank ones left out. Raises `FileRefused`."""
    if len(raw) > MAX_BYTES:
        raise FileRefused(f"This file is larger than {MAX_BYTES // 1024 // 1024} MB, which is more than {MAX_ROWS} {layout.lines} need.")
    reader, named = _reader_after_header(_decoded(raw), layout)
    left_out = len(layout.columns) - len(named)
    # A line is padded to every column only when its cells match its header's, so a line of another count stays wrong.
    lines: list[Line] = []
    try:
        for cells in reader:
            if _is_blank(cells):
                continue
            if len(lines) == MAX_ROWS:
                raise FileRefused(f"This file has more than {MAX_ROWS} {layout.lines}. Split it{layout.split_by} and import each part.")
            cells = _trimmed(cells, named)
            if len(cells) == len(named):  # The columns the header left out read as empty.
                lines.append(Line(reader.line_num, cells + [""] * left_out))
            else:
                lines.append(Line(reader.line_num, cells, len(named)))
    except csv.Error:
        raise FileRefused(f"Line {reader.line_num} could not be read: check its quotation marks.") from None
    if not lines:
        raise FileRefused(f"This file has no {layout.lines} below its first line. Add one {layout.a_line} per line.")
    return lines


def wrong_cell_count(line: Line, layout: Layout) -> Problem | None:
    """The cells cannot be matched to the columns, so nothing can be said about any one of them."""
    if line.header_width is None and len(line.cells) == len(layout.columns):
        return None
    return Problem(None, f"This line has {len(line.cells)} cells, not {line.header_width or len(layout.columns)}. "
                         f"Put double quotes around {layout.quote_hint}.")


def parsed(adapter: TypeAdapter, value: str):
    """The value as the rule for adding one nurse leaves it, or None when that rule refuses it."""
    try:
        return adapter.validate_python(value)
    except ValidationError:
        return None


def _lines_by_hospital_id(lines: list[Line]) -> dict[str, list[int]]:
    """Each possible hospital ID in the file, in any case as `accounts_username_any_case` holds it, and where it is.
    Only possible ones, as the database is asked about these and cannot take a NUL."""
    seen: dict[str, list[int]] = {}
    for line in lines:
        hospital_id = parsed(HOSPITAL_ID, line.cells[0].strip()) if wrong_cell_count(line, NURSE_FILE) is None else None
        if hospital_id:
            seen.setdefault(hospital_id.lower(), []).append(line.number)
    return seen


@dataclass(frozen=True)
class _Standing:
    """What a row is judged against: the file's other lines and the database as it is now."""
    lines_by_id: dict[str, list[int]]
    taken: set[str]
    live_units: dict[str, Unit]
    archived_names: dict[str, str]


def _judged(line: Line, standing: _Standing) -> Row:
    if problem := wrong_cell_count(line, NURSE_FILE):
        row = Row(line.number, *(line.cells + [""] * len(COLUMNS))[:len(COLUMNS)])
        row.problems.append(problem)
        return row
    row = Row(line.number, *(cell.strip() for cell in line.cells))  # `Row`'s fields are in the order of `COLUMNS`.

    hospital_id = parsed(HOSPITAL_ID, row.hospital_id)
    if hospital_id is None:
        row.problems.append(Problem("hospital_id", "Use 1 to 150 letters, digits, dots, dashes and underscores."))
    else:
        others = [number for number in standing.lines_by_id[hospital_id.lower()] if number != row.line]
        if others:
            row.problems.append(Problem("hospital_id", f"{hospital_id} is also on line {others[0]}. Each nurse needs their own hospital ID."))
        if hospital_id.lower() in standing.taken:
            row.problems.append(Problem("hospital_id", f"{hospital_id} is already in use. Give this nurse another, or remove the line if they are already added."))

    full_name = parsed(_FULL_NAME, row.full_name)
    if full_name is None:
        row.problems.append(Problem("full_name", "Enter a full name of at most 200 characters."))

    gender = _GENDERS.get(row.gender.casefold())
    if gender is None:
        row.problems.append(Problem("gender", "Use F or M."))

    unit = standing.live_units.get(row.unit.lower())
    if unit is None:
        name = standing.archived_names.get(row.unit.lower())
        row.problems.append(Problem("unit", f"{name} is archived, so no nurse can join it. Choose a unit that is not archived."
                                    if name else "There is no such unit. Use its name on the Units page."))
    else:
        row.unit = unit.name

    start_date = parsed(_START_DATE, row.start_date)
    if start_date is None:
        row.problems.append(Problem("start_date", f"Use a date as YYYY-MM-DD, between {FIRST_YEAR} and {LAST_YEAR}."))

    on_probation = _ON_PROBATION.get(row.on_probation.casefold())
    if on_probation is None:
        row.problems.append(Problem("on_probation", "Use Yes or No, or leave it empty for no."))

    if not row.problems:
        row.gender = gender
        row.on_probation = "Yes" if on_probation else "No"
        row.nurse = NewNurse(hospital_id, full_name, gender, unit, start_date, on_probation)
    return row


def _token(rows: list[Row]) -> str:
    """Stands for what these rows mean right now, the unit's identity with its name, so that a unit archived and
    another opened under its name is a different preview."""
    meant = [(row.nurse.hospital_id, row.nurse.full_name, row.nurse.gender, str(row.nurse.unit.pk), row.nurse.unit.name,
              row.nurse.start_date.isoformat(), row.nurse.on_probation) for row in rows]
    return hashlib.sha256(json.dumps(meant).encode()).hexdigest()


def check(lines: list[Line]) -> Preview:
    """Every row's problems as things stand. It writes nothing and holds no lock."""
    seen = _lines_by_hospital_id(lines)
    taken = set(User.objects.annotate(lowered=Lower("username")).filter(lowered__in=seen)
                .values_list("lowered", flat=True))
    all_units = list(Unit.objects.all())
    units = {unit.name.lower(): unit for unit in all_units if not unit.archived}
    archived = {unit.name.lower(): unit.name for unit in all_units if unit.archived}
    standing = _Standing(seen, taken, units, archived)
    rows = [_judged(line, standing) for line in lines]
    return Preview(rows, None if any(row.problems for row in rows) else _token(rows))
