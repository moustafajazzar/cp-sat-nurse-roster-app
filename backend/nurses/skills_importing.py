"""Judging every row of a skills import file, against the rules recording or renewing one skill holds (`nurses.skills_api`).
The file is read as every import's is (`nurses.importing.read_file`); nothing here saves anything: `nurses.files_api` does,
after judging the file again under the locks a skill write takes.

A row says the whole record: its expiry, identifier and issue date, an empty cell being none, and last month's file of
three columns having none of the two. A row for a skill the nurse does not hold records it; one for a skill she holds that
differs from her record in any of the three is a renewal; one that says what is already recorded changes nothing.
Confirming is quick (no passwords), so it is one short request."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum

from uuid import UUID

from django.db.models.functions import Lower
from pydantic import TypeAdapter

from hospital.catalogue_api import FIRST_YEAR, LAST_YEAR, IsoDate
from hospital.models import Skill
from nurses.importing import HOSPITAL_ID, Layout, Line, Problem, parsed, wrong_cell_count
from nurses.models import NurseProfile, NurseSkill
from nurses.skills import Identifier, counted_expiry, problems_with

# Identifier and Issue date came after the others: last month's file of three columns is still read.
COLUMNS = ("Hospital ID", "Skill", "Expiry", "Identifier", "Issue date")
SKILLS_FILE = Layout(COLUMNS, "skill", "skills", "", "a skill name that has a comma in it", optional=2, together=True)

_DATE = TypeAdapter(IsoDate)
_IDENTIFIER = TypeAdapter(Identifier | None)
_DATE_WORDS = f"Use a date as YYYY-MM-DD, between {FIRST_YEAR} and {LAST_YEAR}."


class SkillImportChange(StrEnum):
    """What confirming does with a row that has no problem."""
    RECORD = "record"
    RENEW = "renew"
    NONE = "none"


@dataclass(frozen=True)
class RowMeaning:
    """What a row means when there is no problem with it."""
    nurse: NurseProfile
    skill: Skill
    expiry: dt.date | None
    identifier: str | None
    issue_date: dt.date | None
    held: NurseSkill | None  # Her current record of the skill, which a renewal ends.

    @property
    def change(self) -> SkillImportChange:
        if self.held is None:
            return SkillImportChange.RECORD
        recorded = (counted_expiry(self.held), self.held.identifier, self.held.issue_date)
        return SkillImportChange.NONE if recorded == (self.expiry, self.identifier, self.issue_date) else SkillImportChange.RENEW

    @property
    def kept_expiry(self) -> dt.date | None:
        """The expiry to write: a kind that no longer expires keeps the date recorded while it did, as a renewal by hand
        keeps it (`nurses.skills_api.renew_skill`)."""
        return self.expiry if self.skill.expires or self.held is None else self.held.expiry


@dataclass
class Row:
    """What the file says, for the page to show, and what it means when there is no problem with it."""
    line: int
    hospital_id: str = ""
    skill: str = ""
    expiry: str = ""
    identifier: str = ""
    issue_date: str = ""
    full_name: str = ""
    kind: Skill.Kind | None = None  # The catalogue's, when the skill is one.
    problems: list[Problem] = field(default_factory=list)
    meaning: RowMeaning | None = None

    @property
    def change(self) -> SkillImportChange | None:
        return self.meaning.change if self.meaning else None

    @property
    def _held(self) -> NurseSkill | None:
        return self.meaning.held if self.meaning else None

    @property
    def held_expiry(self) -> dt.date | None:
        return counted_expiry(self._held) if self._held else None

    @property
    def held_identifier(self) -> str | None:
        return self._held.identifier if self._held else None

    @property
    def held_issue_date(self) -> dt.date | None:
        return self._held.issue_date if self._held else None


@dataclass(frozen=True)
class Preview:
    rows: list[Row]
    token: str | None  # None when any row has a problem, so there is nothing to confirm.


@dataclass(frozen=True)
class _Standing:
    """What a row is judged against: the file's other lines and the database as it is now."""
    lines_by_key: dict[tuple[str, str], list[int]]
    # By (skill in lower case, identifier): each line giving the entry that identifier, with its nurse's hospital ID in lower
    # case, as `lines_by_key` has it, and as the file has it, for the words.
    lines_by_identifier: dict[tuple[str, str], list[tuple[int, str, str]]]
    nurses: dict[str, NurseProfile]  # By hospital ID in lower case.
    skills: dict[str, Skill]  # By name in lower case: unique in any case of letters.
    held: dict[tuple[UUID, UUID], NurseSkill]  # By (nurse, skill).


def _key(line: Line) -> tuple[str, str] | None:
    """The nurse and skill a line names, in lower case, when it can name one."""
    if wrong_cell_count(line, SKILLS_FILE):
        return None
    hospital_id, skill = parsed(HOSPITAL_ID, line.cells[0].strip()), line.cells[1].strip()
    return (hospital_id.lower(), skill.lower()) if hospital_id and skill else None


def _identifier(line: Line) -> str | None:
    """The identifier a line gives, as a record would keep it, when it can give one."""
    if wrong_cell_count(line, SKILLS_FILE):
        return None
    return parsed(_IDENTIFIER, line.cells[3])


