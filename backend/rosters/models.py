import datetime as dt

from django.conf import settings
from django.db import models
from django.db.models.functions import UUID7


class RosterState(models.TextChoices):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"


class PinOrigin(models.TextChoices):
    MANAGER = "manager"
    REQUEST = "request"
    SWAP = "swap"
    COVER = "cover"


class PinEnded(models.TextChoices):
    CLEARED = "cleared"
    REPLACED = "replaced", "Another pin took its place."
    LEAVE = "leave", "Leave recorded on its date, which wins over a pin."


class VersionOrigin(models.TextChoices):
    SOLVE = "solve"
    EDIT = "edit"
    REPLAN = "replan"
    SWAP = "swap"
    REQUEST = "request"
    COVER = "cover"


class Decision(models.TextChoices):
    APPROVED = "approved"
    REJECTED = "rejected"


class Roster(models.Model):
    """A unit's plan for one month. `grid` is the one in use and follows a shift's rename, unlike the stored result it came from.
    Until the first approval each solve replaces the grid, and the solve history keeps what it replaced. Once approved, `grid` is
    the latest version's (`RosterVersion`), under the codes in use now. Submitting freezes it (`rosters.drafts.why_frozen`)."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    unit = models.ForeignKey("units.Unit", on_delete=models.PROTECT, related_name="rosters")
    month = models.DateField(help_text="The first day of the month.")
    state = models.CharField(max_length=9, choices=RosterState, default=RosterState.DRAFT)
    grid = models.JSONField(help_text='{hospital ID: {date: shift code or ""}}, the shape of a result\'s roster.')
    solve = models.ForeignKey("solves.Solve", on_delete=models.PROTECT, related_name="+",
                              help_text="The solve that produced this grid.")
    restored_at = models.DateTimeField(null=True, blank=True, help_text="When the manager last restored `solve`'s result as "
                                                                        "this grid. A solve replacing it clears this.")
    updated_at = models.DateTimeField(auto_now=True, help_text="When a solve or a restore last replaced the grid. A hand edit "
                                                               "does not move it, so a failed solve before one is still news.")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["unit", "month"], name="rosters_one_roster_a_unit_month"),
            models.CheckConstraint(condition=models.Q(month__day=1), name="rosters_roster_month_starts_a_month"),
        ]

    @property
    def audit_target(self) -> str:
        """As a solve of the same unit and month names itself in the audit log."""
        return f"{self.unit.name}, {self.month:%Y-%m}"


class Submission(models.Model):
    """One sending of a draft for a decision, and the decision. Nothing is deleted, so every rejection's comment is kept, which
    the audit log cannot hold: a comment may name a nurse or a leave."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    roster = models.ForeignKey(Roster, on_delete=models.PROTECT, related_name="submissions")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    submitted_at = models.DateTimeField(auto_now_add=True)
    month_at_submit = models.JSONField(default=list, help_text="The month's nurses as a solve input would hold them at the submit: "
                                                               "each one's hospital ID, gender, credited days and the pins that "
                                                               "count, and no leave type. What has changed since, which the manager "
                                                               "was not warned of, is compared with this.")
    dates_at_submit = models.JSONField(null=True, blank=True,
                                       help_text='The month\'s Ramadan days and public holidays at the submit, as a solve input '
                                                 'holds them: {"ramadan_days": [...], "public_holidays": [...]}. Null on a '
                                                 'submission made before they were recorded, which compares none.')
    decision = models.CharField(max_length=8, choices=Decision, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    comment = models.TextField(blank=True, help_text="Why it was rejected. Required then, and seen by the unit's manager.")

    class Meta:
        ordering = ["-submitted_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["roster"], condition=models.Q(decision=""),
                                    name="rosters_one_undecided_submission_a_roster"),
            models.CheckConstraint(condition=~models.Q(decision="rejected") | ~models.Q(comment=""),
                                   name="rosters_a_rejection_has_a_comment"),
        ]


class RosterVersion(models.Model):
    """One saved state of an approved roster. The first approval makes number 1, with origin `solve`; its grid is the roster's as
    approved, so it can hold hand edits: the pins, and the solve history, say where each cell came from. The latest is in force.
    A version keeps the codes it was saved with, as a stored result does, and is read through `rosters.drafts.in_current_codes`."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    roster = models.ForeignKey(Roster, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    grid = models.JSONField(help_text='{hospital ID: {date: shift code or ""}}.')
    origin = models.CharField(max_length=7, choices=VersionOrigin)
    solve = models.ForeignKey("solves.Solve", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
                              help_text="The solve behind the grid, when one produced it.")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["roster", "-number"]
        constraints = [models.UniqueConstraint(fields=["roster", "number"], name="rosters_one_version_number_a_roster")]


class PinnedCellQuerySet(models.QuerySet):
    def current(self) -> "PinnedCellQuerySet":
        return self.filter(removed_at__isnull=True)

    def in_month(self, month: dt.date) -> "PinnedCellQuerySet":
        return self.filter(date__year=month.year, date__month=month.month)


class PinnedCell(models.Model):
    """A nurse's shift, or day off when `shift` is null, on one date, fixed by a unit's manager or by an accepted request or
    swap: the solver may not change it. The shift is a key, so a rename needs nothing here. The row belongs to the unit whose
    month it is, so a nurse transferred out does not take it along. Nothing is hard-deleted: a pin that ends keeps its row, since
    the audit log may say nothing of what it held (`rosters.pins`)."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    unit = models.ForeignKey("units.Unit", on_delete=models.PROTECT, related_name="pins")
    nurse = models.ForeignKey("nurses.NurseProfile", on_delete=models.PROTECT, related_name="pins")
    date = models.DateField()
    shift = models.ForeignKey("hospital.Shift", on_delete=models.PROTECT, null=True, blank=True, related_name="pins")
    origin = models.CharField(max_length=7, choices=PinOrigin, default=PinOrigin.MANAGER)
    pinned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    pinned_at = models.DateTimeField(auto_now_add=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    removed_why = models.CharField(max_length=8, choices=PinEnded, blank=True)

    objects = PinnedCellQuerySet.as_manager()

    class Meta:
        ordering = ["date", "id"]
        constraints = [
            models.UniqueConstraint(fields=["nurse", "date"], condition=models.Q(removed_at__isnull=True),
                                    name="rosters_one_current_pin_a_nurse_date"),
        ]

    @property
    def code(self) -> str:
        return self.shift.code if self.shift else ""
