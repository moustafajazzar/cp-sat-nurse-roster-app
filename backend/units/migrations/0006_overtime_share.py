"""Every unit, existing ones included, starts sharing its overtime, with 12 hours above a share free and the carry moving a
share by 12 hours at most. The values are written here, not read from `units.rules`, so that a later change of
the defaults does not rewrite this step."""
from django.db import migrations

DEFAULTS = {"share_overtime": True, "overtime_tolerance": 12, "overtime_carry_cap": 12}


def add(apps, schema_editor):
    Unit = apps.get_model("units", "Unit")
    for unit in Unit.objects.all():
        missing = {key: value for key, value in DEFAULTS.items() if key not in unit.rules}
        if missing:
            unit.rules = unit.rules | missing
            unit.save(update_fields=["rules"])


def remove(apps, schema_editor):
    Unit = apps.get_model("units", "Unit")
    for unit in Unit.objects.all():
        if any(key in unit.rules for key in DEFAULTS):
            unit.rules = {key: value for key, value in unit.rules.items() if key not in DEFAULTS}
            unit.save(update_fields=["rules"])


class Migration(migrations.Migration):

    dependencies = [
        ('units', '0005_skill_needs'),
    ]

    operations = [
        migrations.RunPython(add, remove),
    ]