def _judged(line: Line, standing: _Standing) -> Row:
    if problem := wrong_cell_count(line, SKILLS_FILE):
        row = Row(line.number, *(line.cells + [""] * len(COLUMNS))[:len(COLUMNS)])
        row.problems.append(problem)
        return row
    row = Row(line.number, *(cell.strip() for cell in line.cells))  # `Row`'s first fields are in the order of `COLUMNS`.

    nurse = None
    hospital_id = parsed(HOSPITAL_ID, row.hospital_id)
    if hospital_id is None:
        row.problems.append(Problem("hospital_id", "Use 1 to 150 letters, digits, dots, dashes and underscores."))
    else:
        nurse = standing.nurses.get(hospital_id.lower())
        if nurse is None:
            row.problems.append(Problem("hospital_id", "No nurse has this hospital ID. Use the one the Nurses page or Rostering attributes shows."))
        else:
            row.hospital_id, row.full_name = nurse.user.username, nurse.user.full_name
            if not nurse.user.is_active:
                row.problems.append(Problem("hospital_id", f"{row.full_name} is deactivated. Record their skills on the Nurses "
                                                           "page, or remove the line."))

    skill = standing.skills.get(row.skill.lower()) if row.skill and "\x00" not in row.skill else None
    if skill is None:
        row.problems.append(Problem("skill", "There is no such skill. Use its name on the Skills page."))
    else:
        row.skill, row.kind = skill.name, skill.kind
        if not skill.active:
            row.problems.append(Problem("skill", f"{skill.name} is retired, so it can no longer be recorded or renewed."))
    key = _key(line)
    others = [number for number in standing.lines_by_key.get(key, []) if number != row.line] if key else []
    if others:
        row.problems.append(Problem("skill", f"{row.skill} of {row.hospital_id} is also on line {others[0]}. "
                                             "Keep one line for each nurse's skill."))

    expiry = parsed(_DATE, row.expiry) if row.expiry else None
    if skill is not None and not skill.expires and row.expiry:
        row.problems.append(Problem("expiry", f"{skill.name} does not expire, so leave its expiry empty."))
    elif row.expiry and expiry is None:
        row.problems.append(Problem("expiry", _DATE_WORDS))
    elif skill is not None and skill.expires and not row.expiry:
        row.problems.append(Problem("expiry", f"{skill.name} expires. Enter the last day it counts, as YYYY-MM-DD."))

    identifier = _identifier(line)
    if identifier is not None:
        row.identifier = identifier
    elif row.identifier:
        row.problems.append(Problem("identifier", "Enter the identifier as its issuing body gave it, in at most 100 "
                                                  "characters."))
    if identifier is not None and skill is not None:
        given = standing.lines_by_identifier.get((skill.name.lower(), identifier), [])
        if other := next(((number, written) for number, whose, written in given if key and whose != key[0]), None):
            row.problems.append(Problem("identifier", f"Line {other[0]} gives {other[1]} this identifier for {skill.name} too. "
                                                      "Check the number: each one is one nurse's."))
    issue_date = parsed(_DATE, row.issue_date) if row.issue_date else None
    if row.issue_date and issue_date is None:
        row.problems.append(Problem("issue_date", _DATE_WORDS))

    if not row.problems:
        found = problems_with(nurse, skill, expiry, identifier, issue_date)
        for field, words in found:
            if field == "identifier" and identifier is None:
                words += " Enter it in the Identifier column, as the template has it."
            row.problems.append(Problem(field, words))
        if not found:
            row.meaning = RowMeaning(nurse, skill, expiry, identifier, issue_date, standing.held.get((nurse.pk, skill.pk)))
    return row


def _token(rows: list[Row]) -> str:
    """Stands for what these rows mean right now: the nurse, the skill with its name and kind, the expiry, the identifier,
    the issue date, and the record a renewal would end, so that a renewal by hand since the preview, or a skill renamed, is
    a different preview."""
    meant = [(str(row.meaning.nurse.pk), row.meaning.nurse.user.username, str(row.meaning.skill.pk), row.meaning.skill.name,
              row.meaning.skill.expires, row.meaning.skill.kind, row.meaning.expiry and row.meaning.expiry.isoformat(),
              row.meaning.identifier, row.meaning.issue_date and row.meaning.issue_date.isoformat(),
              str(row.meaning.held.pk) if row.meaning.held else None) for row in rows]
    return hashlib.sha256(json.dumps(meant).encode()).hexdigest()


def nurse_ids(lines: list[Line]) -> set[str]:
    """Each possible hospital ID in the file, in lower case. Only possible ones, as the database is asked about these and
    cannot take a NUL."""
    return {hospital_id.lower() for line in lines if not wrong_cell_count(line, SKILLS_FILE)
            if (hospital_id := parsed(HOSPITAL_ID, line.cells[0].strip()))}


def nurses_named(lines: list[Line]):
    """The nurse profiles the file names, in any case of letters."""
    return (NurseProfile.objects.annotate(lowered=Lower("user__username"))
            .filter(lowered__in=nurse_ids(lines)))


def check(lines: list[Line]) -> Preview:
    """Every row's problems as things stand. It writes nothing and holds no lock."""
    by_key: dict[tuple[str, str], list[int]] = {}
    by_identifier: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    for line in lines:
        if key := _key(line):
            by_key.setdefault(key, []).append(line.number)
            if identifier := _identifier(line):
                by_identifier.setdefault((key[1], identifier), []).append((line.number, key[0], line.cells[0].strip()))
    nurses = {nurse.user.username.lower(): nurse for nurse in nurses_named(lines).select_related("user")}
    skills = {skill.name.lower(): skill for skill in Skill.objects.all()}
    held = {(row.nurse_id, row.skill_id): row
            for row in NurseSkill.objects.current().filter(nurse__in=nurses.values()).select_related("skill")}
    standing = _Standing(by_key, by_identifier, nurses, skills, held)
    rows = [_judged(line, standing) for line in lines]
    return Preview(rows, None if any(row.problems for row in rows) else _token(rows))
