"""Solving a unit's month: only its manager."""
from __future__ import annotations

import datetime as dt
from typing import Literal
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Field, Query, Router, Schema, Status
from ninja.errors import HttpError

from accounts.authz import SignedIn
from accounts.models import Role
from accounts.sessions import not_activity
from audit.log import record
from hospital.api import StrictSchema, refuse
from hospital.catalogue_api import IsoDate
from hospital.models import HospitalSettings
from nurses import membership
from nurses.api import MonthText
from nurses.models import NurseProfile
from ops import solver_client
from ops.models import TechnicalSettings
from ops.queue import enqueue
from ops.solver_client import SolverRefused, SolverUnreachable
from rosters.drafts import frozen_month
from rosters.models import Roster, RosterState
from solves import replans
from solves.input import Assembled, assemble, assemble_replan, recorded_cells_differ, solver_refusal
from solves.models import ACTIVE_STATES, ReplanOutcomeWord, Solve, SolveFailureWord, SolveState, why_not_another_month
from solves.result import has_a_roster_to_keep, in_dates, precheck_of
from solves.tasks import run_solve
from units.models import Unit
from units.scope import unit_in_scope

router = Router(tags=["solves"])
manager = SignedIn(Role.UNIT_MANAGER)


class MonthFilter(Schema):
    month: MonthText | None = Field(None, description="YYYY-MM. The month after the hospital's current one when left out, "
                                                      "since that is the one usually being rostered.")


def chosen_month(month: str | None) -> dt.date:
    if month:
        return dt.date.fromisoformat(f"{month}-01")
    this = membership.this_month()
    return (this + dt.timedelta(days=32)).replace(day=1)


def _time_limits(unit: Unit) -> tuple[int, int]:
    """The default and the cap. A unit's default can be above the cap if the System Admin lowered it after the unit's save."""
    cap = TechnicalSettings.load().time_limit_cap_seconds
    return min(unit.default_time_limit, cap), cap


def _names(nurses: list[NurseProfile]) -> list[str]:
    return [nurse.user.full_name or nurse.user.username for nurse in nurses]


def _what_stops_it(unit: Unit, month: dt.date, assembled: Assembled) -> str | None:
    return _what_stops_the_month(unit, month) or _what_the_nurses_stop(assembled)


def _what_stops_the_month(unit: Unit, month: dt.date) -> str | None:
    """What needs no assembly, so a start can be refused before it assembles the month under the hospital settings' lock."""
    if month < membership.this_month():
        return "This month is over, so it cannot be solved."
    frozen = frozen_month(unit, month)
    if frozen:
        return frozen
    if Solve.objects.filter(unit=unit, month=month, state__in=ACTIVE_STATES).exists():
        return "A solve of this month is already queued or running."
    return why_not_another_month(unit, month)


def _what_the_nurses_stop(assembled: Assembled) -> str | None:
    if not assembled.nurses:
        return "This unit has no nurses in this month."
    if assembled.to_check:
        return (f"Check the allowed shifts of {', '.join(_names(assembled.to_check))} first: a transfer emptied them, "
                "as none of their shifts are worked here.")
    return None


class Summary(Schema):
    nurses: int
    leave_days: int = Field(description="Recorded leave within the nurses' dates.")
    outside_days: int = Field(description="Days before a start date or after an end date, credited like leave.")
    pins: int = Field(description="Pinned cells that reach this solve.")


class Warnings(Schema):
    hospital_rules_need_review: bool
    unit_rules_need_review: bool
    unreviewed_scores: int = Field(description="Nurses of the month whose attributes no manager has ever saved, so their "
                                               "score is still the default.")
    left_out: list[str] = Field(description="Names of managers with a nurse profile whose account is deactivated.")


class SolvePreview(Schema):
    month: str
    summary: Summary
    warnings: Warnings
    precheck: list[str] | None = Field(description="Reasons the month may have no roster. Empty when none are known; null "
                                                   "when the check could not run.")
    rules_refusal: str | None = Field(description="Why the solver would refuse the rules as they stand. Not a failed solve: "
                                                  "review the unit rules.")
    refusal: str | None = Field(description="Why no solve can start for this month, whatever the rules say.")
    time_limit_default: int
    time_limit_cap: int


