"""A machine: a ``States`` class, one list of ``(from, to, step)`` rows, and a state backend.

    orders = Machine(Order, TRANSITIONS, load=get_order)

A step is a function of the entity that returns the next state, or ``None`` for "not now", or a
``Decide`` that puts the question to a model. Its allowed targets are the rows it sits on; anything
else it returns is a bug, raised as ``TransitionError`` with nothing written. Every row in the log
is a move that happened.
"""

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cached_property

from .backends import BACKEND_ENV, get_backend
from .backends.base.base import AlreadyStarted, BaseStateBackend, Current, Move, utc_now
from .conf import settings
from .decide import Brief, Decide, Decision, answer_fields
from .states import States


class MachineError(ValueError):
    """The machine's declaration is wrong; raised by ``Machine(...)``."""


class TransitionError(Exception):
    """A move that can't be made: unknown step, an entity not started, a step that doesn't leave
    the entity's state, a result outside the step's rows, or an answer that doesn't validate.
    Nothing is written."""


class ProgressError(Exception):
    """``progress_all`` moved every entity it could, and some entities' steps raised.

    ``moves`` are the moves made, each committed; ``failures`` maps each entity that raised to
    its exception. A failed entity's move, and its step's own writes, were undone.
    """

    def __init__(self, moves: list[Move], failures: dict[str, Exception]):
        self.moves = moves
        self.failures = failures
        listed = "; ".join(f"{entity}: {type(error).__name__}: {error}" for entity, error in failures.items())
        super().__init__(f"{len(failures)} of the entities raised, {len(moves)} moved: {listed}")


CHECK_ATTRIBUTE = "agentic_fsm_check"


def check(step: Callable) -> Callable:
    """Mark a code step as a check: it only reads the entity, so ``Machine.due`` may run it.

    Used as a decorator. A ``Decide`` can't be one: running it calls a model.
    """
    if isinstance(step, Decide) or not callable(step):
        raise TypeError(f"{step!r} can't be a check: only a code step that only reads can")
    setattr(step, CHECK_ATTRIBUTE, True)
    return step


