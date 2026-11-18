"""The log loses `step`: a state has one step, so a row's `from_state` says which one ran."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("agentic_fsm", "0002_only_real_moves"),
    ]

    operations = [
        migrations.RemoveField(model_name="logentry", name="step"),
    ]
