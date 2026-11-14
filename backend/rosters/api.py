"""A unit's roster of a month. Its manager solves, edits and submits it; the CNO and the Deputy CNO read a submitted or an
approved one, and approve or reject what is submitted (`rosters.review`). A draft is the unit management's alone, so they see
only that one exists."""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Field, Query, Router, Schema
from ninja.errors import HttpError

from accounts.authz import SignedIn
from accounts.models import Role, User
from audit.log import record
from hospital.api import StrictSchema, refuse
from hospital.catalogue_api import IsoDate
from hospital.models import HospitalSettings, Shift
from notifications import sending
from nurses import membership
from nurses.api import MonthText
from rosters import in_force, review
from rosters.cells import AsideWhy, NewCell
from rosters.drafts import FROZEN, MONTH_OVER, in_current_codes
from rosters.editing import Checked, DraftEdits, Edit, check_saved, scored, with_edits
from rosters.grids import GridView, full_names, grid_view
from rosters.models import Roster, RosterState, RosterVersion
from rosters.pins import lost_to_leave, month_pins
from rosters.versions import cells_changed, differences
from solves.api import MonthFilter, SolveProgress, chosen_month, manager, progress_of
from solves.input import Assembled, assemble, changes_since
from solves.models import ACTIVE_STATES, Solve, SolveState
from solves.result import has_a_roster_to_keep
from units.models import Unit
from units.scope import HOSPITAL_WIDE, unit_in_scope

router = Router(tags=["rosters"])
approver = SignedIn(Role.CNO, Role.DEPUTY_CNO)
reader = SignedIn(Role.UNIT_MANAGER, Role.CNO, Role.DEPUTY_CNO)
ONLY_THE_MANAGER = "Only the unit's manager changes its roster or submits it."
ONLY_APPROVERS = "Only the CNO or the Deputy CNO approve or reject a roster."
SAVED_AS_A_VERSION = "This roster is approved, so a change to it is saved as a new version in force at once, with a reason."
NOTHING_CHANGED = ("These changes leave every cell of the roster in force as it is, so there is no new version to save. Discard "
                   "them, or change a cell.")
NO_DRAFT = "This month has no draft yet. Solve it first."
SAVED_AS_PINS = "This roster is not approved yet, so its changes are saved as pins on the draft, with no version or reason."


class DraftChange(Schema):
    hospital_id: str = Field(description="Empty for a change of the unit's own (`skills`) or of the month's (`ramadan_days`, "
                                         "`public_holidays`).")
    what: Literal["gender", "leave", "joined", "left", "pins", "preferences", "skills", "closed", "ramadan_days",
                  "public_holidays", "on_probation", "off_probation", "preceptor"] = Field(
        description="`on_probation`: she is on probation this month and it was planned with her counted; `off_probation`: she "
                    "is no longer on probation this month and it was planned with her on probation; `preceptor`: her preceptor "
                    "this month is not the one it was planned with. `skills`: her skills as the month sees them were recorded, renewed, taken off or retired since, or, with no "
                    "hospital ID, the unit's skill needs or their flag, the skills required to work or the skill shortfall's "
                    "price changed. `preferences`: a preference of hers made or ended since. `closed`: the month before closed after this was solved, so the carry-over it started from was projected and "
                    "the close's is the one that holds now. `ramadan_days`, `public_holidays`, with no hospital ID: "
                    "the month's Ramadan days or its public holidays differ from the list it was solved or submitted on.")
    full_name: str


class GridViolation(Schema):
    rule: str
    hospital_id: str | None
    full_name: str
    date: dt.date | None
    text: str


class GridCheck(Schema):
    """What the solver's own code says of a grid. `reason` says why it could not, and the rest is then null."""
    reason: str | None = Field(description="Why this could not be worked out: the solver's code cannot read the month as it "
                                           "stands.")
    ok: bool | None = Field(description="Whether it breaks no hard rule.")
    violations: list[GridViolation]
    cost: int | None