def is_check(step: Callable) -> bool:
    """Whether ``step`` was marked with ``check``."""
    return getattr(step, CHECK_ATTRIBUTE, False) is True


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
    tags: tuple[str, ...] = ()


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
        self._backend = backend or settings.BACKEND
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
            states=[
                StateInfo(s.name, s.value, s.kind, s.description, tuple(sorted(s.tags))) for s in self.states
            ],
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
        """Every move logged for the entity, oldest first."""
        return self.backend.history(self.name, str(entity_id))

    # ── Moves ──

    def start(self, entity_id) -> Move:
        """Put an entity in the initial state. Raises ``AlreadyStarted`` if it has a state."""
        return self.backend.start(self.name, str(entity_id), self.initial.value)

    def start_all(self, entity_ids: Iterable) -> list[Move]:
        """Start each of ``entity_ids`` that has no state yet; returns the moves made, in order.

        For entities made elsewhere (rows another program inserts), started by whoever runs the
        machine: hand it every id there is, and the ones already started are left alone. An id
        another caller starts first is skipped.
        """
        started = {current.entity for current in self.backend.all_current(self.name)}
        moves = []
        for entity_id in map(str, entity_ids):
            if entity_id in started:
                continue
            started.add(entity_id)
            try:
                moves.append(self.start(entity_id))
            except AlreadyStarted:
                continue
        return moves

    def fire(self, entity_id, step: Callable | str, *, entity=None, answer=None) -> Move | None:
        """Run one step on one entity: the logged move, or ``None`` if the step said "not now".

        The entity is locked from the read of its state to the write of the move, so a second
        caller waits and then reads the state this one left. ``entity`` is the entity already in
        hand, given to the step instead of ``load(id)``: a caller walking its own rows, or one
        that has set on the entity what it chose for the step. ``answer`` decides a ``Decide``
        step in the model's place: its fields as a dict or a JSON object, or a bare state value.
        No model is called and the prompt function isn't run. An answer the schema or the
        step's rows don't allow raises ``TransitionError`` with the reason, and nothing is written.
        """
        step = self._resolve_step(step)
        with self.backend.lock(self.name, str(entity_id)) as current:
            state, exit_ = self._exit_from(entity_id, current, step)
            if answer is not None:
                return self._answer(current, state, exit_, answer)
            return self._try(current, state, exit_, self._entity(current, entity))

    def brief(self, entity_id, step: Callable | str, *, entity=None) -> Brief:
        """What a ``Decide`` step would give the model for this entity, for a session to decide instead.

        Returns a ``Brief``: the instructions, the prompt, the choices with their descriptions,
        the answer's JSON Schema and the agent's tools. Answer it with ``fire(..., answer=)``.
        ``entity`` is as for ``fire``.
        """
        step = self._resolve_step(step)
        current = self.backend.current(self.name, str(entity_id))
        state, exit_ = self._exit_from(entity_id, current, step)
        if not isinstance(exit_.step, Decide):
            raise TransitionError(f"{_step_name(exit_.step)} is code, not a Decide: there is nothing to brief")
        return exit_.step.brief(self._entity(current, entity), exit_.targets)

    def progress(self, entity_id) -> list[Move]:
        """Move one entity until it rests; returns the moves made, oldest first.

        Each round tries the steps out of the entity's state in list order, as ``progress_all``
        does, on the entity loaded afresh, and commits its move. It rests when no step moves it
        (a final state, or every step says "not now"), and stops after a move that brings it back
        to a state this call already left, so it makes at most as many moves as there are states.
        """
        entity_id = str(entity_id)
        if self.backend.current(self.name, entity_id) is None:
            raise TransitionError(f"{self.name}:{entity_id} hasn't started")
        moves, left = [], set()
        while (move := self._one_move(entity_id)) is not None:
            moves.append(move)
            left.add(move.from_state)
            if move.to_state in left:
                break
        return moves

    def progress_all(self, tag: str | None = None, *, limit: int | None = None) -> list[Move]:
        """Try the steps out of each non-final entity's state, in list order.

        The first step that doesn't return ``None`` decides, so each entity makes at most one move
        per call. Each entity is locked and committed on its own. Returns the moves made.

        With ``tag``, only the entities in a state carrying it are tried: one worker per tag,
        each moving its own queue. A tag no state carries raises ``MachineError``.

        With ``limit``, it stops once that many moves are made, oldest entity first: a worker
        that takes a few from its queue each run. An entity that says "not now" or raises
        doesn't count, so one stuck at the head never holds the queue.

        An entity whose step raises doesn't hold the others: its move is undone, the rest are
        tried, and ``ProgressError`` is raised at the end with the moves made and each failure.
        """
        if limit is not None and limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit!r}")
        tagged = self._tagged(tag)
        moves, failures = [], {}
        for current in self.backend.all_current(self.name):
            if limit is not None and len(moves) >= limit:
                break
            try:
                state = self._state(current)
                if not self._exits.get(state) or (tagged is not None and state not in tagged):
                    continue
                move = self._one_move(current.entity)
            except Exception as error:
                failures[current.entity] = error
                continue
            if move is not None:
                moves.append(move)
        if failures:
            raise ProgressError(moves, failures)
        return moves

    def due(self, tag: str | None = None) -> list[str]:
        """The unfinished entities a step could move now; nothing is moved or written.

        For a scheduler deciding whether a run is worth starting. Each entity is tried as
        ``progress_all`` would try it, under its lock: a ``check`` step runs and says whether it
        would move the entity; any other step (an outside effect, a ``Decide``) can't be tried
        without its effect, so an entity it leaves is due once the checks before it say "not
        now". With ``tag``, only the entities in a state carrying it, as ``progress_all(tag)``
        would try. Returns the entity ids, oldest first.
        """
        tagged = self._tagged(tag)
        return [
            current.entity
            for current in self.backend.all_current(self.name)
            if self._exits.get(state := self._state(current))
            and (tagged is None or state in tagged)
            and self._would_move(current.entity)
        ]

    def _tagged(self, tag: str | None) -> set[States] | None:
        """The states carrying ``tag``; ``None`` for no tag, which is every state."""
        if tag is None:
            return None
        tagged = {state for state in self.states if tag in state.tags}
        if not tagged:
            known = ", ".join(sorted({t for state in self.states for t in state.tags})) or "none"
            raise MachineError(f"no state of {self.name} is tagged {tag!r}; its tags are {known}")
        return tagged

    def _would_move(self, entity_id: str) -> bool:
        """Under the entity's lock, whether the first step to answer would move it."""
        with self.backend.lock(self.name, entity_id) as current:
            entity, loaded = None, False
            for exit_ in self._exits[self._state(current)].values():
                if not is_check(exit_.step):
                    return True
                if not loaded:
                    entity, loaded = self.load(current.entity), True
                if exit_.step(entity) is not None:
                    return True
        return False

    def _one_move(self, entity_id: str) -> Move | None:
        """Under the entity's lock, the first move a step out of its state makes, if any."""
        with self.backend.lock(self.name, entity_id) as current:
            state = self._state(current)
            exits = self._exits.get(state, {}).values()
            entity = self.load(current.entity) if exits else None
            for exit_ in exits:
                move = self._try(current, state, exit_, entity)
                if move is not None:
                    return move
        return None

    def _try(self, current: Current, state: States, exit_: _Exit, entity) -> Move | None:
        if isinstance(exit_.step, Decide):
            result, decided_by = exit_.step.run(entity, exit_.targets), exit_.step.decided_by
        else:
            result, decided_by = exit_.step(entity), "code"
        if result is None:
            return None
        return self._apply(current, state, exit_, result, decided_by)

    def _answer(self, current: Current, state: States, exit_: _Exit, answer) -> Move:
        """Apply an answer given in the model's place; raise with the reason if it can't be."""
        step = exit_.step
        if not isinstance(step, Decide):
            raise TransitionError(f"{_step_name(step)} is code, not a Decide: it takes no answer")
        try:
            decision = step.parse(answer_fields(answer), exit_.targets)
        except ValueError as error:
            raise TransitionError(f"the answer to {step.name} doesn't validate: {error}") from None
        return self._apply(current, state, exit_, decision, "agent")

    def _apply(self, current: Current, state: States, exit_: _Exit, result, decided_by: str) -> Move:
        """Write a step's result as a move; a result outside its rows is a bug and raises."""
        decided = isinstance(result, Decision)
        target = result.to if decided else result
        self._check_target(state, exit_, target)
        return self.backend.move(
            Move(
                machine=self.name,
                entity=current.entity,
                step=_step_name(exit_.step),
                from_state=state.value,
                to_state=target.value,
                decided_by=decided_by,
                at=utc_now(),
                decision=result.model_dump(mode="json") if decided else None,
            )
        )

    def _check_target(self, state: States, exit_: _Exit, target):
        if not isinstance(target, self.states):
            raise TransitionError(
                f"{_step_name(exit_.step)} returned {target!r}, which is not a state of {self.states.__name__}"
            )
        if target not in exit_.targets:
            allowed = ", ".join(t.value for t in exit_.targets)
            raise TransitionError(
                f"{_step_name(exit_.step)} returned {target.value}, but may move {state.value} only to {allowed}"
            )

    # ── Lookups ──

    def _entity(self, current: Current, entity):
        return self.load(current.entity) if entity is None else entity

    def _exit_from(self, entity_id, current: Current | None, step: Callable) -> tuple[States, _Exit]:
        """The entity's state and the step's exit from it; raises if there's none."""
        if current is None:
            raise TransitionError(f"{self.name}:{entity_id} hasn't started")
        state = self._state(current)
        exit_ = self._exits.get(state, {}).get(_step_name(step))
        if exit_ is None:
            raise TransitionError(f"{_step_name(step)} doesn't leave {state.value}")
        return state, exit_

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
        _check_step(states, step, index)
        edges.extend(Edge(source, target, step) for source in sources)
    return edges


def _check_member(states: type[States], state, index: int):
    if not isinstance(state, states):
        raise MachineError(f"row {index}: {state!r} is not a state of {states.__name__}")


def _check_step(states: type[States], step, index: int):
    if isinstance(step, Decide):
        if step.states not in (states, States):
            raise MachineError(
                f"row {index}: {step.name}'s Decision types `to` as {step.states.__name__}, "
                f"not {states.__name__}"
            )
        return
    if not callable(step) or isinstance(step, type):
        raise MachineError(f"row {index}: the step {step!r} is not a function or a Decide")
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


def _step_name(step) -> str:
    return step.name if isinstance(step, Decide) else step.__name__


