"""Swaps: a nurse's proposal to change a set of cells of her unit's approved roster, her
own and any colleagues' needed to make it work. Every colleague whose cells change answers it, then the unit's manager decides;
approved, its cells become pinned cells of origin `swap` in a new version. Nothing here is deleted: a swap keeps its final state,
every cell it named, each colleague's answer and who ended it."""
from django.conf import settings
from django.db import models
from django.db.models.functions import UUID7


class SwapState(models.TextChoices):
    PENDING = "pending"
    AWAITING_MANAGER = "awaiting_manager"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


# A swap still waiting on its colleagues or the manager: the only ones a passing date or a new version can expire, and one a
# cell.
WAITING = (SwapState.PENDING, SwapState.AWAITING_MANAGER)


class Expiry(models.TextChoices):
    """Why a swap expired, which only the unit's manager reads: a cell changed by a version can be leave."""
    DATE_PASSED = "date_passed"
    CELL_CHANGED = "cell_changed"


class Swap(models.Model):
    """`proposed_on` is the roster version in force when it was proposed, whose cells `SwapCell.before` holds. `ended_by` is who
    rejected, withdrew or decided it; null while it waits and when it expired. `reason` is the manager's, for a decision,
    read by the proposer and every colleague in it; `note` is the proposer's own, read by the same people and the manager."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    unit = models.ForeignKey("units.Unit", on_delete=models.PROTECT, related_name="swaps")
    roster = models.ForeignKey("rosters.Roster", on_delete=models.PROTECT, related_name="swaps")
    proposed_on = models.ForeignKey("rosters.RosterVersion", on_delete=models.PROTECT, related_name="+",
                                    help_text="The version in force when it was proposed.")
    proposer = models.ForeignKey("nurses.NurseProfile", on_delete=models.PROTECT, related_name="swaps_proposed")
    state = models.CharField(max_length=16, choices=SwapState, default=SwapState.PENDING)
    note = models.TextField(blank=True, help_text="The proposer's words to the colleagues and the manager. Optional.")
    proposed_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    expired_why = models.CharField(max_length=12, choices=Expiry, blank=True)
    reason = models.TextField(blank=True, help_text="The manager's, for an approval or a rejection.")
    made_version = models.ForeignKey("rosters.RosterVersion", on_delete=models.PROTECT, null=True, blank=True,
                                     related_name="+", help_text="The version its approval made.")

    class Meta:
        ordering = ["-proposed_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=~models.Q(state="expired") | ~models.Q(expired_why=""),
                                   name="swaps_an_expiry_says_why"),
        ]

    @property
    def waiting(self) -> bool:
        return self.state in WAITING


class SwapCell(models.Model):
    """One cell the swap changes: a nurse's date, from `before` to `after`, each a shift or null for a day off. Shifts are keys,
    so a rename needs nothing. `pin` is the pinned cell its approval made."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    swap = models.ForeignKey(Swap, on_delete=models.PROTECT, related_name="cells")
    nurse = models.ForeignKey("nurses.NurseProfile", on_delete=models.PROTECT, related_name="+")
    date = models.DateField()
    before = models.ForeignKey("hospital.Shift", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    after = models.ForeignKey("hospital.Shift", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    pin = models.OneToOneField("rosters.PinnedCell", on_delete=models.PROTECT, null=True, blank=True, related_name="swap_cell")

    class Meta:
        ordering = ["date", "id"]
        constraints = [models.UniqueConstraint(fields=["swap", "nurse", "date"], name="swaps_a_cell_once_a_swap")]

    @property
    def before_code(self) -> str:
        return self.before.code if self.before else ""

    @property
    def after_code(self) -> str:
        return self.after.code if self.after else ""


class Answer(models.TextChoices):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SwapParticipant(models.Model):
    """A colleague whose cells the swap changes, and their answer. The proposer is not one."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    swap = models.ForeignKey(Swap, on_delete=models.PROTECT, related_name="participants")
    nurse = models.ForeignKey("nurses.NurseProfile", on_delete=models.PROTECT, related_name="swaps_joined")
    answer = models.CharField(max_length=8, choices=Answer, default=Answer.PENDING)
    answered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["nurse__user__full_name", "id"]
        constraints = [models.UniqueConstraint(fields=["swap", "nurse"], name="swaps_a_colleague_once_a_swap")]
