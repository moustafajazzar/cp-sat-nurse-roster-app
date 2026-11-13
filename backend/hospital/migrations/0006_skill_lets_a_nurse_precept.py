"""A skill gains its third mark, "Lets a nurse precept": off for every skill there is."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hospital', '0005_skill_shortfall_weight'),
    ]

    operations = [
        migrations.AddField(
            model_name='skill',
            name='lets_a_nurse_precept',
            field=models.BooleanField(default=False),
        ),
    ]