class DraftPin(Schema):
    hospital_id: str
    full_name: str
    date: dt.date
    shift: str = Field(description="The shift's code. Empty for a day off.")
    origin: Literal["manager", "request", "swap", "cover"]
    set_aside: AsideWhy | None = Field(description="Why this pin counts for nothing at present and reaches no solve. It is kept, "
                                                   "and counts again if that stops being so. Null for a pin that counts.")


class PinLostToLeave(Schema):
    hospital_id: str
    full_name: str
    date: dt.date
    shift: str
    by: str = Field(description="Who recorded the leave.")


class EditorShift(Schema):
    code: str
    display_name: str
    colour: str
    hours: int


class EditorNurse(Schema):
    hospital_id: str
    allowed_shifts: list[str] | None = Field(description="Null for any the unit works.")
    credited: list[dt.date] = Field(description="Their days of leave and days outside their dates, as they stand now.")


class DraftEditor(Schema):
    refusal: str | None = Field(description="Why this roster cannot be edited now: the month is over, the roster is submitted, "
                                            "or the reader is not its manager. An approved one is edited too, each save a new "
                                            "version (`saveRosterVersion`).")
    shifts: list[EditorShift] = Field(description="What a cell can be pinned to: the unit's shifts.")
    nurses: list[EditorNurse]
    gone: list[str] = Field(description="Hospital IDs on the grid no longer in the unit's month, such as a nurse transferred "
                                        "out: nothing can be pinned for them, and clearing a cell takes its shift off.")


class Draft(GridView):
    solve_id: UUID
    solved_at: dt.datetime
    restored_at: dt.datetime | None = Field(description="When the manager restored this result as the draft. Null when the "
                                                        "draft is what its solve left.")
    edited: bool = Field(description="Whether the grid differs from what its solve answered.")
    cost: int | None = Field(description="This grid's on the month as it stands, which is its solve's while neither the grid nor "
                                         "the month has changed since. Null if it could not be worked out.")
    solved_cost: int
    check: GridCheck | None = Field(description="What the grid breaks on the month as it stands. Null while neither the grid nor "
                                                "the month has changed since its solve, whose roster breaks none.")
    pins: list[DraftPin]
    pins_lost_to_leave: list[PinLostToLeave] = Field(description="Pins leave has replaced since this draft's solve was started, "
                                                                 "whoever recorded it, until a newer solve has taken that in.")
    editor: DraftEditor
    stopped_early: bool
    changes: list[DraftChange] = Field(description="What has changed since this draft was solved. Warned of here, and "
                                                   "checked again at submit and at approval.")


class Waiting(Schema):
    submitted_by: str
    submitted_at: dt.datetime
    new_since: list[DraftChange] = Field(description="Those of `changes` that have come since it was submitted, such as leave "
                                                     "recorded while it waits: the manager was not warned of these.")


class Rejection(Schema):
    rejected_by: str
    rejected_at: dt.datetime
    comment: str


class Approval(Schema):
    approved_by: str
    approved_at: dt.datetime
    version: int = Field(description="The roster version in force: the latest.")


class RosterMonth(Schema):
    month: str
    state: Literal["none", "draft", "submitted", "approved"] = Field(description="`none` until a solve keeps a roster.")
    submitted: Waiting | None = Field(description="While it waits for a decision.")
    last_rejection: Rejection | None = Field(description="The latest rejection, kept through the submits after it.")
    approved: Approval | None
    submit_refusal: str | None = Field(description="Why the manager cannot submit it now: also a hard rule it breaks on the "
                                                   "month as it stands, which `draft.check` lists.")
    approve_refusal: str | None = Field(description="Why it cannot be approved now, the same check included.")
    reject_refusal: str | None
    active_solve: SolveProgress | None
    last_solve: SolveProgress | None = Field(description="The latest to have ended, if it is newer than the draft or there "
                                                         "is none: why the draft is not what was last asked for. A restore "
                                                         "is newer than what it follows, as the manager has seen those.")
    draft: Draft | None = Field(description="The roster's grid and what it answered, whatever its state. Null for the CNO and the "
                                            "Deputy CNO while it is a draft.")


