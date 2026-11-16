"""State kept in a Django project's database, through this package's two models.

Add ``"agentic_fsm.backends.django"`` to ``INSTALLED_APPS`` and migrate. ``lock`` opens a
transaction on Django's own connection and takes the entity's current-state row with
``select_for_update``, so the step's own writes commit with the move. SQLite has no row lock and
ignores it; there the transaction holds the database once the step writes.
"""

from contextlib import contextmanager

from django.db import IntegrityError, transaction

from ..base.base import AlreadyStarted, BaseStateBackend, Current, Move, utc_now
from .models import CurrentState, LogEntry

LOG_FIELDS = ("machine", "entity", "step", "from_state", "to_state", "decided_by", "at", "decision")


class StateBackend(BaseStateBackend):
    # ── Writes ──

    def start(self, machine, entity, state):
        move = Move(machine, entity, None, None, state, "code", utc_now())
        try:
            with transaction.atomic():
                CurrentState.objects.create(machine=machine, entity=entity, state=state, updated_at=move.at)
                _append(move)
        except IntegrityError:
            raise AlreadyStarted(f"{machine}:{entity} has already started") from None
        return move

    @contextmanager
    def lock(self, machine, entity):
        with transaction.atomic():
            row = (
                CurrentState.objects.select_for_update()
                .filter(machine=machine, entity=entity)
                .values_list("entity", "state")
                .first()
            )
            yield Current(*row) if row else None

    def move(self, move):
        CurrentState.objects.filter(machine=move.machine, entity=move.entity).update(
            state=move.to_state, updated_at=move.at
        )
        _append(move)
        return move

    # ── Reads ──

    def current(self, machine, entity):
        row = CurrentState.objects.filter(machine=machine, entity=entity).values_list("entity", "state").first()
        return Current(*row) if row else None

    def in_state(self, machine, state):
        rows = CurrentState.objects.filter(machine=machine, state=state).order_by("id")
        return list(rows.values_list("entity", flat=True))

    def all_current(self, machine):
        rows = CurrentState.objects.filter(machine=machine).order_by("id")
        return [Current(*row) for row in rows.values_list("entity", "state")]

    def history(self, machine, entity):
        rows = LogEntry.objects.filter(machine=machine, entity=entity).order_by("id")
        return [Move(**row) for row in rows.values(*LOG_FIELDS)]


def _append(move: Move):
    LogEntry.objects.create(**{field: getattr(move, field) for field in LOG_FIELDS})
