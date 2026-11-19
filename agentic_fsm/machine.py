"""A machine: a ``States`` class whose states each name their handler, one list of ``(from, to)``
edges, and a state backend.

    orders = Machine(Order, EDGES, load=get_order)

A state's handler is a function of the entity that returns the next state, or ``None`` for "not
now", or another handler to hand over to (a ``Decide`` puts the question to a model). Its allowed
targets are the states its state has an edge to; anything else it returns is a bug, raised as
``TransitionError`` with nothing written. Every row in the log is a move that happened.
"""

import logging
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cached_property

from .backends import BACKEND_ENV, get_backend
from .backends.base.base import AlreadyStarted, BaseStateBackend, Current, Move, utc_now
from .conf import settings
from .decide import Brief, Decide, Decision, answer_fields
from .states import States
from .handlers import Handler

logger = logging.getLogger(__name__)

ANSWERED_BY = "agent"


class MachineError(ValueError):
    """The machine's declaration is wrong; raised by ``Machine(...)``."""


class TransitionError(Exception):
    """A move that can't be made: an entity not started or in a final state, a result outside
    the state's edges, an answer that doesn't validate or that the state's code leaves no room
    for. Nothing is written."""


class ProgressError(Exception):
    """``progress_all`` moved every entity it could, and some entities' handlers raised.

    ``moves`` are the moves made, each committed; ``failures`` maps each entity that raised to
    its exception. A failed entity's move, and its handler's own writes, were undone.
    """

    def __init__(self, moves: list[Move], failures: dict[str, Exception]):
        self.moves = moves
        self.failures = failures
        listed = "; ".join(f"{entity}: {type(error).__name__}: {error}" for entity, error in failures.items())
        super().__init__(f"{len(failures)} of the entities raised, {len(moves)} moved: {listed}")


@dataclass(frozen=True)
class Edge:
    """One pair of the edge list, with a tuple ``from`` expanded."""

    from_state: States
    to_state: States


@dataclass(frozen=True)
class StateInfo:
    name: str
    value: str
    kind: str
    description: str
    tags: tuple[str, ...] = ()
    handler: str | None = None  # what the state's handler is called; None for a final state


@dataclass(frozen=True)
class Graph:
    """A machine as data, for review: its states and its edges."""

    name: str
    states: list[StateInfo]
    edges: list[tuple[str, str]]  # (from, to)