def _why_not_edit(roster: Roster, user: User) -> str | None:
    """One place for the page and both saves: a submitted roster and a month that is over refuse an edit. An approved roster is
    edited, each save a new version (`save_version`), though `rosters.drafts.why_frozen` still refuses it a solve and a restore:
    a re-plan does that."""
    if user.role != Role.UNIT_MANAGER:
        return ONLY_THE_MANAGER
    if roster.state == RosterState.SUBMITTED:
        return FROZEN[RosterState.SUBMITTED]
    if roster.month < membership.this_month():
        return in_force.MONTH_OVER if roster.state == RosterState.APPROVED else MONTH_OVER
    return None


def _grid_check(checked: Checked) -> GridCheck:
    if checked.reason:
        return GridCheck(reason=checked.reason, ok=None, violations=[], cost=None)
    violations = checked.answer["violations"]
    names = full_names(row["nurse"] for row in violations if row["nurse"])
    return GridCheck(reason=None, ok=checked.answer["ok"], cost=checked.answer["cost"],
                     violations=[GridViolation(rule=row["rule"], hospital_id=row["nurse"], full_name=names.get(row["nurse"], ""),
                                               date=row["date"], text=row["text"]) for row in violations])


def _editor(roster: Roster, solve_input: dict, user: User) -> DraftEditor:
    codes = roster.unit.rules.get("shifts", [])
    present = {row["id"] for row in solve_input["nurses"]}
    return DraftEditor(
        gone=sorted(set(roster.grid) - present),
        refusal=_why_not_edit(roster, user),
        shifts=[EditorShift(code=shift.code, display_name=shift.display_name, colour=shift.colour, hours=shift.hours)
                for shift in Shift.objects.filter(code__in=codes, active=True)],
        nurses=[EditorNurse(hospital_id=row["id"], allowed_shifts=row["allowed_shifts"], credited=row["leave"])
                for row in solve_input["nurses"]])


def _lost_to_leave(roster: Roster) -> list[PinLostToLeave]:
    # A newer solve than the draft's took that leave in, even if an older result has been restored since. Only one that kept a
    # roster did: a failed solve, or one that found none, left the draft holding the pin's shift on the leave day.
    ended = (Solve.objects.filter(unit=roster.unit, month=roster.month).exclude(state__in=[*ACTIVE_STATES, SolveState.FAILED])
             .filter(requested_at__gt=roster.solve.requested_at).order_by("-requested_at").only("requested_at", "result"))
    newest = next((solve for solve in ended.iterator() if has_a_roster_to_keep(solve.result)), roster.solve)
    rows = lost_to_leave(roster.unit, roster.month, newest.requested_at)
    return [PinLostToLeave(hospital_id=row.nurse.user.username, full_name=row.nurse.user.full_name, date=row.date, shift=row.code,
                           by=row.removed_by.full_name or row.removed_by.username) for row in rows]


def _changes(changes: list[dict]) -> list[DraftChange]:
    names = full_names(change["hospital_id"] for change in changes)
    return [DraftChange(hospital_id=change["hospital_id"], what=change["what"], full_name=names.get(change["hospital_id"], ""))
            for change in changes]


def _checked(roster: Roster, changes: list[dict], solve_input: dict) -> Checked | None:
    """None while neither the grid nor the month has changed since its solve, whose own answer then holds: it breaks no hard
    rule, since a roster that does never becomes a draft. `solve_input` is the month as it stands (`check_saved`'s)."""
    edited = roster.grid != in_current_codes(roster.solve.result["roster"])
    return scored(solve_input, roster.grid) if edited or changes else None


def _draft(roster: Roster, changes: list[dict], checked: Checked | None, user: User, solve_input: dict) -> Draft:
    solve = roster.solve
    edited = roster.grid != in_current_codes(solve.result["roster"])
    # The solve's own cost and its roster's soundness hold only for the month it was given.
    check = _grid_check(checked) if checked else None
    pins = month_pins(roster.unit, roster.month)
    return Draft(**grid_view(roster.grid, solve).dict(), solve_id=solve.pk, solved_at=timezone.localtime(solve.finished_at),
                 restored_at=timezone.localtime(roster.restored_at) if roster.restored_at else None,
                 edited=edited, cost=check.cost if check else solve.result["cost"], solved_cost=solve.result["cost"], check=check,
                 stopped_early=solve.state == SolveState.CANCELLED,
                 pins=[DraftPin(hospital_id=pin.nurse.user.username, full_name=pin.nurse.user.full_name, date=pin.date,
                                shift=pin.code, origin=pin.origin, set_aside=pin.aside.why if pin.aside else None)
                       for pin in pins.rows],
                 pins_lost_to_leave=_lost_to_leave(roster),
                 editor=_editor(roster, solve_input, user), changes=_changes(changes))


