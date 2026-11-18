"""``Machine(...)`` refuses a faulty declaration with an error naming the state or the edge."""

import pytest

from agentic_fsm import FinalState, InitialState, Machine, MachineError, State, States
from agentic_fsm.backends import BACKEND_ENV

from .conftest import EDGES, Order

LOCMEM = "agentic_fsm.backends.locmem"


def build(states, edges, **kwargs):
    return Machine(states, edges, load=lambda entity_id: None, backend=LOCMEM, **kwargs)


def open_it(door):
    return Door.open


def close_or_remove(door):
    return Door.removed if door.broken else Door.closed


class Door(States):
    closed = InitialState("Shut", step=open_it)
    open = State("Open", step=close_or_remove)
    removed = FinalState("Taken off its hinges")


DOOR_EDGES = [(Door.closed, Door.open), (Door.open, Door.closed), (Door.open, Door.removed)]


# ── A sound declaration ──


def test_a_sound_machine_builds_and_is_named_after_its_states():
    orders = build(Order, EDGES)
    assert orders.name == "order"
    assert orders.initial is Order.pending
    assert build(Order, EDGES, name="purchase").name == "purchase"


def test_graph_lists_states_with_their_steps_and_one_pair_per_edge():
    graph = build(Order, EDGES).graph()
    assert [(s.name, s.kind, s.step) for s in graph.states] == [
        ("pending", "initial", "await_payment"),
        ("paid", "state", "pack"),
        ("shipped", "state", "await_delivery"),
        ("delivered", "final", None),
        ("cancelled", "final", None),
    ]
    assert graph.states[1].description == "Payment cleared, waiting to ship"
    assert graph.edges == [
        ("pending", "cancelled"),
        ("paid", "cancelled"),
        ("pending", "paid"),
        ("paid", "shipped"),
        ("shipped", "delivered"),
    ]


# ── States ──


def test_refuses_no_initial_state():
    class NoStart(States):
        a = State("A", step=open_it)
        b = FinalState("B")

    with pytest.raises(MachineError, match="exactly one InitialState, found none"):
        build(NoStart, [(NoStart.a, NoStart.b)])


def test_refuses_two_initial_states():
    class TwoStarts(States):
        a = InitialState("A", step=open_it)
        b = InitialState("B", step=open_it)
        c = FinalState("C")

    with pytest.raises(MachineError, match="exactly one InitialState, found a, b"):
        build(TwoStarts, [((TwoStarts.a, TwoStarts.b), TwoStarts.c)])


def test_refuses_an_unreachable_state():
    with pytest.raises(MachineError, match="not reachable from closed: open"):
        build(Door, [(Door.closed, Door.removed), (Door.open, Door.removed)])


def test_refuses_edges_leaving_a_final_state():
    with pytest.raises(MachineError, match="removed is a FinalState but has edges leaving it"):
        build(Door, [*DOOR_EDGES, (Door.removed, Door.closed)])


def test_refuses_a_stuck_state():
    with pytest.raises(MachineError, match="open has no edges leaving it"):
        build(Door, [(Door.closed, Door.open), (Door.closed, Door.removed)])


def test_refuses_something_that_is_not_a_states_class():
    with pytest.raises(MachineError, match="is not a States class"):
        build(States, [])
    with pytest.raises(MachineError, match="is not a States class"):
        build("Order", [])


# ── Steps ──


def test_refuses_a_state_with_no_step():
    class Gate(States):
        closed = InitialState("Shut", step=open_it)
        open = State("Open")
        removed = FinalState("Taken off its hinges")

    with pytest.raises(MachineError, match="open has no step; give it one with step= or make it a FinalState"):
        build(Gate, [(Gate.closed, Gate.open), (Gate.open, Gate.removed)])


def test_refuses_a_final_state_with_a_step():
    class Gate(States):
        closed = InitialState("Shut", step=open_it)
        removed = FinalState("Taken off its hinges", step=open_it)

    with pytest.raises(MachineError, match="removed is a FinalState but has a step"):
        build(Gate, [(Gate.closed, Gate.removed)])


def test_one_step_may_serve_several_states():
    def leave(gate):
        return Gate.removed

    class Gate(States):
        closed = InitialState("Shut", step=leave)
        open = State("Open", step=leave)
        removed = FinalState("Taken off its hinges")

    gates = build(Gate, [(Gate.closed, Gate.open), ((Gate.closed, Gate.open), Gate.removed)])
    assert Gate.closed.step.function is Gate.open.step.function is leave
    assert gates.graph().states[1].step == "leave"


# ── Edges ──


def test_refuses_a_state_from_another_machine():
    with pytest.raises(MachineError, match=r"edge 3: <Order.paid: 'paid'> is not a state of Door"):
        build(Door, [*DOOR_EDGES, (Door.closed, Order.paid)])


@pytest.mark.parametrize("edge", [(Door.closed, Door.open, open_it), "closed -> open", [Door.closed, Door.open]])
def test_refuses_an_edge_that_is_not_a_pair(edge):
    with pytest.raises(MachineError, match=r"edge 3 is .*an edge is a \(from, to\) tuple"):
        build(Door, [*DOOR_EDGES, edge])


def test_refuses_an_empty_from_tuple():
    with pytest.raises(MachineError, match="edge 3 has an empty from tuple"):
        build(Door, [*DOOR_EDGES, ((), Door.open)])


# ── Backend ──


def test_refuses_a_machine_with_no_backend(monkeypatch):
    monkeypatch.delenv(BACKEND_ENV, raising=False)
    with pytest.raises(MachineError, match="order has no state backend"):
        Machine(Order, EDGES, load=lambda entity_id: None)


def test_takes_the_backend_from_the_environment(monkeypatch):
    monkeypatch.setenv(BACKEND_ENV, LOCMEM)
    orders = Machine(Order, EDGES, load=lambda entity_id: None)
    assert type(orders.backend).__module__ == "agentic_fsm.backends.locmem.base"


@pytest.mark.parametrize("path", ["nodots", "agentic_fsm.backends.missing", "agentic_fsm.backends.base"])
def test_a_bad_backend_path_fails_on_first_use(path):
    orders = Machine(Order, EDGES, load=lambda entity_id: None, backend=path)
    with pytest.raises(LookupError):
        orders.backend
