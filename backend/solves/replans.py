"""Re-planning an approved month from a date. A re-plan is a solve: the same queue, one active a unit, the same progress,
stop and retry. What differs is its input (`solves.input.assemble_replan`) and its end: its roster becomes a new version of the
roster in force (`rosters.versions.make_version`), passing on purpose the freeze `rosters.drafts.why_frozen` puts on a solve of an
approved month. Everything that decides whether one may start, and what its end does, is here."""
from __future__ import annotations

import datetime as dt
import logging

from audit.log import record
from notifications import sending
from notifications.models import SolveOutcome
from nurses import membership
from rosters.drafts import FROZEN, in_current_codes
from rosters.models import Roster, RosterState, VersionOrigin
from rosters.pins import long_date
from rosters.versions import differences, make_version
from solves.input import changes_since
from solves.models import ACTIVE_STATES, ReplanOutcome, Solve, SolveState, why_not_another_month
from solves.result import has_a_roster_to_keep

logger = logging.getLogger(__name__)

NO_ROSTER_YET = "This month has no roster yet. A re-plan changes an approved roster, so solve it, submit it and have it approved first."
A_DRAFT = ("This roster is a draft, with nothing in force to re-plan. Solve it again instead: each solve of a draft starts afresh "
           "and keeps your pins.")
MONTH_OVER = "This month is over, so its roster in force cannot be re-planned."
ACTIVE = "A solve or a re-plan of this month is already queued or running."


def earliest(month: dt.date) -> dt.date:
    """Days before today have happened, so the first date to plan again is today, or the month's first day if it is still to
    come. Today is allowed: a nurse who calls in sick in the morning is re-planned around that day, and a pin keeps any shift of
    it already worked."""
    return max(membership.today(), month)


def why_not(roster: Roster | None, month: dt.date) -> str | None:
    """Why no re-plan of this month can start, whatever the date: the roster's state, the month, a solve already active."""
    if month < membership.this_month():
        return MONTH_OVER
    if roster is None:
        return NO_ROSTER_YET
    if roster.state == RosterState.DRAFT:
        return A_DRAFT
    if roster.state == RosterState.SUBMITTED:
        return FROZEN[RosterState.SUBMITTED]
    if Solve.objects.filter(unit=roster.unit, month=month, state__in=ACTIVE_STATES).exists():
        return ACTIVE
    return why_not_another_month(roster.unit, month)


def why_not_from(day: dt.date, month: dt.date) -> str | None:
    """Why the date cannot be the first one planned again: outside the month (its first day after is month close's), or
    already over."""
    if day.replace(day=1) != month:
        return f"Choose a date in {month:%B %Y}."
    first = earliest(month)
    if day < first:
        return f"{long_date(day)} is over. Re-plan from {long_date(first)} or later: the days before then are facts."
    return None


def reason_for(solve: Solve) -> str:
    return solve.replan_reason or f"Re-planned from {long_date(solve.replan_from)}."


def finish(solve: Solve, result: dict | None, state: SolveState) -> SolveOutcome:
    """A re-plan's end, holding the catalogue's lock, the month's nurses' (`FOR NO KEY UPDATE`), the unit's and the roster's, in
    that order, as an approval takes them, so no leave, edit or rename passes between deciding and writing. A roster it found
    becomes a new version only while nothing it was given has moved: the version it started from is still in force, and the
    month's nurses, leave and pins are as its input holds them. Otherwise its roster would undo what came since, such as a shift
    leave took off, so it is kept in the history and the manager re-plans again. A rename is no change: the grid is carried to
    today's codes. A roster that changes no cell makes no version and tells no nurse, as a direct edit that changes none is
    refused. A stopped re-plan's best roster goes in force like a finished one's, as a stopped solve's becomes the draft."""
    roster = Roster.objects.select_for_update(of=("self",)).get(unit=solve.unit, month=solve.month)
    stopped = state == SolveState.CANCELLED
    if not has_a_roster_to_keep(result):
        return _ended(solve, None, SolveOutcome.NO_ROSTER)
    if solve.month < membership.this_month():
        return _ended(solve, ReplanOutcome.MONTH_OVER, SolveOutcome.REPLAN_MONTH_OVER)
    latest = roster.versions.first()
    if roster.state != RosterState.APPROVED or latest is None or latest.number != solve.replan_of \
            or changes_since(solve.input, solve.unit, solve.month):
        return _ended(solve, ReplanOutcome.OUTDATED, SolveOutcome.REPLAN_OUTDATED)
    grid = in_current_codes(result["roster"])
    cells = len(differences(roster.grid, grid))
    if not cells:
        return _ended(solve, ReplanOutcome.UNCHANGED, SolveOutcome.REPLAN_UNCHANGED)
    version = make_version(roster, grid, VersionOrigin.REPLAN, solve.requested_by, reason_for(solve), solve=solve)
    # The grid is this solve's now: clearing a pin gives a cell back to it, and the page's details are its.
    roster.solve = solve
    roster.save(update_fields=["solve", "updated_at"])
    record("roster.changed", actor=solve.requested_by, target=solve.audit_target,
           after={"version": version.number, "pins": 0, "cells": cells})
    sending.roster_changed(solve.unit, solve.month, solve.requested_by)
    return _ended(solve, ReplanOutcome.VERSION, SolveOutcome.STOPPED_REPLANNED if stopped else SolveOutcome.REPLANNED)


def _ended(solve: Solve, outcome: ReplanOutcome | None, told: SolveOutcome) -> SolveOutcome:
    if outcome:
        Solve.objects.filter(pk=solve.pk).update(replan_outcome=outcome)
        solve.replan_outcome = outcome
    if outcome in (ReplanOutcome.OUTDATED, ReplanOutcome.MONTH_OVER):
        logger.info("re-plan %s: not applied (%s)", solve.pk, outcome)
    return told
