from datetime import time

from django.db import models


# Callables, not fixed times: Django's check `fields.W161` warns of a fixed time default when the clock is near it.
def default_backup_time() -> time:
    return time(2, 0)


def default_close_time() -> time:
    return time(0, 30)


class TechnicalSettings(models.Model):
    """The System Admin's limits and schedule. One row, reached only through `load`."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    time_limit_cap_seconds = models.PositiveIntegerField(default=300)
    backup_time = models.TimeField(default=default_backup_time)
    backup_retention_days = models.PositiveIntegerField(default=30)
    close_time = models.TimeField(default=default_close_time)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="ops_technical_settings_one_row")]

    @classmethod
    def load(cls, *, for_update: bool = False) -> "TechnicalSettings":
        objects = cls.objects.select_for_update() if for_update else cls.objects
        return objects.get_or_create(pk=1)[0]
