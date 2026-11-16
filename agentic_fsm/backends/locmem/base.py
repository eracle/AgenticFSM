"""State kept in this process's memory, for tests. Nothing survives the process."""

from collections import defaultdict
from contextlib import contextmanager
from threading import Lock, RLock

from ..base.base import AlreadyStarted, BaseStateBackend, Current, Move, utc_now


class StateBackend(BaseStateBackend):
    def __init__(self):
        self._log: list[Move] = []
        self._current: dict[tuple[str, str], Current] = {}  # insertion order is start order
        self._lock = Lock()
        self._entity_locks: defaultdict[tuple[str, str], RLock] = defaultdict(RLock)

    # ── Writes ──

    def start(self, machine, entity, state):
        move = Move(machine, entity, None, None, state, "code", utc_now())
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
        with entity_lock:
            yield self.current(machine, entity)

    def move(self, move):
        with self._lock:
            self._current[move.machine, move.entity] = Current(move.entity, move.to_state)
            self._log.append(move)
        return move

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
