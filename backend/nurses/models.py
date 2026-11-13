import datetime as dt

from django.conf import settings
from django.db import models
from django.db.models.functions import UUID7


class Gender(models.TextChoices):
    # The solver's own two values: see `gender` in solver/nurse_roster/problem.py.
    FEMALE = "F", "Female"
    MALE = "M", "Male"


class NurseProfile(models.Model):
    """A user's roster identity. The hospital ID is the user's username, so there is one place for it and
    usernames already keep it unique in any case. The unit is not here: see `Placement`. Nor are the allowed shifts,
    which only mean something in a unit. The rest of the rostering attributes and the opening values keep the solver's
    field names and its defaults, and no value of theirs may reach `audit.log.record`: the System Admin reads the log. Her
    probation is the exception: its entries carry their values."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="nurse_profile")
    gender = models.CharField(max_length=1, choices=Gender)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    team_leader = models.BooleanField(default=False)
    score = models.FloatField(default=0.8)
    ramadan_reduced = models.BooleanField(default=False)
    breastfeeding = models.BooleanField(default=False)
    ytd_overtime = models.IntegerField(default=0, help_text="Overtime hours this year, as of the day they were added.")
    hours_balance = models.IntegerField(default=0, help_text="Hours still owed (+) or worked ahead (-).")
    night_balance = models.FloatField(default=0.0, help_text="Night shifts above (+) or below (-) their fair share.")
    friday_balance = models.FloatField(default=0.0, help_text="Weekend days above (+) or below (-) their fair share.")
    overtime_balance = models.FloatField(
        default=0.0, help_text="Hours of overtime above (+) or below (-) their overtime share so far this year.")
    attributes_saved = models.BooleanField(
        default=False, help_text="A manager has saved these attributes at least once, so a score at the default was looked at.")
    # Her probation, read through `nurses.probation`, which derives its last day from these and the start date.
    # Unlike the attributes above, its values do reach the audit log.
    on_probation = models.BooleanField(default=False, help_text="The CNO's tick. Only a staff nurse's.")
    probation_months = models.PositiveSmallIntegerField(
        default=3, help_text="Whole calendar months from her start date, a month she joins after the 1st not counted. Her unit "
                             "manager's: 1 to 3, then extended a month at a time to 6.")
    probation_ended_in = models.DateField(
        null=True, blank=True, help_text="The first day of the month whose last day became her probation's: her unit manager's "
                                         "End, or the CNO's untick, which keeps the months whose rosters were approved.")
    probation_notified_for = models.DateField(
        null=True, blank=True, help_text="The last month of her probation her unit manager was told of, so that it is told once.")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(probation_months__gte=1, probation_months__lte=6),
                                   name="nurses_probation_one_to_six_months"),
            models.CheckConstraint(condition=models.Q(probation_ended_in__day=1), name="nurses_probation_ends_in_a_month"),
        ]


class Placement(models.Model):
    """From this month on, until a later placement of the same nurse, the nurse belongs to this unit. A transfer
    takes effect from a chosen month, so a coming one has to wait here: see `nurses.membership`."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    nurse = models.ForeignKey(NurseProfile, on_delete=models.PROTECT, related_name="placements")
    unit = models.ForeignKey("units.Unit", on_delete=models.PROTECT, related_name="placements")
    from_month = models.DateField(help_text="The first day of the month.")
    # On the placement because a list only means something in a unit, and two units can each roster their own months
    # of a nurse at once: see `membership.transfer`. Empty means any shift this unit works; never an empty list.
    allowed_shifts = models.JSONField(null=True, blank=True)
    allowed_shifts_to_check = models.BooleanField(
        default=False, help_text="A transfer found none of the list's shifts in this unit, so the list was emptied.")

    class Meta:
        ordering = ["from_month"]
        constraints = [
            models.UniqueConstraint(fields=["nurse", "from_month"], name="nurses_one_placement_a_month"),
            models.CheckConstraint(condition=models.Q(from_month__day=1), name="nurses_placement_starts_a_month"),
        ]


class LeaveQuerySet(models.QuerySet):
    def current(self) -> "LeaveQuerySet":
        return self.filter(removed_at__isnull=True)

    def in_month(self, month: dt.date) -> "LeaveQuerySet":
        return self.filter(date__year=month.year, date__month=month.month)


