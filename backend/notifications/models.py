from django.conf import settings
from django.db import models
from django.db.models.functions import UUID7


class Event(models.TextChoices):
    """What a notification is about. A new kind is added here, with its words in `notifications.reading`."""
    ROSTER_SUBMITTED = "roster_submitted"
    ROSTER_APPROVED = "roster_approved"
    ROSTER_REJECTED = "roster_rejected"
    ROSTER_CHANGED = "roster_changed"
    SOLVE_ENDED = "solve_ended"
    WINDOW_OPENED = "window_opened"
    WINDOW_CLOSED_EARLY = "window_closed_early"
    WINDOW_CLOSED = "window_closed"
    REQUEST_DECIDED = "request_decided"
    SWAP_STEP = "swap_step"
    MONTH_CLOSED = "month_closed"
    COVER_STEP = "cover_step"
    PROBATION_ENDING = "probation_ending"
    PRECEPTOR_ASSIGNED = "preceptor_assigned"
    PRECEPTOR_LEAVING = "preceptor_leaving"
    MONTH_DATES_CHANGED = "month_dates_changed"


class SolveOutcome(models.TextChoices):
    """How a solve ended, as the manager is told: a solve's payload keeps it, since the solve's row cannot change after."""
    DRAFT = "draft"
    STOPPED_DRAFT = "stopped_draft"
    NO_ROSTER = "no_roster"
    FROZEN = "frozen"
    FAILED = "failed"
    # A re-plan's own ends. Its failure and a search that kept no roster are FAILED and NO_ROSTER, with `replan` in the payload.
    REPLANNED = "replanned"
    STOPPED_REPLANNED = "stopped_replanned"
    REPLAN_UNCHANGED = "replan_unchanged"
    REPLAN_OUTDATED = "replan_outdated"
    REPLAN_MONTH_OVER = "replan_month_over"


class Notification(models.Model):
    """One thing one user is told. `payload` holds identifiers only (a unit, a month, a solve, who did it): the words are
    worked out when the notification is read, from what those name then, so a rename or a correction leaves nothing stale and
    nothing about a person is kept here. Never a rejection's comment or a leave. A nurse only as her profile's identifier, for a
    probation ending, whose words are her unit manager's to read, and for a preceptor assigned, changed or leaving,
    whose words are read by the two nurses and her unit manager."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="notifications")
    event = models.CharField(max_length=32, choices=Event)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "-created_at"], name="notifications_newest_a_user")]


class PushKeyPair(models.Model):
    """The installation's own VAPID key pair, one row: it names this installation to the browsers' push services, and
    every push subscription is bound to its public key. Kept in the database, so a backup brings it back with the subscriptions
    it serves. Made at first start (`notifications.push_keys`); replaced only by `rotate_push_keys`."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    private_key = models.TextField(help_text="PEM, PKCS #8, of a P-256 key.")
    public_key = models.CharField(max_length=100, help_text="The uncompressed point, base64url without padding.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="push_key_pair_one_row")]


class PushSubscription(models.Model):
    """A device's standing permission to be sent pushes for one user. Nothing of the device beyond what the push
    service gave it: no user agent, no name, no IP. Deleted when it ends, since it is no one's record."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.URLField(max_length=2000, unique=True, help_text="The push service's address for this device.")
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    turned_on_at = models.DateTimeField(auto_now_add=True)
    failures_in_a_row = models.PositiveIntegerField(default=0)
    last_success_at = models.DateTimeField(null=True, blank=True)


class PushOutcome(models.TextChoices):
    """What a push service answered one send, in the app's words (`notifications.pushing`)."""
    SENT = "sent"
    GONE = "gone", "The push service says the subscription is gone (404 or 410)."
    REFUSED = "refused", "The push service refused the push (another 4xx, or a redirect)."
    UNREACHABLE = "unreachable", "The push service could not be reached, timed out, or answered 429 or 5xx."


class PushAttempt(models.Model):
    """One send to one subscription, for the health page's counts: when, and what came of it. Nothing of the user or
    the device. Append-only, so a bigint key."""

    id = models.BigAutoField(primary_key=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    outcome = models.CharField(max_length=11, choices=PushOutcome)
