"""The hospital's weights gain the price of an hour of overtime above a nurse's overtime share, at 1, or at the
price of an hour of overtime where that is below 1: the weights page refuses a share weight above it, and an installation
pricing no overtime must still be able to save its rules. The value is written here, not read from `hospital.rules`, so
that a later change of the default does not rewrite this step."""
from django.db import migrations


def add(apps, schema_editor):
    HospitalSettings = apps.get_model("hospital", "HospitalSettings")
    for row in HospitalSettings.objects.all():
        weights = row.rules["weights"]
        if "overtime_above_share" not in weights:
            weights["overtime_above_share"] = min(1, weights.get("nurse_overtime", 1))
            row.save(update_fields=["rules"])


def remove(apps, schema_editor):
    HospitalSettings = apps.get_model("hospital", "HospitalSettings")
    for row in HospitalSettings.objects.all():
        if row.rules["weights"].pop("overtime_above_share", None) is not None:
            row.save(update_fields=["rules"])


class Migration(migrations.Migration):

    dependencies = [
        ('hospital', '0007_skill_kind_and_issuing_body'),
    ]

    operations = [
        migrations.RunPython(add, remove),
    ]