class Leave(models.Model):
    """One date a nurse is off for a recorded reason. A correction or a removal ends the row rather than rewriting or
    deleting it, because the audit log may hold neither the date nor the type (the System Admin reads it), so this is
    the only record of what was there. Only `current()` rows mean anything to anyone. The unit is not here: the manager
    of a date is whoever runs its month, which a placement says (`nurses.membership.placement_in`)."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    nurse = models.ForeignKey(NurseProfile, on_delete=models.PROTECT, related_name="leave")
    date = models.DateField()
    leave_type = models.ForeignKey("hospital.LeaveType", on_delete=models.PROTECT, related_name="+")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(auto_now_add=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    removed_at = models.DateTimeField(null=True, blank=True)

    objects = LeaveQuerySet.as_manager()

    class Meta:
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(fields=["nurse", "date"], condition=models.Q(removed_at__isnull=True),
                                    name="nurses_one_leave_a_day"),
        ]


class NurseSkillQuerySet(models.QuerySet):
    def current(self) -> "NurseSkillQuerySet":
        return self.filter(removed_at__isnull=True)


class NurseSkill(models.Model):
    """A skill a nurse holds, with its expiry, the last day it counts, where the catalogue says the kind expires. The CNO's
    and the Deputy CNO's to record, renew and take off. A renewal or a removal ends the row rather than rewriting or deleting
    it, as leave does, because the audit log may hold neither the skill nor the date (the System Admin reads it), so this is
    the only record of what was there. Only `current()` rows mean anything to anyone. On the profile, not a placement, so a transfer carries it.
    The expiry stays as recorded when the CNO turns the kind's `expires` off, and counts for nothing until it is on again."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    nurse = models.ForeignKey(NurseProfile, on_delete=models.PROTECT, related_name="skills")
    skill = models.ForeignKey("hospital.Skill", on_delete=models.PROTECT, related_name="+")
    expiry = models.DateField(null=True, blank=True)
    # The number or code the issuing body gave her certificate or licence: required on a licence, and unique
    # among one entry's current records. Read by the CNO, the Deputy CNO and the nurse herself, never in a file, a
    # notification or an audit entry. The issue date is read by nothing: the expiry is what a roster reads.
    identifier = models.CharField(max_length=100, null=True, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(auto_now_add=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    removed_at = models.DateTimeField(null=True, blank=True)

    objects = NurseSkillQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["nurse", "skill"], condition=models.Q(removed_at__isnull=True),
                                    name="nurses_one_current_skill_a_nurse"),
            # Beside the API's check, so two records made at once cannot both pass. Ended rows keep theirs.
            models.UniqueConstraint(fields=["skill", "identifier"],
                                    condition=models.Q(removed_at__isnull=True, identifier__isnull=False),
                                    name="nurses_one_current_identifier_a_skill"),
        ]


class PreceptorAssignment(models.Model):
    """From this month on, which nurse precepts a nurse on probation. The latest that has
    begun is in force, as a placement is, and only in the months she is on probation, so a probation's end, extension or untick
    moves it with no write here. `until` is the last month of one ended by a nurse leaving the unit: the preceptor's transfer
    or end date, or her own transfer. It is never brought back, so a transfer cancelled after the manager was told to choose
    another leaves it ended. Read through `nurses.preceptors`."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    nurse = models.ForeignKey(NurseProfile, on_delete=models.PROTECT, related_name="preceptor_assignments")
    preceptor = models.ForeignKey(NurseProfile, on_delete=models.PROTECT, related_name="precepting")
    unit = models.ForeignKey("units.Unit", on_delete=models.PROTECT, related_name="+")
    from_month = models.DateField(help_text="The first day of the month.")
    until = models.DateField(null=True, blank=True, help_text="The first day of its last month, when a nurse's leaving the unit "
                                                              "ended it.")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["from_month"]
        constraints = [
            models.UniqueConstraint(fields=["nurse", "from_month"], name="nurses_one_preceptor_from_a_month"),
            models.CheckConstraint(condition=models.Q(from_month__day=1), name="nurses_preceptor_from_a_month"),
            models.CheckConstraint(condition=models.Q(until__day=1), name="nurses_preceptor_until_a_month"),
            models.CheckConstraint(condition=~models.Q(nurse=models.F("preceptor")), name="nurses_preceptor_is_another_nurse"),
        ]
