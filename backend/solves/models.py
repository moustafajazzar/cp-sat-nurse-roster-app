import datetime as dt
from typing import TYPE_CHECKING, Literal

from django.conf import settings
from django.db import models
from django.db.models.functions import UUID7

if TYPE_CHECKING:
    from units.models import Unit


class SolveState(models.TextChoices):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATES = (SolveState.QUEUED, SolveState.RUNNING)


class SolveFailure(models.TextChoices):
    """Why a solve gave no answer, as one of a few fixed kinds the System Admin reads in place of `Solve.error`, whose words
    can name a nurse and a date. A label holds no name, date or rule value, and neither may a kind added later. The page's
    words are these, in `frontend/src/lib/solve-words.ts`: change both together."""
    REFUSED = "refused", "The solver refused the rules it was given: the unit's or the hospital's rules need review."
    UNREACHABLE = "unreachable", "The solver could not be reached."
    LOST = "lost", "The solver restarted and lost the solve."
    OVERRUN = "overrun", "The solver ran well past the time limit and was given up on."
    SOLVER_ERROR = "solver_error", "The solver stopped with an error of its own."
    STUCK = "stuck", "The solver would not stop an earlier job, so this one could not start."
    WORKER_LOST = "worker_lost", "The worker stopped during the solve and could not take it up again."
    UNEXPECTED = "unexpected", "The worker met an error of its own."
    # Before the kinds were told apart, every failure but a refusal was this one.
    SOLVER = "solver", "The solver failed or could not be reached (from before the kinds were told apart)."


# How the API types a `SolveFailure`.
SolveFailureWord = Literal[SolveFailure.REFUSED, SolveFailure.UNREACHABLE, SolveFailure.LOST, SolveFailure.OVERRUN,
                           SolveFailure.SOLVER_ERROR, SolveFailure.STUCK, SolveFailure.WORKER_LOST, SolveFailure.UNEXPECTED,
                           SolveFailure.SOLVER]


class ReplanOutcome(models.TextChoices):
    """What a re-plan's end did to the roster in force. Blank until it ends, and for a re-plan that kept no roster: no roster
    found, one breaking a hard rule, a failure, or a stop before any was found."""
    VERSION = "version", "Its roster became a new version in force."
    UNCHANGED = "unchanged", "Its roster changed no cell, so no version was made."
    OUTDATED = "outdated", "The roster in force or the month's facts changed while it waited or ran, so it was not applied."
    MONTH_OVER = "month_over", "The month ended while it waited or ran, so it was not applied."


# How the API types a `ReplanOutcome`.
ReplanOutcomeWord = Literal[ReplanOutcome.VERSION, ReplanOutcome.UNCHANGED, ReplanOutcome.OUTDATED, ReplanOutcome.MONTH_OVER]


class Solve(models.Model):
    """One run of the solver on one solve input, kept for good. `input` and `result` are what the solver was given and
    what it answered, and nothing rewrites them, not even a shift's rename: the grid in use is on `rosters.Roster`.

    `succeeded` means the solver ran to its end, which may have found no roster: `result` then says why. `cancelled` may
    still hold the best roster found. Only `failed` means the solver gave no answer, and `failure` says how."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    unit = models.ForeignKey("units.Unit", on_delete=models.PROTECT, related_name="solves")
    month = models.DateField(help_text="The first day of the month.")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(auto_now_add=True)
    time_limit_seconds = models.PositiveIntegerField()
    state = models.CharField(max_length=9, choices=SolveState, default=SolveState.QUEUED)
    # The queue's own id, which is the order solves run in: see `queue_ahead`.
    job_id = models.BigIntegerField(null=True, blank=True)
    solver_job_id = models.CharField(max_length=64, blank=True, help_text="Only while the solver holds the job in memory.")
    attempts = models.PositiveSmallIntegerField(default=0, help_text="How many times the solver was given this solve.")
    stop_requested = models.BooleanField(default=False)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    elapsed_seconds = models.FloatField(default=0)
    best_cost = models.BigIntegerField(null=True, blank=True)
    improvements = models.PositiveIntegerField(default=0)
    input = models.JSONField(help_text="The complete solve input, as the solver receives it.")
    outside_days = models.JSONField(
        default=dict, help_text="{hospital ID: [dates]}: which of a nurse's credited days were outside their dates, which the "
                                "input does not say, as it lists them with leave. No dates of leave, and no type.")
    result = models.JSONField(null=True, blank=True, help_text="The solver's result, as it answered.")
    failure = models.CharField(max_length=12, choices=SolveFailure, blank=True)
    error = models.TextField(blank=True, help_text="Why the last attempt failed, also when a retry then worked.")
    # A re-plan of an approved month: days before `replan_from` are facts, and its roster becomes a new version.
    replan_from = models.DateField(null=True, blank=True, help_text="Set only on a re-plan: the first date it plans again.")
    replan_of = models.PositiveIntegerField(null=True, blank=True, help_text="The version in force when the re-plan started, "
                                                                             "which its input holds as `published`.")
    replan_reason = models.TextField(blank=True, help_text="The manager's reason, for the version it makes. Read by the unit's "
                                                           "manager and the approvers only, as a version's reason is.")
    replan_outcome = models.CharField(max_length=10, choices=ReplanOutcome, blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(month__day=1), name="solves_solve_month_starts_a_month"),
            # A second one would only replace the first one's grid when it ends.
            models.UniqueConstraint(fields=["unit", "month"], condition=models.Q(state__in=ACTIVE_STATES),
                                    name="solves_one_active_solve_a_unit_month"),
        ]

    @property
    def audit_target(self) -> str:
        return f"{self.unit.name}, {self.month:%Y-%m}"

    @property
    def queue_ahead(self) -> int | None:
        """How many solves of any unit go first. Worked out when asked rather than written by the worker, so it cannot be
        out of date when one ahead is stopped. None once this one has started."""
        if self.state != SolveState.QUEUED:
            return None
        return Solve.objects.filter(state__in=ACTIVE_STATES, job_id__lt=self.job_id).count()


def why_not_another_month(unit: "Unit", month: dt.date) -> str | None:
    """A solve or re-plan of another of the unit's months already queued or running, named, so a start of this one is refused.
    The queue is hospital-wide and first come first served, so one active solve a unit keeps
    one unit from holding every other unit's solve behind one for each month it may solve (to 2076). `Meta.constraints` stays one a
    unit's month: one a unit would fail the migration on an installation with two months waiting. Both start routes hold the unit's row
    `FOR UPDATE`, so two starts of one unit read this in turn."""
    other = (Solve.objects.filter(unit=unit, state__in=ACTIVE_STATES).exclude(month=month)
             .only("month", "replan_from").order_by("month").first())
    if other is None:
        return None
    kind = "re-plan" if other.replan_from else "solve"
    return (f"A {kind} of {other.month:%B %Y} is already queued or running. A unit solves one month at a time, so stop it or wait "
            "for it first.")
