"""What every state backend stores and the calls a machine makes on it.

A backend keeps two things: an append-only log of the moves made, and one current-state row per
entity. A machine reads an entity's row under ``lock``, runs a step, and writes the move inside
that same lock, so a second caller waits and then reads the state the first left. A backend
stores state, never the entity.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Move:
    """One log row: a move that happened."""

    machine: str
    entity: str
    step: str | None
    from_state: str | None
    to_state: str
    decided_by: str
    at: datetime
    decision: dict | None = None


@dataclass(frozen=True)
class Current:
    """An entity's current state."""

    entity: str
    state: str


class AlreadyStarted(Exception):
    """``start`` was called on an entity that already has a state."""


def utc_now() -> datetime:
    """The time a move is logged at."""
    return datetime.now(UTC)


class BaseStateBackend:
    """The interface a machine calls. Subclasses implement every method."""

    # ── Writes ──

    def start(self, machine: str, entity: str, state: str) -> Move:
        """Give an entity its first state and log it; raise ``AlreadyStarted`` if it has one."""
        raise NotImplementedError("subclasses of BaseStateBackend must override start()")

    def lock(self, machine: str, entity: str) -> AbstractContextManager[Current | None]:
        """Hold the entity from read to write: yields its current state, or ``None`` if it
        hasn't started. A ``move`` made inside commits when the block ends; an exception
        undoes it."""
        raise NotImplementedError("subclasses of BaseStateBackend must override lock()")

    def move(self, move: Move) -> Move:
        """Rewrite the entity's current state and log ``move``; called inside ``lock``."""
        raise NotImplementedError("subclasses of BaseStateBackend must override move()")

    def on_commit(self, callback: Callable[[], None]) -> None:
        """Call ``callback`` once what was written here is committed: right away outside a
        ``lock``, and inside one when the outermost lock's writes commit. Never, if they are
        undone."""
        raise NotImplementedError("subclasses of BaseStateBackend must override on_commit()")

    # ── Reads ──

    def current(self, machine: str, entity: str) -> Current | None:
        """The entity's current state, or ``None`` if it hasn't started."""
        raise NotImplementedError("subclasses of BaseStateBackend must override current()")

    def in_state(self, machine: str, state: str) -> list[str]:
        """The ids of the machine's entities in ``state``, oldest first."""
        raise NotImplementedError("subclasses of BaseStateBackend must override in_state()")

    def all_current(self, machine: str) -> list[Current]:
        """Every entity of the machine with its current state, oldest first."""
        raise NotImplementedError("subclasses of BaseStateBackend must override all_current()")

    def history(self, machine: str, entity: str) -> list[Move]:
        """Every move logged for the entity, oldest first."""
        raise NotImplementedError("subclasses of BaseStateBackend must override history()")
