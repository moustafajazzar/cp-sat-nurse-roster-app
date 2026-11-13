from django.db import transaction

from accounts.models import Role, User
from accounts.provisioning import deactivate_many
from notifications.sending import probation_ending
from nurses import membership, probation
from nurses.models import NurseProfile
from ops.queue import hourly


@hourly(at_minute=5)
def close_ended_nurses(timestamp: int) -> None:
    """Staff nurses whose last day is over. Hourly rather than at midnight, so a worker that was down catches up, and
    it costs one query when nobody has ended. A manager's account is the System Admin's: their profile ends alone."""
    deactivate_many(User.objects.filter(role=Role.STAFF_NURSE, is_active=True,
                                        nurse_profile__end_date__lt=membership.today()))


@hourly(at_minute=25)
def tell_probations_ending(timestamp: int) -> None:
    """Her unit manager, once, in the last month of her probation, from its first hour on: so a worker that was down still tells
    him later that month. Once per last month, so an extension tells him again in the new one. Not one ended early: there is
    nothing to extend. The probation itself needs no job to end, since its last day is read, never stored as a state."""
    month = membership.this_month()
    waiting = (NurseProfile.objects.filter(on_probation=True, probation_ended_in__isnull=True, user__is_active=True)
               .exclude(probation_notified_for=month).values_list("pk", flat=True))
    for nurse_id in waiting:
        with transaction.atomic():
            nurse = NurseProfile.objects.select_for_update(of=("self",), no_key=True).select_related("user").get(pk=nurse_id)
            last = probation.last_day(nurse)
            if (nurse.probation_notified_for == month or probation.ended_early(nurse) or not nurse.on_probation
                    or last.replace(day=1) != month or last < nurse.start_date):
                continue
            unit = membership.unit_in(nurse, month)
            if unit.manager is None or not unit.manager.is_active:
                continue  # Told once the unit has a manager again, later this month.
            probation_ending(unit, nurse.pk, month)
            nurse.probation_notified_for = month
            nurse.save(update_fields=["probation_notified_for"])
