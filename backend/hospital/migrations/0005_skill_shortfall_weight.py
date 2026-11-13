"""The hospital's weights gain the price of a nurse short of a soft skill need, at 0: no price until the CNO
sets one. The value is written here, not read from `hospital.rules`, so that a later change of the default does not
rewrite this step."""
from django.db import migrations


def add(apps, schema_editor):
    HospitalSettings = apps.get_model("hospital", "HospitalSettings")
    for row in HospitalSettings.objects.all():
        if "skill_shortfall" not in row.rules["weights"]:
            row.rules["weights"]["skill_shortfall"] = 0
            row.save(update_fields=["rules"])


def remove(apps, schema_editor):
    HospitalSettings = apps.get_model("hospital", "HospitalSettings")
    for row in HospitalSettings.objects.all():
        if "skill_shortfall" in row.rules["weights"]:
            del row.rules["weights"]["skill_shortfall"]
            row.save(update_fields=["rules"])


class Migration(migrations.Migration):

    dependencies = [
        ('hospital', '0004_excess_full_run_weight'),
    ]

    operations = [
        migrations.RunPython(add, remove),
    ]
