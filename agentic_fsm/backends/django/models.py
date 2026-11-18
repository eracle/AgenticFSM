"""The two tables behind the Django state backend."""

from django.db import models


class LogEntry(models.Model):
    """One move that happened. Rows are appended and never changed."""

    machine = models.CharField(max_length=100)
    entity = models.CharField(max_length=255)
    from_state = models.CharField(max_length=255, null=True)
    to_state = models.CharField(max_length=255)
    decided_by = models.CharField(max_length=255)
    decision = models.JSONField(null=True)
    at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["machine", "entity", "id"], name="agentic_fsm_log_entity")]

    def __str__(self):
        return f"{self.machine}:{self.entity} {self.from_state} -> {self.to_state}"


class CurrentState(models.Model):
    """Where one entity is now. A fire locks this row from read to write."""

    machine = models.CharField(max_length=100)
    entity = models.CharField(max_length=255)
    state = models.CharField(max_length=255)
    updated_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["machine", "entity"], name="agentic_fsm_one_state_per_entity")
        ]
        indexes = [models.Index(fields=["machine", "state"], name="agentic_fsm_current_state")]

    def __str__(self):
        return f"{self.machine}:{self.entity} in {self.state}"
