"""A catalogue entry gains its kind and the body that issues it: every entry there is becomes a skill with no
issuing body, and a licence always expires. Each step reverses by itself, as the precept mark's migration test needs."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hospital', '0006_skill_lets_a_nurse_precept'),
    ]

    operations = [
        migrations.AddField(
            model_name='skill',
            name='issuing_body',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='skill',
            name='kind',
            field=models.CharField(choices=[('skill', 'Skill'), ('certificate', 'Certificate'), ('licence', 'Licence')], default='skill', max_length=11),
        ),
        migrations.AddConstraint(
            model_name='skill',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('kind', 'licence'), _negated=True), ('expires', True), _connector='OR'), name='hospital_skill_a_licence_expires'),
        ),
    ]
