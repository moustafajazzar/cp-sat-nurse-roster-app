"""Every unit, existing ones included, starts with no skill needs and the needs hard. The values are written
here, not read from `units.rules`, so that a later change of the defaults does not rewrite this step."""
from django.db import migrations

FIELDS = {"skill_needs": [], "skill_needs_hard": True}


def add(apps, schema_editor):
    Unit = apps.get_model("units", "Unit")
    for unit in Unit.objects.all():
        missing = {key: value for key, value in FIELDS.items() if key not in unit.rules}
        if missing:
            unit.rules = unit.rules | missing
            unit.save(update_fields=["rules"])


def remove(apps, schema_editor):
    Unit = apps.get_model("units", "Unit")
    for unit in Unit.objects.all():
        if FIELDS.keys() & unit.rules.keys():
            unit.rules = {key: value for key, value in unit.rules.items() if key not in FIELDS}
            unit.save(update_fields=["rules"])


class Migration(migrations.Migration):

    dependencies = [
        ('units', '0004_max_full_runs'),
    ]

    operations = [
        migrations.RunPython(add, remove),
    ]