@router.get("/{unit_id}/solves/preview", response=SolvePreview, auth=manager, operation_id="previewSolve")
def preview(request: HttpRequest, unit_id: UUID, filters: Query[MonthFilter]) -> SolvePreview:
    unit = unit_in_scope(request.user, unit_id)
    month = chosen_month(filters.month)
    default, cap = _time_limits(unit)
    assembled = assemble(unit, month, time_limit=default)
    stopped_by = _what_stops_it(unit, month, assembled)
    refusal = solver_refusal(assembled.input) if assembled.nurses else None
    issues = None
    if refusal is None and assembled.nurses:
        try:
            issues = solver_client.precheck(assembled.input)
        except SolverRefused as e:
            refusal = str(e)
        except SolverUnreachable:
            pass
    return SolvePreview(
        month=month.strftime("%Y-%m"),
        summary=Summary(nurses=len(assembled.nurses), leave_days=assembled.leave_days,
                        outside_days=sum(map(len, assembled.outside_days.values())), pins=assembled.pins),
        warnings=Warnings(hospital_rules_need_review=HospitalSettings.load().rules_need_review,
                          unit_rules_need_review=unit.rules_need_review,
                          unreviewed_scores=assembled.unreviewed_scores, left_out=_names(assembled.left_out)),
        precheck=issues, rules_refusal=refusal, refusal=stopped_by, time_limit_default=default, time_limit_cap=cap)


class StartSolveBody(StrictSchema):
    month: MonthText
    time_limit_seconds: int = Field(ge=1, description="At most the System Admin's cap.")


class SolveProgress(Schema):
    id: UUID
    month: str
    state: Literal[SolveState.QUEUED, SolveState.RUNNING, SolveState.SUCCEEDED, SolveState.FAILED, SolveState.CANCELLED]
    queue_ahead: int | None = Field(description="Solves of any unit that go first. Null once this one has started.")
    time_limit_seconds: int
    stop_requested: bool
    elapsed_seconds: float
    best_cost: int | None
    improvements: int
    attempts: int
    requested_at: dt.datetime
    finished_at: dt.datetime | None
    failure: SolveFailureWord | None = Field(
        description="`refused` is a matter of the rules, to be shown as such and not as a failed solve.")
    error: str
    found_roster: bool = Field(description="Whether this solve ended with a roster it could keep, which became the draft "
                                            "then. A later solve or a restore may have replaced it since.")
    message: str = Field(description="Why the solver found no roster, in its words.")
    precheck: list[str] = Field(description="What the solver's own precheck said, once it has ended.")
    replan_from: dt.date | None = Field(description="Set on a re-plan of an approved month: the first date it plans again.")
    replan_outcome: ReplanOutcomeWord | None = Field(
        description="What a re-plan's end did to the roster in force. Null until it ends, for a plain solve, and for a re-plan "
                    "that kept no roster.")


def progress_of(solve: Solve) -> SolveProgress:
    result = solve.result or {}
    return SolveProgress(id=solve.pk, month=solve.month.strftime("%Y-%m"), state=solve.state, queue_ahead=solve.queue_ahead,
                         time_limit_seconds=solve.time_limit_seconds, stop_requested=solve.stop_requested,
                         elapsed_seconds=solve.elapsed_seconds, best_cost=solve.best_cost, improvements=solve.improvements,
                         attempts=solve.attempts, requested_at=timezone.localtime(solve.requested_at),
                         finished_at=timezone.localtime(solve.finished_at) if solve.finished_at else None,
                         failure=solve.failure or None, error=in_dates(solve.error, solve.month),
                         found_roster=has_a_roster_to_keep(result),
                         message=in_dates(result.get("message") or "", solve.month), precheck=precheck_of(result, solve.month),
                         replan_from=solve.replan_from, replan_outcome=solve.replan_outcome or None)


@router.post("/{unit_id}/solves", response={201: SolveProgress}, auth=manager, operation_id="startSolve")
def start_solve(request: HttpRequest, unit_id: UUID, body: StartSolveBody) -> Status:
    """The solve input is stored as it stands now, whatever changes while the solve waits for its turn."""
    month = dt.date.fromisoformat(f"{body.month}-01")
    with transaction.atomic():
        HospitalSettings.load(for_update=True)  # A rename would otherwise pass between reading the rules and the shifts.
        unit = unit_in_scope(request.user, unit_id, for_update=True)
        _, cap = _time_limits(unit)
        if body.time_limit_seconds > cap:
            refuse((("time_limit_seconds",), f"The System Admin's cap is {cap} seconds."))
        stopped_by = _what_stops_the_month(unit, month)
        if stopped_by:
            raise HttpError(409, stopped_by)
        assembled = assemble(unit, month, time_limit=body.time_limit_seconds)
        stopped_by = _what_the_nurses_stop(assembled)
        if stopped_by:
            raise HttpError(409, stopped_by)
        refusal = solver_refusal(assembled.input)
        if refusal:
            raise HttpError(409, f"The solver refuses the rules as they stand, so review the unit rules: {refusal}")
        solve = Solve.objects.create(unit=unit, month=month, requested_by=request.user,
                                     time_limit_seconds=body.time_limit_seconds, input=assembled.input,
                                     outside_days=assembled.outside_days)
        solve.job_id = enqueue(run_solve, solve_id=str(solve.pk))
        solve.save(update_fields=["job_id"])
        record("solve.started", actor=request.user, target=solve.audit_target, request=request,
               after={"time_limit_seconds": body.time_limit_seconds})
    return Status(201, progress_of(solve))


