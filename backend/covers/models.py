"""Covers: one more nurse needed on one shift on one date of a unit's roster in force. Leave that takes a
shift off the roster in force opens one by itself; the unit's manager may open one by hand. The manager offers it to staff nurses
who could take it, and the first to accept takes it as a pinned cell in a new version. Nothing here is deleted: a
cover keeps its final state, who ended it and when, and every nurse offered it with her answer.

A cover is about the shift-day, never about whose absence made it short: the absent nurse is not on the row. The version that
opened it says so to the manager, who reads its reason anyway."""
from django.conf import settings
from django.db import models
from django.db.models.functions import UUID7


class CoverState(models.TextChoices):
    OPEN = "open"
    OFFERED = "offered"
    TAKEN = "taken"
    WITHDRAWN = "withdrawn"
    FILLED = "filled"
    EXPIRED = "expired"


# A cover still waiting for a nurse: the only ones a date passing, a withdrawal or a new version can end.
WAITING = (CoverState.OPEN, CoverState.OFFERED)


class Opened(models.TextChoices):
    LEAVE = "leave", "Leave took the shift off the roster in force."
    MANAGER = "manager", "The unit's manager opened it by hand."


class Cover(models.Model):
    """`opened_on` is the version in force when it was opened: for one opened by leave, the version the leave made. `ended_by`
    is who withdrew it; null while it waits, and when it was taken, filled or expired, which are nobody's act but the taker's,
    who is `taker`. `note` is the manager's, written when offering it, read by the nurses offered it."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    unit = models.ForeignKey("units.Unit", on_delete=models.PROTECT, related_name="covers")
    roster = models.ForeignKey("rosters.Roster", on_delete=models.PROTECT, related_name="covers")
    opened_on = models.ForeignKey("rosters.RosterVersion", on_delete=models.PROTECT, related_name="+",
                                  help_text="The version in force when it was opened; for leave, the version the leave made.")
    date = models.DateField()
    shift = models.ForeignKey("hospital.Shift", on_delete=models.PROTECT, related_name="+")
    opened = models.CharField(max_length=8, choices=Opened)
    opened_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(blank=True, help_text="The manager's words to the nurses offered it. Optional.")
    state = models.CharField(max_length=10, choices=CoverState, default=CoverState.OPEN)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    made_version = models.ForeignKey("rosters.RosterVersion", on_delete=models.PROTECT, null=True, blank=True,
                                     related_name="+", help_text="The version its acceptance made.")
    taker = models.ForeignKey("nurses.NurseProfile", on_delete=models.PROTECT, null=True, blank=True, related_name="covers_taken")

    class Meta:
        ordering = ["-opened_at", "-id"]
        indexes = [models.Index(fields=["roster", "date", "shift", "state"], name="covers_of_a_shift_day")]
        constraints = [
            models.CheckConstraint(condition=~models.Q(state="taken") | models.Q(taker__isnull=False, made_version__isnull=False),
                                   name="covers_a_taken_cover_has_its_taker"),
        ]

    @property
    def waiting(self) -> bool:
        return self.state in WAITING


class Answer(models.TextChoices):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class CoverOffer(models.Model):
    """A nurse a cover was offered to, and her answer. Never removed: a manager adds nurses to an offer, never takes one off."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    cover = models.ForeignKey(Cover, on_delete=models.PROTECT, related_name="offers")
    nurse = models.ForeignKey("nurses.NurseProfile", on_delete=models.PROTECT, related_name="cover_offers")
    offered_at = models.DateTimeField(auto_now_add=True)
    answer = models.CharField(max_length=8, choices=Answer, default=Answer.PENDING)
    answered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["nurse__user__full_name", "id"]
        constraints = [models.UniqueConstraint(fields=["cover", "nurse"], name="covers_a_nurse_once_a_cover")]