def _not_the_draft(ended: Solve | None, roster: Roster | None) -> Solve | None:
    if ended is None or roster is None:
        return ended
    return ended if ended.pk != roster.solve_id and ended.finished_at > roster.updated_at else None


def _waiting(roster: Roster, assembled: Assembled) -> Waiting | None:
    submission = review.waiting(roster)
    if submission is None:
        return None
    return Waiting(submitted_by=review.name_of(submission.submitted_by), submitted_at=timezone.localtime(submission.submitted_at),
                   new_since=_changes(review.new_since(submission, assembled)))


def _rejection(roster: Roster) -> Rejection | None:
    rejected = review.last_rejection(roster)
    return rejected and Rejection(rejected_by=review.name_of(rejected.decided_by), rejected_at=timezone.localtime(rejected.decided_at),
                                  comment=rejected.comment)


def _approval(roster: Roster) -> Approval | None:
    approved = review.approval(roster)
    return approved and Approval(approved_by=review.name_of(approved.decided_by), approved_at=timezone.localtime(approved.decided_at),
                                 version=roster.versions.first().number)


def _roster_month(unit: Unit, month: dt.date, user: User) -> RosterMonth:
    solves = Solve.objects.filter(unit=unit, month=month)
    roster = Roster.objects.select_related("solve", "unit").filter(unit=unit, month=month).first()
    approving = user.role in HOSPITAL_WIDE
    label = month.strftime("%Y-%m")
    if roster is None or (approving and roster.state not in review.SEEN_BY_APPROVERS):
        none = "This month has no roster yet." if roster is None else None
        return RosterMonth(month=label, state=roster.state if roster else "none", submitted=None, last_rejection=None,
                           approved=None, active_solve=None if approving else _active(solves), draft=None,
                           last_solve=None if approving else _last(solves, roster),
                           submit_refusal=ONLY_THE_MANAGER if approving else none or None,
                           approve_refusal=review.why_not_decide(roster) if approving else ONLY_APPROVERS,
                           reject_refusal=review.why_not_decide(roster) if approving else ONLY_APPROVERS)
    # The month as it stands, read once: what changed since the solve and since the submit, the check and the editor all ask it,
    # and on a large unit each reading takes a while (every nurse's carry-over among it).
    assembled = assemble(unit, month)
    changes = changes_since(roster.solve.input, unit, month, since=roster.solve.requested_at, assembled=assembled,
                            month_dates=True)
    checked = _checked(roster, changes, assembled.input)
    return RosterMonth(
        month=label, state=roster.state, submitted=_waiting(roster, assembled), last_rejection=_rejection(roster),
        approved=_approval(roster), active_solve=None if approving else _active(solves),
        last_solve=None if approving else _last(solves, roster), draft=_draft(roster, changes, checked, user, assembled.input),
        submit_refusal=ONLY_THE_MANAGER if approving else review.why_not_submit(roster, checked),
        approve_refusal=review.why_not_approve(roster, checked) if approving else ONLY_APPROVERS,
        reject_refusal=review.why_not_decide(roster) if approving else ONLY_APPROVERS)


def _active(solves) -> SolveProgress | None:
    active = solves.filter(state__in=ACTIVE_STATES).first()
    return progress_of(active) if active else None


def _last(solves, roster: Roster | None) -> SolveProgress | None:
    last = _not_the_draft(solves.exclude(state__in=ACTIVE_STATES).order_by("-finished_at", "-id").first(), roster)
    return progress_of(last) if last else None