class ReplanFilter(MonthFilter):
    replan_from: dt.date | None = Field(None, description="The first date to plan again. The earliest allowed when left out.")


class ReplanPreview(Schema):
    month: str
    replan_from: dt.date = Field(description="The date this preview is for.")
    earliest: dt.date = Field(description="The first date a re-plan may start from: today, or the month's first day if it is "
                                          "still to come.")
    latest: dt.date = Field(description="The month's last day.")
    in_force: int | None = Field(description="The version in force, which the re-plan keeps close to. Null with no approved "
                                             "roster.")
    summary: Summary
    recorded_cells_differ: int | None = Field(description="Days before the date whose facts differ from the roster in force: "
                                                          "leave recorded since, a pin. Null when it could not be worked out.")
    change_weight: int = Field(description="The hospital's `change` weight: what keeps the re-plan close to the roster in force. "
                                           "Zero keeps nothing.")
    precheck: list[str] | None
    rules_refusal: str | None
    refusal: str | None = Field(description="Why no re-plan can start now, from this date or any.")
    date_refusal: str | None = Field(description="Why this date cannot be the first one planned again.")
    time_limit_default: int
    time_limit_cap: int


def _in_force(unit: Unit, month: dt.date, *, for_update: bool = False) -> Roster | None:
    rows = Roster.objects.select_for_update(of=("self",)) if for_update else Roster.objects
    return rows.select_related("unit").filter(unit=unit, month=month).first()


def _replan_input(unit: Unit, month: dt.date, day: dt.date, roster: Roster | None, time_limit: int) -> Assembled:
    return assemble_replan(unit, month, day, roster.grid if roster else {}, time_limit=time_limit)


@router.get("/{unit_id}/replans/preview", response=ReplanPreview, auth=manager, operation_id="previewReplan")
def preview_replan(request: HttpRequest, unit_id: UUID, filters: Query[ReplanFilter]) -> ReplanPreview:
    """What a re-plan from this date would be given, and whether it can start. Saves nothing."""
    unit = unit_in_scope(request.user, unit_id)
    month = chosen_month(filters.month)
    first, last = replans.earliest(month), membership.last_day_of(month)
    day = filters.replan_from or min(first, last)
    default, cap = _time_limits(unit)
    roster = _in_force(unit, month)
    refusal = replans.why_not(roster, month)
    date_refusal = replans.why_not_from(day, month)
    approved = roster is not None and roster.state == RosterState.APPROVED
    assembled = _replan_input(unit, month, day if not date_refusal else first, roster, default)
    refusal = refusal or _what_the_nurses_stop(assembled)
    rules_refusal = issues = differ = None
    if approved and assembled.nurses and not date_refusal:
        rules_refusal = solver_refusal(assembled.input)
        differ = recorded_cells_differ(assembled.input)
        if rules_refusal is None:
            try:
                issues = solver_client.precheck(assembled.input)
            except SolverRefused as e:
                rules_refusal = str(e)
            except SolverUnreachable:
                pass
    in_force = roster.versions.first() if approved else None
    return ReplanPreview(
        month=month.strftime("%Y-%m"), replan_from=day, earliest=first, latest=last, in_force=in_force.number if in_force else None,
        summary=Summary(nurses=len(assembled.nurses), leave_days=assembled.leave_days,
                        outside_days=sum(map(len, assembled.outside_days.values())), pins=assembled.pins),
        recorded_cells_differ=differ, change_weight=HospitalSettings.load().rules["weights"].get("change", 0),
        precheck=issues, rules_refusal=rules_refusal, refusal=refusal, date_refusal=date_refusal,
        time_limit_default=default, time_limit_cap=cap)


