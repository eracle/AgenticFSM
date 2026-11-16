"""A machine: a ``States`` class, one list of ``(from, to, step)`` rows, and a state backend.

    orders = Machine(Order, TRANSITIONS, load=get_order)

A step is a function of the entity that returns the next state, or ``None`` for "not now".
Its allowed targets are the rows it sits on; anything else it returns is refused and logged,
never applied.
"""

import os
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from functools import cached_property

from .backends import BACKEND_ENV, get_backend
from .backends.base.base import BaseStateBackend, Current, Move, utc_now
from .states import States


class MachineError(ValueError):
    """The machine's declaration is wrong; raised by ``Machine(...)``."""


class TransitionError(Exception):
    """A fire that can't be tried: unknown step, an entity not started, or a step that doesn't
    leave the entity's state."""


@dataclass(frozen=True)
class Edge:
    """One row of the transition list, with a tuple ``from`` expanded."""

    from_state: States
    to_state: States
    step: Callable


@dataclass(frozen=True)
class StateInfo:
    name: str
    value: str
    kind: str
    description: str


@dataclass(frozen=True)
class Graph:
    """A machine as data, for review: its states and its rows."""

    name: str
    states: list[StateInfo]
    transitions: list[tuple[str, str, str]]  # (from, to, step name)


@dataclass(frozen=True)
class _Exit:
    """One step out of one state, with the targets its rows give it there."""

    step: Callable
    targets: tuple[States, ...]


class Machine:
    """An entity lifecycle. ``Machine(...)`` checks the whole declaration and raises on a fault."""

    def __init__(
        self,
        states: type[States],
        transitions: Iterable,
        *,
        load: Callable[[str], object],
        backend: BaseStateBackend | str | None = None,
        name: str | None = None,
    ):
        if not (isinstance(states, type) and issubclass(states, States) and list(states)):
            raise MachineError(f"{states!r} is not a States class with states declared")
        self.states = states
        self.name = name or states.__name__.lower()
        self.load = load
        self._backend = backend or os.environ.get(BACKEND_ENV)
        if not self._backend:
            raise MachineError(f"{self.name} has no state backend: pass backend= or set {BACKEND_ENV}")
        self.edges = _expand(states, list(transitions))
        self.steps = _index_steps(self.edges)
        self.initial = _the_initial_state(states)
        self._exits = _exits_by_state(self.edges)
        _check_finals_have_no_rows(self._exits)
        _check_nothing_stuck(states, self._exits)
        _check_all_reachable(states, self.initial, self._exits)

    @cached_property
    def backend(self) -> BaseStateBackend:
        """The state backend, built on first use so its package is imported only when needed."""
        return get_backend(self._backend)

    # ── Review ──

    def graph(self) -> Graph:
        """The states with their descriptions and every row, in declaration order."""
        return Graph(
            name=self.name,
            states=[StateInfo(s.name, s.value, s.kind, s.description) for s in self.states],
            transitions=[(e.from_state.value, e.to_state.value, _step_name(e.step)) for e in self.edges],
        )

    # ── Reads ──

    def state_of(self, entity_id) -> States | None:
        """The entity's current state, or ``None`` if it hasn't started."""
        current = self.backend.current(self.name, str(entity_id))
        return self._state(current) if current else None

    def in_state(self, state: States) -> list[str]:
        """The ids of the entities in ``state``."""
        return self.backend.in_state(self.name, self._member(state).value)

    def history(self, entity_id) -> list[Move]:
        """Every move logged for the entity, applied and refused, oldest first."""
        return self.backend.history(self.name, str(entity_id))

    # ── Moves ──

    def start(self, entity_id) -> Move:
        """Put an entity in the initial state. Raises ``AlreadyStarted`` if it has a state."""
        return self.backend.start(self.name, str(entity_id), self.initial.value)

    def fire(self, entity_id, step: Callable | str) -> Move | None:
        """Run one step on one entity: the logged move, or ``None`` if the step said "not now"."""
        step = self._resolve_step(step)
        entity_id = str(entity_id)
        current = self.backend.current(self.name, entity_id)
        if current is None:
            raise TransitionError(f"{self.name}:{entity_id} hasn't started")
        state = self._state(current)
        exit_ = self._exits.get(state, {}).get(_step_name(step))
        if exit_ is None:
            raise TransitionError(f"{_step_name(step)} doesn't leave {state.value}")
        return self._try(current, state, exit_, self.load(entity_id))

    def advance(self) -> list[Move]:
        """Try the steps out of each non-final entity's state, in list order.

        The first step that doesn't return ``None`` decides, so each entity makes at most one move
        per call. Returns the moves logged, applied and refused.
        """
        moves = []
        for current in self.backend.all_current(self.name):
            state = self._state(current)
            exits = self._exits.get(state, {}).values()
            if not exits:
                continue
            entity = self.load(current.entity)
            for exit_ in exits:
                move = self._try(current, state, exit_, entity)
                if move is not None:
                    moves.append(move)
                    break
        return moves

    def _try(self, current: Current, state: States, exit_: _Exit, entity) -> Move | None:
        result = exit_.step(entity)
        if result is None:
            return None
        move = Move(
            machine=self.name,
            entity=current.entity,
            step=_step_name(exit_.step),
            from_state=state.value,
            to_state=_stored(result),
            applied=True,
            decided_by="code",
            at=utc_now(),
        )
        refusal = self._refusal(state, exit_, result)
        if refusal:
            return self.backend.refuse(replace(move, applied=False, reason=refusal))
        return self.backend.move(move, current.version)

    def _refusal(self, state: States, exit_: _Exit, result) -> str | None:
        if not isinstance(result, self.states):
            return f"{result!r} is not a state of {self.states.__name__}"
        if result not in exit_.targets:
            allowed = ", ".join(t.value for t in exit_.targets)
            return f"{_step_name(exit_.step)} may move {state.value} only to {allowed}"
        return None

    # ── Lookups ──

    def _resolve_step(self, step: Callable | str) -> Callable:
        name = step if isinstance(step, str) else _step_name(step)
        found = self.steps.get(name)
        if found is None or (not isinstance(step, str) and found is not step):
            raise TransitionError(f"{name!r} is not a step of {self.name}")
        return found

    def _state(self, current: Current) -> States:
        try:
            return self.states(current.state)
        except ValueError:
            raise TransitionError(
                f"{self.name}:{current.entity} is stored in {current.state!r}, "
                f"which is not a state of {self.states.__name__}"
            ) from None

    def _member(self, state) -> States:
        if not isinstance(state, self.states):
            raise TransitionError(f"{state!r} is not a state of {self.states.__name__}")
        return state


