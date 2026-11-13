"""Month close and the carry-over chain. A close is not a solve: it is worked out in the worker's own process with no
search, so it is in neither solve history and never on the solver's one job."""
from django.conf import settings
from django.db import models
from django.db.models.functions import UUID7


class MonthClose(models.Model):
    """One close of a unit's month: the roster in force, with every recorded leave and pin, made fact. Nothing rewrites it: closing
    it again makes a new row, and the latest of a unit's month is the close in force. `input` and `result` are what it was given and
    what it found, as a solve's are, so a correction made since leaves a closed month's figures as they were."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    unit = models.ForeignKey("units.Unit", on_delete=models.PROTECT, related_name="closes")
    month = models.DateField(help_text="The first day of the month closed.")
    closed_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
                                  help_text="The manager who closed it again. Null when the schedule closed it.")
    version = models.PositiveIntegerField(help_text="The roster version in force that was closed.")
    input = models.JSONField(help_text="The solve input it was worked out on: the roster in force as `published`, and the first "
                                       "day after the month as `replan_from`, so that every day is a fact.")
    result = models.JSONField(help_text='{"facts": the month as it happened, "facts_broke": the hard rules the facts broke, "cost", '
                                        '"nurses": each nurse\'s hours accounting and carry-over, as a result\'s `nurses` reads}.')

    class Meta:
        ordering = ["-closed_at", "-id"]
        constraints = [models.CheckConstraint(condition=models.Q(month__day=1), name="closes_close_month_starts_a_month")]

    @property
    def audit_target(self) -> str:
        return f"{self.unit.name}, {self.month:%Y-%m}"


class CarrySource(models.TextChoices):
    OPENING = "opening", "The nurse's opening values: nothing has closed for her yet."
    PROJECTED = "projected", "Worked out from the roster in force of a month not closed."
    CLOSED = "closed", "Worked out at the month's close."


class CarryOver(models.Model):
    """What a nurse's month hands to the next, in the solve input's own field names (`closes.chain.CARRIED`). A closed one belongs
    to its close and is never rewritten. A projected one is the latest worked out for that month (`closes.tasks`); what a solve
    starts from is worked out again as it is assembled, so a stored projection is a record, not an input. Opening values are the
    nurse profile's own and have no row."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    nurse = models.ForeignKey("nurses.NurseProfile", on_delete=models.PROTECT, related_name="carry_overs")
    unit = models.ForeignKey("units.Unit", on_delete=models.PROTECT, related_name="+",
                             help_text="The unit whose month it is: a transferred nurse's carry-over follows her.")
    month = models.DateField(help_text="The first day of the month it is carried out of.")
    source = models.CharField(max_length=9, choices=CarrySource)
    close = models.ForeignKey(MonthClose, on_delete=models.PROTECT, null=True, blank=True, related_name="carry_overs")
    values = models.JSONField()
    worked_out_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-month", "-worked_out_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(month__day=1), name="closes_carry_over_month_starts_a_month"),
            models.CheckConstraint(condition=~models.Q(source="opening"), name="closes_no_opening_row"),
            models.CheckConstraint(condition=models.Q(source="closed", close__isnull=False)
                                   | models.Q(source="projected", close__isnull=True), name="closes_closed_has_its_close"),
            models.UniqueConstraint(fields=["close", "nurse"], condition=models.Q(source="closed"),
                                    name="closes_one_closed_carry_over_a_close_and_nurse"),
            models.UniqueConstraint(fields=["nurse", "month"], condition=models.Q(source="projected"),
                                    name="closes_one_projected_carry_over_a_nurse_month"),
        ]
