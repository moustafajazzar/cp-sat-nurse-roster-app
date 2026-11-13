"""A draft's way to the roster in force: the manager submits it, which freezes it (`rosters.drafts.why_frozen`), and the CNO or
the Deputy CNO approves it, which makes version 1, or rejects it with a comment, which returns it to draft. Both are checked
again on the month as it stands with the solver's own code (`rosters.editing.check_saved`), because leave can be recorded
while a roster waits, as the user decided, and a stale draft submits with a warning unless that check fails."""
from __future__ import annotations

import datetime as dt

from django.utils import timezone

from accounts.models import User
from nurses import membership
from nurses.models import NurseProfile
from rosters.drafts import FROZEN
from rosters.editing import Checked
from rosters.models import Decision, Roster, RosterState, RosterVersion, Submission, VersionOrigin
from rosters.versions import make_version
from solves.input import MONTH_DATES, Assembled, assemble, changes_since
from solves.models import ACTIVE_STATES, Solve
from units.models import Unit

SOLVE_ACTIVE = ("A solve of this month is queued or running, and would replace the draft when it ends. Stop it or wait for it "
                "first.")
# What the CNO and the Deputy CNO see of a unit's month: a draft is the unit management's alone.
SEEN_BY_APPROVERS = (RosterState.SUBMITTED, RosterState.APPROVED)


def _plural(count: int, one: str) -> str:
    return f"{count} {one}{'' if count == 1 else 's'}"


def unsound(checked: Checked | None) -> str | None:
    """What stops a grid going on, in words that end a sentence beginning "It". None for a sound one; `checked` is None for a
    draft neither edited nor out of date, whose solve's own answer is that it breaks no hard rule."""
    if checked is None:
        return None
    if checked.reason:
        return f"cannot be checked on the month as it stands. The solver's code says: {checked.reason.rstrip('.')}."
    if not checked.answer["ok"]:
        return f"breaks {_plural(len(checked.answer['violations']), 'hard rule')} on the month as it stands, listed with it."
    return None


def why_not_submit(roster: Roster, checked: Checked | None) -> str | None:
    if roster.state != RosterState.DRAFT:
        return FROZEN[roster.state]
    if roster.month < membership.this_month():
        return "This month is over, so its draft cannot be submitted."
    if Solve.objects.filter(unit=roster.unit, month=roster.month, state__in=ACTIVE_STATES).exists():
        return SOLVE_ACTIVE
    broken = unsound(checked)
    if broken:
        return f"This draft {broken} Change it by hand or solve again, then submit it."
    return None


def why_not_decide(roster: Roster | None) -> str | None:
    if roster is None or roster.state == RosterState.DRAFT:
        return "This month has no roster waiting for a decision."
    if roster.state == RosterState.APPROVED:
        approved = approval(roster)
        who = f" by {name_of(approved.decided_by)}" if approved else ""
        return f"This roster has been approved already{who}."
    return None


def why_not_approve_before_checking(roster: Roster | None) -> str | None:
    """What refuses an approval before the grid is checked. A month that is over is refused as submitting refuses it:
    approved late, after the month after it closed, it would be an approved month the close can never reach, and every later
    close of the unit would wait on it for good. Rejecting it stays allowed, so it no longer waits."""
    refusal = why_not_decide(roster)
    if refusal:
        return refusal
    if roster.month < membership.this_month():
        return ("This month is over, so its roster cannot be approved. Reject it with a comment, so it no longer waits for a "
                "decision.")
    return None


def why_not_approve(roster: Roster | None, checked: Checked | None) -> str | None:
    refusal = why_not_approve_before_checking(roster)
    if refusal:
        return refusal
    broken = unsound(checked)
    if broken:
        return f"This roster {broken} Reject it with a comment, so its manager can put it right."
    return None


def name_of(user: User) -> str:
    return user.full_name or user.username


def waiting(roster: Roster) -> Submission | None:
    return roster.submissions.filter(decision="").select_related("submitted_by").first()


def last_rejection(roster: Roster) -> Submission | None:
    return roster.submissions.filter(decision=Decision.REJECTED).select_related("decided_by").first()


def approval(roster: Roster) -> Submission | None:
    return roster.submissions.filter(decision=Decision.APPROVED).select_related("decided_by").first()


def lock_the_months_nurses(unit: Unit, month: dt.date, roster: Roster | None) -> list[NurseProfile]:
    """Every nurse leave could be recorded for in this month, under the lock a leave write takes (the user and the profile), so
    that none lands between checking a roster and submitting or approving it. Take it after the catalogue's and before the
    unit's."""
    in_grid = list(roster.grid) if roster else []
    ids = set(membership.placed_in(unit, month).values_list("pk", flat=True))
    ids |= set(NurseProfile.objects.filter(user__username__in=in_grid).values_list("pk", flat=True))
    return list(NurseProfile.objects.select_for_update(of=("self", "user"), no_key=True).select_related("user")
                .filter(pk__in=ids).order_by("pk"))


MONTH_AT_SUBMIT_KEYS = ("id", "gender", "leave", "pinned", "on_probation", "preceptor")


def submit(roster: Roster, by: User) -> Submission:
    """Keeps the month as it stood, in the words of a solve input and no more, so that what changes while it waits can be told
    from what the manager was warned of: a second leave of a nurse already warned of is news."""
    roster.state = RosterState.SUBMITTED
    roster.save(update_fields=["state"])  # `updated_at` stays: it is when a solve or a restore last replaced the grid.
    assembled = assemble(roster.unit, roster.month)
    # Her skills always, none included, so that one recorded while it waits is news (`solves.input._compares_skills`).
    month = [{key: row[key] for key in MONTH_AT_SUBMIT_KEYS if key in row} | {"skills": row.get("skills", {})}
             for row in assembled.nurses]
    # The month's own dates too, so that a Ramadan day or a holiday changed while it waits is news.
    dates = {key: assembled.input[key] for key in MONTH_DATES}
    return Submission.objects.create(roster=roster, submitted_by=by, month_at_submit=month, dates_at_submit=dates)


def new_since(submission: Submission, assembled: Assembled | None = None) -> list[dict]:
    """`changes_since` against the month as it stood at the submit; `assembled` is the month now, if the caller has it."""
    roster = submission.roster
    return changes_since({"nurses": submission.month_at_submit} | (submission.dates_at_submit or {}), roster.unit,
                         roster.month, since=submission.submitted_at, assembled=assembled, month_dates=True)


def _decide(roster: Roster, decision: Decision, by: User, comment: str = "") -> Submission:
    submission = waiting(roster)
    submission.decision, submission.decided_by, submission.decided_at, submission.comment = decision, by, timezone.now(), comment
    submission.save(update_fields=["decision", "decided_by", "decided_at", "comment"])
    return submission


def approve(roster: Roster, by: User) -> RosterVersion:
    """The first approval makes version 1, with origin `solve` as section 4 has it, and the solve the grid came from. The grid is
    the roster's as approved, hand edits and all."""
    _decide(roster, Decision.APPROVED, by)
    roster.state = RosterState.APPROVED
    roster.save(update_fields=["state"])
    return make_version(roster, roster.grid, VersionOrigin.SOLVE, by, solve=roster.solve)


def reject(roster: Roster, by: User, comment: str) -> Submission:
    roster.state = RosterState.DRAFT
    roster.save(update_fields=["state"])
    return _decide(roster, Decision.REJECTED, by, comment)
