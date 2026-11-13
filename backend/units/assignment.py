"""Taking a unit from a manager who is leaving, in the transaction that deactivates them. Lock order: the
user's row before the unit's, here and in `units.api`."""
from __future__ import annotations

from django.http import HttpRequest

from accounts.models import User
from audit.log import record
from units.models import Unit


def release_managed_unit(request: HttpRequest, manager: User) -> None:
    """The System Admin must be able to end an account at once, and assignments are the CNO's, so this neither
    waits for the CNO nor picks a successor: the unit stays as it is, with no manager until the CNO assigns one."""
    unit = Unit.objects.select_for_update().filter(manager=manager).first()
    if unit is not None:
        unit.manager = None
        unit.save(update_fields=["manager"])
        record("unit.changed", actor=request.user, target=unit.name, request=request,
               before={"manager": manager.username}, after={"manager": None})
