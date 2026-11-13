from django.conf import settings
from django.db import models


class AuditEntry(models.Model):
    """One row per audited event, never changed or deleted. A bigint key: it is an append-only
    log. The actor is empty for what the system itself did and for a failed login with
    an unknown username. A failed login's `target` is the username typed only when an account has it;
    otherwise it is empty too, so nothing typed at an unknown name is kept."""

    id = models.BigAutoField(primary_key=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=200, blank=True)
    before = models.JSONField(null=True)
    after = models.JSONField(null=True)
    ip = models.GenericIPAddressField(null=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["action", "-id"], name="audit_action_newest_first")]