@router.get("/{unit_id}/roster", response=RosterMonth, auth=reader, operation_id="getRosterMonth")
def roster_month(request: HttpRequest, unit_id: UUID, filters: Query[MonthFilter]) -> RosterMonth:
    """The CNO and the Deputy CNO read any unit's, and see a draft only as one that exists, with no solve of it."""
    return _roster_month(unit_in_scope(request.user, unit_id), chosen_month(filters.month), request.user)


# A whole month of a large unit, and no more.
MAX_EDITS = 3100


class EditBody(StrictSchema):
    hospital_id: str = Field(min_length=1, max_length=150)
    date: IsoDate
    action: Literal["shift", "off", "clear"] = Field(description="Pin a shift, pin a day off, or clear the cell's pin.")
    shift: str | None = Field(None, max_length=8, description="The shift's code, with `shift`.")


class EditsBody(StrictSchema):
    month: MonthText
    edits: Annotated[list[EditBody], Field(max_length=MAX_EDITS)]


def _edits_of(body: EditsBody) -> list[Edit]:
    return [Edit(hospital_id=edit.hospital_id, date=edit.date, action=edit.action, shift=edit.shift) for edit in body.edits]


def _refuse_nonsense(body: EditsBody) -> list[Edit]:
    """Whatever the month holds, these could never be meant, so they are 422s for a save and a check alike."""
    edits = _edits_of(body)
    wrong = [(("edits", index), f"{edit.day} is not in {body.month}.")
             for index, edit in enumerate(edits) if edit.date.strftime("%Y-%m") != body.month]
    wrong += [(("edits", index), "Choose a shift.")
              for index, edit in enumerate(edits) if edit.action == "shift" and not edit.shift]
    seen = set()
    for index, edit in enumerate(edits):
        if (edit.hospital_id, edit.date) in seen:
            wrong.append((("edits", index), "This cell is edited twice."))
        seen.add((edit.hospital_id, edit.date))
    if wrong:
        refuse(*wrong)
    return edits


def _draft_of(unit: Unit, month: dt.date) -> Roster:
    roster = Roster.objects.select_related("solve", "unit").filter(unit=unit, month=month).first()
    if roster is None:
        raise HttpError(409, NO_DRAFT)
    return roster


class EditRefusal(Schema):
    hospital_id: str
    date: dt.date
    reason: str


class DraftCheck(GridCheck):
    cost_before: int | None = Field(description="The saved grid's, worked out the same way.")
    delta: int | None = Field(description="`cost` less `cost_before`.")
    refusals: list[EditRefusal] = Field(description="Edits a save would refuse, which are left out of this answer.")


@router.post("/{unit_id}/roster/check", response=DraftCheck, auth=manager, operation_id="checkDraftEdits")
def check_edits(request: HttpRequest, unit_id: UUID, body: EditsBody) -> DraftCheck:
    """The draft as these edits would make it, against the month as it stands, on the solver's own code in this process: `Checked`
    says why. Saves nothing and writes no audit entry."""
    unit = unit_in_scope(request.user, unit_id)
    month = dt.date.fromisoformat(f"{body.month}-01")
    edits = _refuse_nonsense(body)
    roster = _draft_of(unit, month)
    refused = _why_not_edit(roster, request.user)
    if refused:
        raise HttpError(409, refused)
    editing = DraftEdits(roster)
    reasons = [editing.refusal(edit) for edit in edits]
    refusals = [EditRefusal(hospital_id=edit.hospital_id, date=edit.date, reason=reason)
                for edit, reason in zip(edits, reasons) if reason]
    good = [edit for edit, reason in zip(edits, reasons) if not reason]
    solve_input = assemble(unit, month).input
    before = scored(solve_input, roster.grid)
    after = scored(with_edits(solve_input, editing, good), editing.grid_after(good))
    delta = after.cost - before.cost if after.cost is not None and before.cost is not None else None
    return DraftCheck(**_grid_check(after).dict(), cost_before=before.cost, delta=delta, refusals=refusals)


