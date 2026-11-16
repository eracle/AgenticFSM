"""What every state backend stores and the calls a machine makes on it.

A backend keeps two things: an append-only log of moves, and one current-state row per entity.
A move that's applied rewrites that row in the same transaction as its log row, and only if the
entity's version is still the one the caller read (compare-and-set). A backend stores state,
never the entity.
"""

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Move:
    """One log row: a move applied, or one refused."""

    machine: str
    entity: str
    step: str | None
    from_state: str | None
    to_state: str
    applied: bool
    decided_by: str
    at: datetime
    decision: dict | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Current:
    """An entity's current state, with the version a move must still find to be applied."""

    entity: str
    state: str
    version: int


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

    def move(self, move: Move, version: int) -> Move:
        """Apply ``move`` if the entity is still at ``version``, and log it either way.

        Returns the move as logged: applied, or refused because something else moved it first.
        """
        raise NotImplementedError("subclasses of BaseStateBackend must override move()")

    def refuse(self, move: Move) -> Move:
        """Log a refused move; the current state is untouched."""
        raise NotImplementedError("subclasses of BaseStateBackend must override refuse()")

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
        """Every move logged for the entity, applied and refused, oldest first."""
        raise NotImplementedError("subclasses of BaseStateBackend must override history()")


def lost_race(move: Move) -> str:
    """The reason logged when a move finds the entity no longer at the version it read."""
    return f"moved by someone else since it was read in {move.from_state}"
