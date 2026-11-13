"""Request windows and the requests nurses make in them. Nothing here is deleted: a
request keeps its final state, and every step it took, with the reason given, stays as a row of its own."""
from django.conf import settings
from django.db import models
from django.db.models.functions import UUID7


class WindowState(models.TextChoices):
    SCHEDULED = "scheduled"
    OPEN = "open"
    CLOSED = "closed"


class RequestWindow(models.Model):
    """The days, from `opens_on` to `closes_on` on the hospital's clock and both included, in which a unit's nurses make
    requests for one target month. `state` is what the hourly job (`requesting.tasks`) or the last write found; what the dates
    say now is `requesting.windows.state_today`, which every read uses, so the page never waits on the job. At most one window
    of a unit is scheduled or open at a time; one closed, a unit may open another, for the same month too."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    unit = models.ForeignKey("units.Unit", on_delete=models.PROTECT, related_name="request_windows")
    month = models.DateField(help_text="The target month's first day.")
    opens_on = models.DateField()
    closes_on = models.DateField(help_text="The last day requests can be made.")
    state = models.CharField(max_length=9, choices=WindowState, default=WindowState.SCHEDULED)
    opened_at = models.DateTimeField(null=True, blank=True, help_text="When it opened and its nurses were told.")
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                  related_name="+", help_text="The manager who closed it early. Null when its date did.")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-opens_on", "-created_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(month__day=1), name="requesting_window_month_starts_a_month"),
            models.CheckConstraint(condition=models.Q(closes_on__gte=models.F("opens_on")),
                                   name="requesting_window_closes_after_it_opens"),
            models.UniqueConstraint(fields=["unit"], condition=~models.Q(state="closed"),
                                    name="requesting_one_window_waiting_or_open_a_unit"),
        ]

    @property
    def audit_target(self) -> str:
        """As a roster of the same unit and month names itself in the audit log."""
        return f"{self.unit.name}, {self.month:%Y-%m}"


def what_of(shift) -> str:
    """What a request asks for or promises, in the words every page and notification uses."""
    return f"{shift.display_name} ({shift.code})" if shift else "a day off"


class RequestState(models.TextChoices):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    CANCELLED = "cancelled"
    REPLACED_BY_LEAVE = "replaced_by_leave"
    EXPIRED = "expired", "Still pending when its month closed."


# A request that still asks for something: the only ones that count against the cap, and one a date.
LIVE = (RequestState.PENDING, RequestState.ACCEPTED)


class Request(models.Model):
    """A nurse's request for a day off (`shift` null) or a shift on one date of the window's month. While accepted, `pin` is
    the pinned cell, of origin `request`, that keeps it in every solve: the pin holds what is promised, which the manager may
    have changed since (`steps`)."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    window = models.ForeignKey(RequestWindow, on_delete=models.PROTECT, related_name="requests")
    nurse = models.ForeignKey("nurses.NurseProfile", on_delete=models.PROTECT, related_name="requests")
    date = models.DateField()
    shift = models.ForeignKey("hospital.Shift", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
                              help_text="The shift asked for. Null for a day off.")
    state = models.CharField(max_length=17, choices=RequestState, default=RequestState.PENDING)
    pin = models.OneToOneField("rosters.PinnedCell", on_delete=models.PROTECT, null=True, blank=True, related_name="request")
    made_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "made_at"]
        constraints = [
            models.UniqueConstraint(fields=["nurse", "date"], condition=models.Q(state__in=["pending", "accepted"]),
                                    name="requesting_one_live_request_a_nurse_date"),
            models.CheckConstraint(condition=models.Q(state="accepted") | models.Q(pin__isnull=True),
                                   name="requesting_only_an_accepted_request_holds_a_pin"),
        ]

    @property
    def code(self) -> str:
        return self.shift.code if self.shift else ""

    @property
    def what(self) -> str:
        return what_of(self.shift)

    @property
    def state_words(self) -> str:
        """"pending", "replaced by leave": for a sentence."""
        return self.state.replace("_", " ")


class Step(models.TextChoices):
    MADE = "made"
    WITHDRAWN = "withdrawn"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EDITED = "edited"
    CANCELLED = "cancelled"
    REPLACED_BY_LEAVE = "replaced_by_leave"
    EXPIRED = "expired"


class RequestStep(models.Model):
    """One step a request took: who, when, and the manager's reason, which can be about someone's health, so it is read only by
    the nurse and the unit's manager, on their pages, and never kept in a notification or the audit log."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    request = models.ForeignKey(Request, on_delete=models.PROTECT, related_name="steps")
    step = models.CharField(max_length=17, choices=Step)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
                           help_text="Null only for `expired`, which the month's close takes, with no hand in it.")
    at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(blank=True)
    pin = models.ForeignKey("rosters.PinnedCell", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
                            help_text="The pin an acceptance or an edit made, which says what it promised.")

    class Meta:
        ordering = ["at", "id"]


class PreferenceKind(models.TextChoices):
    DAY_OFF = "day_off", "A day off"
    WORKING_DAY = "working_day", "A working day, of any shift"


class PreferenceState(models.TextChoices):
    MADE = "made", "Made"
    WITHDRAWN = "withdrawn", "Withdrawn"
    ASKED_INSTEAD = "asked_instead", "Asked for as a request"


class Preference(models.Model):
    """What a nurse would rather have on one date of the window's month: nobody decides it and it is never a pin; the solve
    input lists the live ones (`made`) as the days she asked off or to work, which the solver keeps at the weight's price.
    Whether its date is on leave is read, never stored, since leave may be removed. Nothing is deleted: one ended keeps its
    final state. Only the nurse and her unit's manager read it, and the audit log keeps neither its date nor its kind."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    window = models.ForeignKey(RequestWindow, on_delete=models.PROTECT, related_name="preferences")
    nurse = models.ForeignKey("nurses.NurseProfile", on_delete=models.PROTECT, related_name="preferences")
    date = models.DateField()
    kind = models.CharField(max_length=11, choices=PreferenceKind)
    state = models.CharField(max_length=13, choices=PreferenceState, default=PreferenceState.MADE)
    made_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    request = models.OneToOneField(Request, on_delete=models.PROTECT, null=True, blank=True, related_name="from_preference",
                                   help_text="The request she asked for instead.")

    class Meta:
        ordering = ["date", "made_at"]
        constraints = [
            models.UniqueConstraint(fields=["nurse", "date"], condition=models.Q(state="made"),
                                    name="requesting_one_live_preference_a_nurse_date"),
            models.CheckConstraint(condition=models.Q(state="made") | models.Q(ended_at__isnull=False),
                                   name="requesting_an_ended_preference_says_when"),
            models.CheckConstraint(condition=models.Q(state="asked_instead") | models.Q(request__isnull=True),
                                   name="requesting_only_one_asked_instead_names_a_request"),
        ]

    @property
    def state_words(self) -> str:
        """"withdrawn", "asked for as a request": for a sentence."""
        return PreferenceState(self.state).label.lower()