def _locked_for_edits(request: HttpRequest, unit_id: UUID, month: dt.date, edits: list[Edit]) -> in_force.Held:
    """Inside the save's transaction: the unit's month held for the edited nurses (`rosters.in_force.hold`), whatever the roster's
    state, then the manager's scope read again on the unit as locked, so a manager taken off it while this waited is refused."""
    held = in_force.hold(unit_id, month, nurses={edit.hospital_id for edit in edits})
    unit_in_scope(request.user, unit_id)
    if held.roster is None:
        raise HttpError(409, NO_DRAFT)
    return held


def _judged(roster: Roster, edits: list[Edit]) -> DraftEdits:
    """Every edit's own refusal at once, before anything is written."""
    editing = DraftEdits(roster)
    refusals = [(("edits", index), reason) for index, edit in enumerate(edits) if (reason := editing.refusal(edit))]
    if refusals:
        refuse(*refusals)
    return editing


@router.post("/{unit_id}/roster/pins", response=RosterMonth, auth=manager, operation_id="saveDraftPins")
def save_pins(request: HttpRequest, unit_id: UUID, body: EditsBody) -> RosterMonth:
    """A draft's, all of it or none of it, under the locks `_locked_for_edits` names; an approved roster's changes are versions
    (`saveRosterVersion`). A solve already under way was given the month without these: it may run on, its roster takes them on
    top when it ends, and the draft says so through `changes`. The audit entry names the unit and the month and how many pins and
    cells changed, and no nurse, date or shift: the System Admin sees no roster."""
    month = dt.date.fromisoformat(f"{body.month}-01")
    edits = _refuse_nonsense(body)
    with transaction.atomic():
        roster = _locked_for_edits(request, unit_id, month, edits).roster
        refused = _why_not_edit(roster, request.user)
        if refused:
            raise HttpError(409, refused)
        if roster.state != RosterState.DRAFT:
            raise HttpError(409, SAVED_AS_A_VERSION)
        editing = _judged(roster, edits)
        unit = roster.unit
        pins, cells = editing.save(edits, request.user)
        if pins or cells:
            record("roster.draft_edited", actor=request.user, target=f"{unit.name}, {body.month}", request=request,
                   after={"pins": pins, "cells": cells})
    return _roster_month(unit, month, request.user)


class VersionBody(EditsBody):
    reason: str = Field(min_length=1, max_length=2000,
                        description="Why, for the unit's manager and the approvers, who read it in the version list. Never a "
                                    "nurse, a notification or the audit log.")


@router.post("/{unit_id}/roster/versions", response=RosterMonth, auth=manager, operation_id="saveRosterVersion")
def save_version(request: HttpRequest, unit_id: UUID, body: VersionBody) -> RosterMonth:
    """An approved roster's direct edits, all of it or none of it, under the locks `_locked_for_edits` names: a new version with
    origin `edit` and the reason, in force at once, each edited cell a manager's pin as on the draft. Edits that change no cell
    are refused and nothing is saved, a pin included, as the user decided: no version and nobody told of a change no nurse
    could see. One that breaks a hard rule saves, and the answer's `draft.check` says what it breaks: the roster in force has to
    say what is so, such as a shift left short by a nurse taken off sick. The audit entry says a version was made, and never why.
    The unit's nurses of the month are told, with no reason, cell or name."""
    month = dt.date.fromisoformat(f"{body.month}-01")
    edits = _refuse_nonsense(body)
    reason = body.reason.strip()
    if not reason:
        refuse((("reason",), "Say why, for the version list."))
    with transaction.atomic():
        held = _locked_for_edits(request, unit_id, month, edits)
        roster, unit = held.roster, held.roster.unit
        # Asked before judging, so a roster that cannot change is refused before any edit's own refusal; `in_force.change`
        # then refuses nothing. A manager working on a draft or a submitted roster is told what she can do with it instead.
        refused = held.refusal()
        if isinstance(refused, in_force.NotApproved):
            raise HttpError(409, _why_not_edit(roster, request.user) or SAVED_AS_PINS)
        if refused:
            raise HttpError(409, str(refused))
        editing = _judged(roster, edits)
        pins = editing.save_pins(edits, request.user)
        made = in_force.change(held, _cells(editing, edits), origin=in_force.Origin.EDIT, by=request.user, reason=reason,
                               pins=pins, request=request)
        if made is in_force.UNCHANGED:
            # Raised inside the transaction, so the pins saved above go with it.
            raise HttpError(409, NOTHING_CHANGED)
    return _roster_month(unit, month, request.user)


