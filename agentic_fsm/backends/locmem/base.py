"""State kept in this process's memory, for tests. Nothing survives the process."""

from dataclasses import replace
from threading import Lock

from ..base.base import AlreadyStarted, BaseStateBackend, Current, Move, lost_race, utc_now


class StateBackend(BaseStateBackend):
    def __init__(self):
        self._log: list[Move] = []
        self._current: dict[tuple[str, str], Current] = {}  # insertion order is start order
        self._lock = Lock()

    # ── Writes ──

    def start(self, machine, entity, state):
        move = Move(machine, entity, None, None, state, True, "code", utc_now())
        with self._lock:
            if (machine, entity) in self._current:
                raise AlreadyStarted(f"{machine}:{entity} has already started")
            self._current[machine, entity] = Current(entity, state, 1)
            self._log.append(move)
        return move

    def move(self, move, version):
        key = (move.machine, move.entity)
        with self._lock:
            if self._current[key].version == version:
                self._current[key] = Current(move.entity, move.to_state, version + 1)
            else:
                move = replace(move, applied=False, reason=lost_race(move))
            self._log.append(move)
        return move

    def refuse(self, move):
        with self._lock:
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
