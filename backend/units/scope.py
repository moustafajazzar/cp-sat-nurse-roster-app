"""Which units a caller may reach. `SignedIn` names the roles an endpoint is for; every endpoint that acts on
a unit then goes through here, so a manager reaches his own unit and nothing of another:

    @router.get("/{unit_id}/rules", auth=SignedIn(Role.UNIT_MANAGER, Role.CNO, Role.DEPUTY_CNO))
    def rules(request, unit_id: UUID):
        unit = unit_in_scope(request.user, unit_id)

The walking test in accounts/tests/test_authorization.py cannot see this, so such an endpoint needs its own
test that another unit's manager is refused.
"""
from __future__ import annotations

from uuid import UUID

from django.db.models import QuerySet
from django.http import Http404
from ninja.errors import HttpError

from accounts.models import Role, User
from nurses.membership import unit_of_nurse
from units.models import Unit

HOSPITAL_WIDE = (Role.CNO, Role.DEPUTY_CNO)


def units_in_scope(user: User) -> QuerySet[Unit]:
    """Every unit for the CNO and the Deputy CNO, the one they hold for a manager, whose nurse profile if any is in
    it (`nurses.membership`), the one they belong to this month for a staff nurse, and none for anyone else."""
    if user.role in HOSPITAL_WIDE:
        return Unit.objects.all()
    if user.role == Role.UNIT_MANAGER:
        return Unit.objects.filter(manager=user)
    if user.role == Role.STAFF_NURSE:
        return unit_of_nurse(user)
    return Unit.objects.none()


def unit_in_scope(user: User, unit_id: UUID, *, for_update: bool = False) -> Unit:
    """The unit, or a refusal: 404 for a hospital-wide role, which may know what exists, and for anyone else
    the same 403 whether the unit exists or not, since a manager sees nothing of other units."""
    units = units_in_scope(user)
    unit = (units.select_for_update() if for_update else units).filter(pk=unit_id).first()
    if unit is not None:
        return unit
    if user.role in HOSPITAL_WIDE:
        raise Http404("There is no such unit.")
    raise HttpError(403, "This is not your unit.")