def _cells(editing: DraftEdits, edits: list[Edit]) -> list[NewCell]:
    """Each edited cell as the roster in force would hold it. Clearing the cell of a nurse with no row on the grid changes
    nothing, so it has none."""
    on_grid = [edit for edit in edits if edit.day in editing.roster.grid.get(edit.hospital_id, ())]
    return [NewCell(edit.hospital_id, edit.date, editing.cell_after(edit) or None) for edit in on_grid]


class VersionRow(Schema):
    number: int
    origin: Literal["solve", "edit", "replan", "swap", "request", "cover"]
    created_by: str
    created_at: dt.datetime
    reason: str = Field(description="Empty for version 1, which the approval made. It can say a cell was leave, so only the "
                                    "unit's manager and the approvers read it.")
    cells_changed: int | None = Field(description="Cells that differ from the version before. Null for version 1.")


@router.get("/{unit_id}/roster/versions", response=list[VersionRow], auth=reader,
            operation_id="listRosterVersions")
def roster_versions(request: HttpRequest, unit_id: UUID, filters: Query[MonthFilter]) -> list[VersionRow]:
    """Newest first. Only an approved roster has versions, so the CNO and the Deputy CNO read them as they read the roster."""
    unit = unit_in_scope(request.user, unit_id)
    month = chosen_month(filters.month)
    rows = list(RosterVersion.objects.filter(roster__unit=unit, roster__month=month).select_related("created_by"))
    return [VersionRow(number=version.number, origin=version.origin, created_by=review.name_of(version.created_by),
                       created_at=timezone.localtime(version.created_at), reason=version.reason,
                       cells_changed=cells_changed(version, rows[index + 1] if index + 1 < len(rows) else None))
            for index, version in enumerate(rows)]


class CompareFilter(MonthFilter):
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=1)


class CellChangeRow(Schema):
    hospital_id: str
    full_name: str
    date: dt.date
    before: str = Field(description="The shift's code in `from_version`, today's code. Empty for a day not worked.")
    after: str = Field(description="The same in `to_version`.")


class VersionComparison(Schema):
    from_version: int
    to_version: int
    changes: list[CellChangeRow] = Field(description="Each cell that differs, by nurse then date. A rename is not a change.")


@router.get("/{unit_id}/roster/versions/compare", response=VersionComparison, auth=reader,
            operation_id="compareRosterVersions")
def compare_versions(request: HttpRequest, unit_id: UUID, filters: Query[CompareFilter]) -> VersionComparison:
    """Any two versions of the month, in either order, cell by cell under today's codes. For the unit's manager and the approvers:
    a nurse reads the roster in force on My roster, and nothing of what changed in a colleague's row."""
    unit = unit_in_scope(request.user, unit_id)
    month = chosen_month(filters.month)
    found = {version.number: version for version in RosterVersion.objects.filter(
        roster__unit=unit, roster__month=month, number__in=[filters.from_version, filters.to_version])}
    for number in (filters.from_version, filters.to_version):
        if number not in found:
            raise HttpError(404, f"This month's roster has no version {number}.")
    changes = differences(found[filters.from_version].grid, found[filters.to_version].grid)
    names = full_names(change.hospital_id for change in changes)
    return VersionComparison(from_version=filters.from_version, to_version=filters.to_version, changes=[
        CellChangeRow(hospital_id=change.hospital_id, full_name=names.get(change.hospital_id, ""), date=change.date,
                      before=change.before, after=change.after) for change in changes])


class MonthBody(StrictSchema):
    month: MonthText


class RejectBody(MonthBody):
    comment: str = Field(min_length=1, max_length=2000, description="Why, for the unit's manager, who sees it with the draft.")


def _target(unit: Unit, month: dt.date) -> str:
    return f"{unit.name}, {month:%Y-%m}"


