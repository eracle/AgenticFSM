"""State kept in this process's memory, for tests. Nothing survives the process."""

from collections import defaultdict
from contextlib import contextmanager
from threading import Lock, RLock, local

from ..base.base import AlreadyStarted, BaseStateBackend, Current, Move, utc_now


class StateBackend(BaseStateBackend):
    def __init__(self):
        self._log: list[Move] = []
        self._current: dict[tuple[str, str], Current] = {}  # insertion order is start order
        self._lock = Lock()
        self._entity_locks: defaultdict[tuple[str, str], RLock] = defaultdict(RLock)
        self._held = local()  # .callbacks: what waits for this thread's outermost lock to end

    # ── Writes ──

    def start(self, machine, entity, state):
        move = Move(machine, entity, None, state, "code", utc_now())
        with self._lock:
            if (machine, entity) in self._current:
                raise AlreadyStarted(f"{machine}:{entity} has already started")
            self._current[machine, entity] = Current(entity, state)
            self._log.append(move)
        return move

    @contextmanager
    def lock(self, machine, entity):
        with self._lock:
            entity_lock = self._entity_locks[machine, entity]
        outermost = not hasattr(self._held, "callbacks")
        if outermost:
            self._held.callbacks = []
        try:
            with entity_lock:
                yield self.current(machine, entity)
        finally:
            if outermost:
                # Nothing is undone in memory, so they run even when the block raised.
                for callback in self._held.__dict__.pop("callbacks"):
                    callback()

    def move(self, move):
        with self._lock:
            self._current[move.machine, move.entity] = Current(move.entity, move.to_state)
            self._log.append(move)
        return move

    def on_commit(self, callback):
        callbacks = getattr(self._held, "callbacks", None)
        if callbacks is None:
            callback()
        else:
            callbacks.append(callback)

    # ── Reads ──

    def current(self, machine, entity):
        return self._current.get((machine, entity))

    def in_state(self, machine, state):
        return [c.entity for c in self.all_current(machine) if c.state == state]

    def all_current(self, machine):
        with self._lock:
            return [c for (name, _), c in self._current.items() if name == machine]

    def history(self, machine, entity):
        with self._lock:
            return [m for m in self._log if (m.machine, m.entity) == (machine, entity)]
