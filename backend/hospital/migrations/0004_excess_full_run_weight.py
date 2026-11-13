"""The hospital's weights gain the price of a full run above a unit's most, at 4. The value is written here, not
read from `hospital.rules`, so that a later change of the default does not rewrite this step."""
from django.db import migrations


def add(apps, schema_editor):
    HospitalSettings = apps.get_model("hospital", "HospitalSettings")
    for row in HospitalSettings.objects.all():
        if "excess_full_run" not in row.rules["weights"]:
            row.rules["weights"]["excess_full_run"] = 4
            row.save(update_fields=["rules"])


def remove(apps, schema_editor):
    HospitalSettings = apps.get_model("hospital", "HospitalSettings")
    for row in HospitalSettings.objects.all():
        if row.rules["weights"].pop("excess_full_run", None) is not None:
            row.save(update_fields=["rules"])


class Migration(migrations.Migration):

    dependencies = [
        ('hospital', '0003_skills'),
    ]

    operations = [
        migrations.RunPython(add, remove),
    ]
