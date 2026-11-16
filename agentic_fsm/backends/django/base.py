"""State kept in a Django project's database, through this package's two models.

Add ``"agentic_fsm.backends.django"`` to ``INSTALLED_APPS`` and migrate. Writes go through
Django's own connection, so a caller that wraps ``fire`` in ``transaction.atomic()`` commits its
own rows and the move together.
"""

from dataclasses import replace

from django.db import IntegrityError, transaction
from django.db.models import F

from ..base.base import AlreadyStarted, BaseStateBackend, Current, Move, lost_race, utc_now
from .models import CurrentState, LogEntry

LOG_FIELDS = (
    "machine",
    "entity",
    "step",
    "from_state",
    "to_state",
    "applied",
    "decided_by",
    "at",
    "decision",
    "reason",
)


class StateBackend(BaseStateBackend):
    # ── Writes ──

    def start(self, machine, entity, state):
        move = Move(machine, entity, None, None, state, True, "code", utc_now())
        try:
            with transaction.atomic():
                CurrentState.objects.create(
                    machine=machine, entity=entity, state=state, version=1, updated_at=move.at
                )
                _append(move)
        except IntegrityError:
            raise AlreadyStarted(f"{machine}:{entity} has already started") from None
        return move

    def move(self, move, version):
        with transaction.atomic():
            still_there = CurrentState.objects.filter(
                machine=move.machine, entity=move.entity, version=version
            ).update(state=move.to_state, version=F("version") + 1, updated_at=move.at)
            if not still_there:
                move = replace(move, applied=False, reason=lost_race(move))
            _append(move)
        return move

    def refuse(self, move):
        _append(move)
        return move

    # ── Reads ──

    def current(self, machine, entity):
        row = (
            CurrentState.objects.filter(machine=machine, entity=entity)
            .values_list("entity", "state", "version")
            .first()
        )
        return Current(*row) if row else None

    def in_state(self, machine, state):
        rows = CurrentState.objects.filter(machine=machine, state=state).order_by("id")
        return list(rows.values_list("entity", flat=True))

    def all_current(self, machine):
        rows = CurrentState.objects.filter(machine=machine).order_by("id")
        return [Current(*row) for row in rows.values_list("entity", "state", "version")]

    def history(self, machine, entity):
        rows = LogEntry.objects.filter(machine=machine, entity=entity).order_by("id")
        return [Move(**row) for row in rows.values(*LOG_FIELDS)]


def _append(move: Move):
    LogEntry.objects.create(**{field: getattr(move, field) for field in LOG_FIELDS})