class StartReplanBody(StartSolveBody):
    replan_from: IsoDate = Field(description="The first date to plan again: earlier days are facts.")
    reason: str = Field("", max_length=2000, description="Why, kept with the version it makes, for the unit's manager and the "
                                                         "approvers. One is written when left empty. Never a nurse's, a "
                                                         "notification's or the audit log's.")


@router.post("/{unit_id}/replans", response={201: SolveProgress}, auth=manager, operation_id="startReplan")
def start_replan(request: HttpRequest, unit_id: UUID, body: StartReplanBody) -> Status:
    """An approved month's re-plan from a date, through the same queue as a solve. Its input holds the roster in force as it
    stands now, and the version it was, so its end can tell whether anything moved since (`solves.replans.finish`). Under the
    catalogue's lock, the unit's and the roster's. The audit entry is a solve's, and says nothing of the date or the reason,
    or that it is a re-plan: those are about the roster."""
    month = dt.date.fromisoformat(f"{body.month}-01")
    with transaction.atomic():
        HospitalSettings.load(for_update=True)
        unit = unit_in_scope(request.user, unit_id, for_update=True)
        _, cap = _time_limits(unit)
        if body.time_limit_seconds > cap:
            refuse((("time_limit_seconds",), f"The System Admin's cap is {cap} seconds."))
        roster = _in_force(unit, month, for_update=True)
        stopped_by = replans.why_not(roster, month)
        if stopped_by:
            raise HttpError(409, stopped_by)
        date_refusal = replans.why_not_from(body.replan_from, month)
        if date_refusal:
            refuse((("replan_from",), date_refusal))
        assembled = _replan_input(unit, month, body.replan_from, roster, body.time_limit_seconds)
        stopped_by = _what_the_nurses_stop(assembled)
        if stopped_by:
            raise HttpError(409, stopped_by)
        refusal = solver_refusal(assembled.input)
        if refusal:
            raise HttpError(409, f"The solver refuses the month as it stands, so review the unit rules and the roster in force: "
                                 f"{refusal}")
        solve = Solve.objects.create(unit=unit, month=month, requested_by=request.user,
                                     time_limit_seconds=body.time_limit_seconds, input=assembled.input,
                                     outside_days=assembled.outside_days, replan_from=body.replan_from,
                                     replan_of=roster.versions.first().number, replan_reason=body.reason.strip())
        solve.job_id = enqueue(run_solve, solve_id=str(solve.pk))
        solve.save(update_fields=["job_id"])
        # As a solve's: the System Admin reads the log, and whether a solve was a re-plan is about a roster (section 8).
        record("solve.started", actor=request.user, target=solve.audit_target, request=request,
               after={"time_limit_seconds": body.time_limit_seconds})
    return Status(201, progress_of(solve))


def _solve_of(request: HttpRequest, unit_id: UUID, solve_id: UUID) -> tuple[Unit, Solve]:
    unit = unit_in_scope(request.user, unit_id)
    return unit, get_object_or_404(Solve, pk=solve_id, unit=unit)


@router.get("/{unit_id}/solves/{solve_id}", response=SolveProgress, auth=manager, operation_id="getSolveProgress")
def solveprogress_of(request: HttpRequest, unit_id: UUID, solve_id: UUID) -> SolveProgress:
    """A timer calls this, so it does not count as the manager being there, whatever it answers: a test holds it."""
    not_activity(request)
    _, solve = _solve_of(request, unit_id, solve_id)
    return progress_of(solve)


@router.post("/{unit_id}/solves/{solve_id}/stop", response=SolveProgress, auth=manager, operation_id="stopSolve")
def stop_solve(request: HttpRequest, unit_id: UUID, solve_id: UUID) -> SolveProgress:
    """A queued solve ends here. A running one is the worker's to stop, which keeps the best roster found. Asking again, or
    after the end, changes nothing."""
    unit = unit_in_scope(request.user, unit_id)
    with transaction.atomic():
        solve = get_object_or_404(Solve.objects.select_for_update(), pk=solve_id, unit=unit)
        if solve.state in ACTIVE_STATES and not solve.stop_requested:
            solve.stop_requested = True
            if solve.state == SolveState.QUEUED:
                solve.state = SolveState.CANCELLED
                solve.finished_at = timezone.now()
            solve.save(update_fields=["stop_requested", "state", "finished_at"])
            record("solve.stopped", actor=request.user, target=solve.audit_target, request=request)
    return progress_of(solve)