def _locked_roster(unit: Unit, month: dt.date) -> Roster | None:
    return Roster.objects.select_for_update(of=("self",)).select_related("solve", "unit").filter(unit=unit, month=month).first()


@router.post("/{unit_id}/roster/submit", response=RosterMonth, auth=manager, operation_id="submitRoster")
def submit_roster(request: HttpRequest, unit_id: UUID, body: MonthBody) -> RosterMonth:
    """Checked again on the month as it stands, always: a draft out of date or edited by hand goes through with its warning,
    and is refused only if it breaks a hard rule or cannot be checked. Refused while a solve of the month is active, so a solve
    never ends on a frozen grid. Under the catalogue's lock, the month's nurses' and the unit's, in that order, as approving takes
    them. The audit entry names the unit and the month."""
    month = dt.date.fromisoformat(f"{body.month}-01")
    with transaction.atomic():
        HospitalSettings.load(for_update=True)
        # The nurses' lock too, so that no leave lands between the check and the month kept for the approver to compare with.
        review.lock_the_months_nurses(unit_in_scope(request.user, unit_id), month,
                                      Roster.objects.filter(unit_id=unit_id, month=month).first())
        unit = unit_in_scope(request.user, unit_id, for_update=True)
        roster = _locked_roster(unit, month)
        if roster is None:
            raise HttpError(409, NO_DRAFT)
        refusal = review.why_not_submit(roster, check_saved(roster))
        if refusal:
            raise HttpError(409, refusal)
        review.submit(roster, request.user)
        record("roster.submitted", actor=request.user, target=_target(unit, month), request=request)
        sending.roster_submitted(unit, month, request.user)
    return _roster_month(unit, month, request.user)


@router.post("/{unit_id}/roster/approve", response=RosterMonth, auth=approver, operation_id="approveRoster")
def approve_roster(request: HttpRequest, unit_id: UUID, body: MonthBody) -> RosterMonth:
    """Checked again on the month as it stands, since leave may be recorded while it waits, and refused only if that fails. Under
    the catalogue's lock, then the month's nurses', which a leave write takes, then the unit's: no leave lands between the check
    and the approval, and of two approvers the second is told the first decided. The first approval makes version 1."""
    month = dt.date.fromisoformat(f"{body.month}-01")
    with transaction.atomic():
        HospitalSettings.load(for_update=True)
        unit = unit_in_scope(request.user, unit_id)
        review.lock_the_months_nurses(unit, month, Roster.objects.filter(unit=unit, month=month).first())
        unit = unit_in_scope(request.user, unit_id, for_update=True)
        roster = _locked_roster(unit, month)
        refusal = review.why_not_approve_before_checking(roster) or review.why_not_approve(roster, check_saved(roster))
        if refusal:
            raise HttpError(409, refusal)
        version = review.approve(roster, request.user)
        record("roster.approved", actor=request.user, target=_target(unit, month), request=request,
               after={"version": version.number})
        sending.roster_approved(unit, month, request.user)
    return _roster_month(unit, month, request.user)


@router.post("/{unit_id}/roster/reject", response=RosterMonth, auth=approver, operation_id="rejectRoster")
def reject_roster(request: HttpRequest, unit_id: UUID, body: RejectBody) -> RosterMonth:
    """Back to draft, with the comment kept for the manager. The audit entry holds no comment, since it may name a nurse or
    their leave and the System Admin reads the log."""
    month = dt.date.fromisoformat(f"{body.month}-01")
    if not body.comment.strip():
        refuse((("comment",), "Say why, for the unit's manager."))
    with transaction.atomic():
        HospitalSettings.load(for_update=True)
        unit = unit_in_scope(request.user, unit_id, for_update=True)
        roster = _locked_roster(unit, month)
        refusal = review.why_not_decide(roster)
        if refusal:
            raise HttpError(409, refusal)
        review.reject(roster, request.user, body.comment.strip())
        record("roster.rejected", actor=request.user, target=_target(unit, month), request=request)
        sending.roster_rejected(unit, month, request.user)
    return _roster_month(unit, month, request.user)
