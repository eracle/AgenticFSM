from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="CurrentState",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("machine", models.CharField(max_length=100)),
                ("entity", models.CharField(max_length=255)),
                ("state", models.CharField(max_length=255)),
                ("version", models.PositiveIntegerField()),
                ("updated_at", models.DateTimeField()),
            ],
            options={
                "indexes": [
                    models.Index(fields=["machine", "state"], name="agentic_fsm_current_state")
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("machine", "entity"), name="agentic_fsm_one_state_per_entity"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="LogEntry",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("machine", models.CharField(max_length=100)),
                ("entity", models.CharField(max_length=255)),
                ("step", models.CharField(max_length=255, null=True)),
                ("from_state", models.CharField(max_length=255, null=True)),
                ("to_state", models.CharField(max_length=255)),
                ("applied", models.BooleanField()),
                ("decided_by", models.CharField(max_length=255)),
                ("decision", models.JSONField(null=True)),
                ("reason", models.TextField(null=True)),
                ("at", models.DateTimeField()),
            ],
            options={
                "indexes": [
                    models.Index(fields=["machine", "entity", "id"], name="agentic_fsm_log_entity")
                ],
            },
        ),
    ]
