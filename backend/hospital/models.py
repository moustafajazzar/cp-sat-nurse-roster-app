import copy
from typing import Literal

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models.functions import UUID7, Lower

from hospital.rules import DEFAULT_RULES


def default_rules() -> dict:
    return copy.deepcopy(DEFAULT_RULES)


class HospitalSettings(models.Model):
    """One row, reached only through `load`. `rules` is written only through the API's schema."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    rules = models.JSONField(default=default_rules)
    rules_need_review = models.BooleanField(default=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="hospital_settings_one_row")]

    @classmethod
    def load(cls, *, for_update: bool = False) -> "HospitalSettings":
        objects = cls.objects.select_for_update() if for_update else cls.objects
        return objects.get_or_create(pk=1)[0]


class Shift(models.Model):
    """A row of the shift catalogue. `code` is the name the solver and every roster know it by. Never
    deleted, only retired. Ordered by age, so the seeded order stays and a rename does not move a row."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    code = models.CharField(max_length=8)
    display_name = models.CharField(max_length=100)
    hours = models.PositiveSmallIntegerField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    colour = models.CharField(max_length=7)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["id"]
        constraints = [models.UniqueConstraint(Lower("code"), name="hospital_shift_code_any_case")]


class ShiftRename(models.Model):
    """One change of a shift's code, so that an old name in a nurse's history can be followed to the
    shift's current one. A bigint key: it is an append-only log."""

    id = models.BigAutoField(primary_key=True)
    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="renames")
    old_code = models.CharField(max_length=8)
    new_code = models.CharField(max_length=8)
    at = models.DateTimeField(auto_now_add=True)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["id"]


class LeaveType(models.Model):
    """The recorded reason for a leave. Never deleted, only retired: leave already recorded keeps it."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = [Lower("name")]
        constraints = [models.UniqueConstraint(Lower("name"), name="hospital_leave_type_name_any_case")]


# `Skill.Kind` as an API schema names it, one list wherever a kind is read.
SkillKind = Literal["skill", "certificate", "licence"]


class Skill(models.Model):
    """A row of the skill catalogue. `name` is what nurses' records, unit rules and solve inputs call it. Never deleted,
    only retired: nurses' records of it and past rosters keep it. Every kind is read by the same rules, but a licence
    always expires."""

    class Kind(models.TextChoices):
        SKILL = "skill"  # The plain kind: an ability, a course card.
        CERTIFICATE = "certificate"  # A degree, a diploma, a course certificate.
        LICENCE = "licence"  # Permits a nurse to practise, from the Saudi Commission for Health Specialties.

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=11, choices=Kind, default=Kind.SKILL)
    issuing_body = models.CharField(max_length=100, blank=True, default="")  # Free text; may be empty.
    expires = models.BooleanField()  # Every nurse's record of it carries an expiry.
    required_to_work = models.BooleanField()  # From the day after its expiry, a nurse holding it works no shift.
    # A nurse holding it, unexpired on the day of assigning, may be assigned as a preceptor. Checked then only.
    lets_a_nurse_precept = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = [Lower("name")]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="hospital_skill_name_any_case"),
            models.CheckConstraint(condition=~models.Q(kind="licence") | models.Q(expires=True),
                                   name="hospital_skill_a_licence_expires"),
        ]


class SkillRename(models.Model):
    """One change of a skill's name, so that an old name in a stored solve input can be followed to the skill's current
    one. A bigint key: it is an append-only log."""

    id = models.BigAutoField(primary_key=True)
    skill = models.ForeignKey(Skill, on_delete=models.PROTECT, related_name="renames")
    old_name = models.CharField(max_length=100)
    new_name = models.CharField(max_length=100)
    at = models.DateTimeField(auto_now_add=True)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["id"]


class PublicHoliday(models.Model):
    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    date = models.DateField(unique=True)
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["date"]


class FixedRamadanMonth(models.Model):
    """The CNO's dates for a month, after the moon sighting. A month with no row is derived from the
    Umm al-Qura calendar: see `hospital.catalogue.ramadan_dates`. Empty `dates` means none this month."""

    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    month = models.CharField(max_length=7, unique=True)  # YYYY-MM, as the solver names a month.
    dates = ArrayField(models.DateField())