class Machine:
    """An entity lifecycle. ``Machine(...)`` checks the whole declaration and raises on a fault."""

    def __init__(
        self,
        states: type[States],
        edges: Iterable,
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
        self._listeners: list[Callable[[Move], None]] = []
        self._backend = backend or settings.BACKEND
        if not self._backend:
            raise MachineError(f"{self.name} has no state backend: pass backend= or set {BACKEND_ENV}")
        self.edges = _expand(states, list(edges))
        self.initial = _the_initial_state(states)
        self._targets = _targets_by_state(self.edges)
        _check_handlers(states)
        _check_finals_have_no_edges(self._targets)
        _check_nothing_stuck(states, self._targets)
        _check_all_reachable(states, self.initial, self._targets)

    @cached_property
    def backend(self) -> BaseStateBackend:
        """The state backend, built on first use so its package is imported only when needed."""
        return get_backend(self._backend)

    # ── Review ──

    def graph(self) -> Graph:
        """The states with their descriptions and handlers, and every edge, in declaration order."""
        return Graph(
            name=self.name,
            states=[
                StateInfo(
                    s.name, s.value, s.kind, s.description, tuple(sorted(s.tags)), s.handler.label if s.handler else None
                )
                for s in self.states
            ],
            edges=[(e.from_state.value, e.to_state.value) for e in self.edges],
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

    # ── Listeners ──

    def on_move(self, listener: Callable[[Move], None]) -> Callable[[Move], None]:
        """Call ``listener(move)`` after every move this machine makes, a start included.

        Returns ``listener``, so it works as a decorator. A listener is told once the move is
        committed, in the process that made it: one made inside a transaction that is later
        undone is never told. Listeners are called in the order they were added. One that
        raises is logged and the others are still told: the move happened, so nothing is
        raised to whoever made it.
        """
        self._listeners.append(listener)
        return listener

    def _announce(self, move: Move) -> Move:
        """Tell the listeners of ``move`` once it's committed; returns the move."""
        if self._listeners:
            self.backend.on_commit(lambda: self._tell(move))
        return move

    def _tell(self, move: Move):
        for listener in self._listeners:
            try:
                listener(move)
            except Exception:
                name = getattr(listener, "__name__", repr(listener))
                logger.exception("%s raised on %s:%s reaching %s", name, move.machine, move.entity, move.to_state)

    # ── Moves ──

    def start(self, entity_id) -> Move:
        """Put an entity in the initial state. Raises ``AlreadyStarted`` if it has a state."""
        return self._announce(self.backend.start(self.name, str(entity_id), self.initial.value))

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

    def fire(self, entity_id, *, entity=None, answer=None) -> Move | None:
        """Run the handler of the entity's state: the logged move, or ``None`` if it said "not now".

        The entity is locked from the read of its state to the write of the move, so a second
        caller waits and then reads the state this one left. ``entity`` is the entity already in
        hand, given to the handler instead of ``load(id)``: a caller walking its own rows, or one
        that has set on the entity what it chose for the handler.

        ``answer`` decides in the model's place: a ``Decide``'s fields as a dict or a JSON
        object, or a bare state value. No model is called and the prompt function isn't run.
        When the state's handler is a function that hands over to a ``Decide``, the function runs
        first and the answer is taken only if it hands over: one that moves the entity itself,
        or says "not now", refuses the answer. Only a handler that only reads (``@check``) is run
        this way. An answer that is refused, or that the schema or the state's edges don't
        allow, raises ``TransitionError`` with the reason, and nothing is written. A
        ``Decide``'s ``effect`` runs with the decision either way, before the move is written.
        """
        with self.backend.lock(self.name, str(entity_id)) as current:
            state = self._unfinished_state(entity_id, current)
            if answer is not None:
                return self._answer(current, state, answer, entity)
            return self._try(current, state, self._entity(current, entity))

    def brief(self, entity_id, *, entity=None) -> Brief:
        """What a ``Decide`` would give the model for this entity, for a session to decide instead.

        Returns a ``Brief``: the instructions, the prompt, the choices with their descriptions,
        the answer's JSON Schema and the agent's tools. Answer it with ``fire(..., answer=)``.
        The ``Decide`` is the handler of the entity's state, or the one its function hands over
        to, found as ``fire(..., answer=)`` finds it. ``entity`` is as for ``fire``.
        """
        current = self.backend.current(self.name, str(entity_id))
        state = self._unfinished_state(entity_id, current)
        decide, entity = self._decide_for(current, state, entity)
        return decide.brief(self._entity(current, entity), state, self._targets[state])

    def progress(self, entity_id) -> list[Move]:
        """Move one entity until it rests; returns the moves made, oldest first.

        Each round runs the handler of the entity's state on the entity loaded afresh, and commits
        its move. It rests when the handler doesn't move it (a final state, or "not now"), and
        stops after a move that brings it back to a state this call already left, so it makes
        at most as many moves as there are states.
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
        """Run the handler of each non-final entity's state: at most one move per entity per call.

        Each entity is locked and committed on its own. Returns the moves made.

        With ``tag``, only the entities in a state carrying it are tried: one worker per tag,
        each moving its own queue. A tag no state carries raises ``MachineError``.

        With ``limit``, it stops once that many moves are made, oldest entity first: a worker
        that takes a few from its queue each run. An entity that says "not now" or raises
        doesn't count, so one stuck at the head never holds the queue.

        An entity whose handler raises doesn't hold the others: its move is undone, the rest are
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
                if state.is_final or (tagged is not None and state not in tagged):
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
        """The unfinished entities their state's handler could move now; nothing is moved or written.

        For a scheduler deciding whether a run is worth starting. Each entity is tried as
        ``progress_all`` would try it, under its lock: a handler that only reads (a ``@check``
        function) runs and says whether it would move the entity; any other handler (an outside
        effect, a ``Decide``) can't be tried without its effect, so an entity is due when its
        state's handler is one, or hands over to one. With ``tag``, only the entities in a state
        carrying it, as ``progress_all(tag)`` would try. Returns the entity ids, oldest first.
        """
        tagged = self._tagged(tag)
        return [
            current.entity
            for current in self.backend.all_current(self.name)
            if not (state := self._state(current)).is_final
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
        """Under the entity's lock, whether its state's handler would move it."""
        with self.backend.lock(self.name, entity_id) as current:
            state = self._state(current)
            if state.is_final:
                return False
            handler, entity = state.handler, None
            while handler.reads_only:
                entity = self._entity(current, entity)
                result = handler.run(entity, self._targets[state])
                if not isinstance(result, Handler):
                    return result is not None
                handler = result
        return True

    def _one_move(self, entity_id: str) -> Move | None:
        """Under the entity's lock, the move its state's handler makes, if any."""
        with self.backend.lock(self.name, entity_id) as current:
            state = self._state(current)
            if state.is_final:
                return None
            return self._try(current, state, self.load(current.entity))

    def _try(self, current: Current, state: States, entity) -> Move | None:
        """Run the state's handler and each handler it hands over to; write the move the last one decides."""
        handler, targets = state.handler, self._targets[state]
        result = handler.run(entity, targets)
        while isinstance(result, Handler):
            handler = result
            result = handler.run(entity, targets)
        if result is None:
            return None
        return self._apply(current, state, handler, result, handler.decided_by, entity)

    def _answer(self, current: Current, state: States, answer, entity) -> Move:
        """Apply an answer given in the model's place; raise with the reason if it can't be."""
        decide, entity = self._decide_for(current, state, entity)
        try:
            decision = decide.parse(answer_fields(answer), self._targets[state])
        except ValueError as error:
            raise TransitionError(f"the answer for {state.value} doesn't validate: {error}") from None
        if decide.effect is not None:
            entity = self._entity(current, entity)
        return self._apply(current, state, decide, decision, ANSWERED_BY, entity)

    def _decide_for(self, current: Current, state: States, entity) -> tuple[Decide, object]:
        """The ``Decide`` that decides for the entity in ``state``, and the entity if it was loaded.

        It is the state's handler, or the one that handler hands over to. A handler is run to find out
        only if it only reads; one that settles the move itself leaves nothing to decide.
        """
        handler, subject = state.handler, f"{self.name}:{current.entity}"
        while not isinstance(handler, Decide):
            if not handler.reads_only:
                raise TransitionError(
                    f"{state.value}'s handler is code that doesn't only read: no model decides {subject} there"
                )
            entity = self._entity(current, entity)
            result = handler.run(entity, self._targets[state])
            if result is None:
                raise TransitionError(f'{state.value}\'s handler says "not now" for {subject}: there is nothing to decide')
            if not isinstance(result, Handler):
                target = result.to if isinstance(result, Decision) else result
                raise TransitionError(
                    f"{state.value}'s handler moves {subject} to {getattr(target, 'value', target)} itself: "
                    "there is nothing to decide"
                )
            handler = result
        return handler, entity

    def _apply(self, current: Current, state: States, handler: Handler, result, decided_by: str, entity) -> Move:
        """Write a handler's result as a move; a result outside the state's edges is a bug and raises.

        The handler's effect runs here, once the result is allowed and before the move.
        """
        decided = isinstance(result, Decision)
        target = result.to if decided else result
        self._check_target(state, target)
        if handler.effect is not None:
            handler.effect(entity, result)
        move = self.backend.move(
            Move(
                machine=self.name,
                entity=current.entity,
                from_state=state.value,
                to_state=target.value,
                decided_by=decided_by,
                at=utc_now(),
                decision=result.model_dump(mode="json") if decided else None,
            )
        )
        return self._announce(move)

    def _check_target(self, state: States, target):
        if not isinstance(target, self.states):
            raise TransitionError(
                f"{state.value}'s handler returned {target!r}, which is not a state of {self.states.__name__}"
            )
        if target not in self._targets[state]:
            allowed = ", ".join(t.value for t in self._targets[state])
            raise TransitionError(
                f"{state.value}'s handler returned {target.value}, but {state.value} has edges only to {allowed}"
            )

    # ── Lookups ──

    def _entity(self, current: Current, entity):
        return self.load(current.entity) if entity is None else entity

    def _unfinished_state(self, entity_id, current: Current | None) -> States:
        """The entity's state, which has a handler; raises if it hasn't started or is final."""
        if current is None:
            raise TransitionError(f"{self.name}:{entity_id} hasn't started")
        state = self._state(current)
        if state.is_final:
            raise TransitionError(f"{self.name}:{entity_id} is in {state.value}, a final state: nothing leaves it")
        return state

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


def _expand(states: type[States], pairs: list) -> list[Edge]:
    edges = []
    for index, pair in enumerate(pairs):
        if not (isinstance(pair, tuple) and len(pair) == 2):
            raise MachineError(f"edge {index} is {pair!r}: an edge is a (from, to) tuple")
        sources, target = pair
        sources = sources if isinstance(sources, tuple) else (sources,)
        if not sources:
            raise MachineError(f"edge {index} has an empty from tuple")
        for state in (*sources, target):
            if not isinstance(state, states):
                raise MachineError(f"edge {index}: {state!r} is not a state of {states.__name__}")
        edges.extend(Edge(source, target) for source in sources)
    return edges


def _the_initial_state(states: type[States]) -> States:
    initials = [s for s in states if s.is_initial]
    if len(initials) != 1:
        found = ", ".join(s.name for s in initials) or "none"
        raise MachineError(f"{states.__name__} needs exactly one InitialState, found {found}")
    return initials[0]


def _targets_by_state(edges: list[Edge]) -> dict[States, tuple[States, ...]]:
    """For each state with an edge out, the states it leads to, in list order."""
    targets: dict[States, list[States]] = {}
    for edge in edges:
        leads_to = targets.setdefault(edge.from_state, [])
        if edge.to_state not in leads_to:
            leads_to.append(edge.to_state)
    return {state: tuple(leads_to) for state, leads_to in targets.items()}


def _check_handlers(states: type[States]):
    for state in states:
        if state.is_final and state.handler is not None:
            raise MachineError(f"{state.name} is a FinalState but has a handler")
        if not state.is_final and state.handler is None:
            raise MachineError(f"{state.name} has no handler; give it one with handler= or make it a FinalState")


def _check_finals_have_no_edges(targets: dict[States, tuple[States, ...]]):
    for state in targets:
        if state.is_final:
            raise MachineError(f"{state.name} is a FinalState but has edges leaving it")


def _check_nothing_stuck(states: type[States], targets: dict[States, tuple[States, ...]]):
    for state in states:
        if not state.is_final and state not in targets:
            raise MachineError(f"{state.name} has no edges leaving it; make it a FinalState or add one")


def _check_all_reachable(states: type[States], initial: States, targets: dict[States, tuple[States, ...]]):
    reached, queue = {initial}, deque([initial])
    while queue:
        for target in targets.get(queue.popleft(), ()):
            if target not in reached:
                reached.add(target)
                queue.append(target)
    unreachable = [s.name for s in states if s not in reached]
    if unreachable:
        raise MachineError(f"not reachable from {initial.name}: {', '.join(unreachable)}")
