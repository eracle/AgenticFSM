"""Only moves that happened stay in the log, and the current-state row loses its version.

The refused rows are deleted, then `applied`, `reason` and `version` go: a fire now locks the
entity's row instead of comparing versions. The three columns are given defaults first, so the
migration reverses: every row comes back applied, with no reason, at version 1.
"""

from django.db import migrations, models


def delete_refused_moves(apps, schema_editor):
    LogEntry = apps.get_model("agentic_fsm", "LogEntry")
    LogEntry.objects.filter(applied=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("agentic_fsm", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(model_name="logentry", name="applied", field=models.BooleanField(default=True)),
        migrations.AlterField(model_name="logentry", name="reason", field=models.TextField(null=True, default=None)),
        migrations.AlterField(
            model_name="currentstate", name="version", field=models.PositiveIntegerField(default=1)
        ),
        migrations.RunPython(delete_refused_moves, migrations.RunPython.noop),
        migrations.RemoveField(model_name="logentry", name="applied"),
        migrations.RemoveField(model_name="logentry", name="reason"),
        migrations.RemoveField(model_name="currentstate", name="version"),
    ]
