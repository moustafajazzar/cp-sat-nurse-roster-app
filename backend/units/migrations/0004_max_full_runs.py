"""Every unit, existing ones included, starts with the most full runs in a month at 3. The value is written here,
not read from `units.rules`, so that a later change of the default does not rewrite this step."""
from django.db import migrations


def add(apps, schema_editor):
    Unit = apps.get_model("units", "Unit")
    for unit in Unit.objects.all():
        if "max_full_runs" not in unit.rules:
            unit.rules = unit.rules | {"max_full_runs": 3}
            unit.save(update_fields=["rules"])


def remove(apps, schema_editor):
    Unit = apps.get_model("units", "Unit")
    for unit in Unit.objects.all():
        if "max_full_runs" in unit.rules:
            unit.rules = {key: value for key, value in unit.rules.items() if key != "max_full_runs"}
            unit.save(update_fields=["rules"])


class Migration(migrations.Migration):

    dependencies = [
        ('units', '0003_unit_preference_cap'),
    ]

    operations = [
        migrations.RunPython(add, remove),
    ]