# ── Build-time checks ──


def _expand(states: type[States], rows: list) -> list[Edge]:
    edges = []
    for index, row in enumerate(rows):
        if not (isinstance(row, tuple) and len(row) == 3):
            raise MachineError(f"row {index} is {row!r}: a row is a (from, to, step) tuple")
        sources, target, step = row
        sources = sources if isinstance(sources, tuple) else (sources,)
        if not sources:
            raise MachineError(f"row {index} has an empty from tuple")
        for state in (*sources, target):
            _check_member(states, state, index)
        _check_step(step, index)
        edges.extend(Edge(source, target, step) for source in sources)
    return edges


def _check_member(states: type[States], state, index: int):
    if not isinstance(state, states):
        raise MachineError(f"row {index}: {state!r} is not a state of {states.__name__}")


def _check_step(step, index: int):
    if not callable(step) or isinstance(step, type):
        raise MachineError(f"row {index}: the step {step!r} is not a function")
    name = getattr(step, "__name__", None)
    if not name or name == "<lambda>":
        raise MachineError(f"row {index}: the step {step!r} has no name; define it with def")


def _index_steps(edges: list[Edge]) -> dict[str, Callable]:
    steps = {}
    for edge in edges:
        name = _step_name(edge.step)
        if steps.setdefault(name, edge.step) is not edge.step:
            raise MachineError(f"two different steps are named {name!r}")
    return steps


def _the_initial_state(states: type[States]) -> States:
    initials = [s for s in states if s.is_initial]
    if len(initials) != 1:
        found = ", ".join(s.name for s in initials) or "none"
        raise MachineError(f"{states.__name__} needs exactly one InitialState, found {found}")
    return initials[0]


def _exits_by_state(edges: list[Edge]) -> dict[States, dict[str, _Exit]]:
    """For each state, its steps in list order, each with the targets its rows give it there."""
    targets: dict[States, dict[str, list[States]]] = {}
    steps: dict[str, Callable] = {}
    for edge in edges:
        name = _step_name(edge.step)
        steps[name] = edge.step
        row_targets = targets.setdefault(edge.from_state, {}).setdefault(name, [])
        if edge.to_state not in row_targets:
            row_targets.append(edge.to_state)
    return {
        state: {name: _Exit(steps[name], tuple(ts)) for name, ts in by_step.items()}
        for state, by_step in targets.items()
    }


def _check_finals_have_no_rows(exits: dict[States, dict[str, _Exit]]):
    for state in exits:
        if state.is_final:
            raise MachineError(f"{state.name} is a FinalState but has rows leaving it")


def _check_nothing_stuck(states: type[States], exits: dict[States, dict[str, _Exit]]):
    for state in states:
        if not state.is_final and state not in exits:
            raise MachineError(f"{state.name} has no rows leaving it; make it a FinalState or add one")


def _check_all_reachable(states: type[States], initial: States, exits):
    reached, queue = {initial}, deque([initial])
    while queue:
        for exit_ in exits.get(queue.popleft(), {}).values():
            for target in exit_.targets:
                if target not in reached:
                    reached.add(target)
                    queue.append(target)
    unreachable = [s.name for s in states if s not in reached]
    if unreachable:
        raise MachineError(f"not reachable from {initial.name}: {', '.join(unreachable)}")


# ── Helpers ──


def _step_name(step: Callable) -> str:
    return step.__name__


def _stored(result) -> str:
    if isinstance(result, States):
        return result.value
    return (result if isinstance(result, str) else repr(result))[:255]


