"""``Machine(...)`` refuses a faulty declaration with an error naming the state or the row."""

import pytest

from agentic_fsm import FinalState, InitialState, Machine, MachineError, State, States
from agentic_fsm.backends import BACKEND_ENV

from .conftest import TRANSITIONS, Order, cancel, payment_cleared

LOCMEM = "agentic_fsm.backends.locmem"


def build(states, rows, **kwargs):
    return Machine(states, rows, load=lambda entity_id: None, backend=LOCMEM, **kwargs)


class Door(States):
    closed = InitialState("Shut")
    open = State("Open")
    removed = FinalState("Taken off its hinges")


def open_it(door):
    return Door.open


def close_it(door):
    return Door.closed


def remove_it(door):
    return Door.removed


DOOR_ROWS = [(Door.closed, Door.open, open_it), (Door.open, Door.closed, close_it), (Door.open, Door.removed, remove_it)]


# ── A sound declaration ──


def test_a_sound_machine_builds_and_is_named_after_its_states():
    orders = build(Order, TRANSITIONS)
    assert orders.name == "order"
    assert orders.initial is Order.pending
    assert build(Order, TRANSITIONS, name="purchase").name == "purchase"


def test_graph_lists_states_and_one_row_per_edge():
    graph = build(Order, TRANSITIONS).graph()
    assert [(s.name, s.kind) for s in graph.states][:2] == [("pending", "initial"), ("paid", "state")]
    assert graph.states[1].description == "Payment cleared, waiting to ship"
    assert graph.transitions == [
        ("pending", "cancelled", "cancel"),
        ("paid", "cancelled", "cancel"),
        ("pending", "paid", "payment_cleared"),
        ("paid", "shipped", "ship"),
        ("shipped", "delivered", "carrier_delivered"),
    ]


# ── States ──


def test_refuses_no_initial_state():
    class NoStart(States):
        a = State("A")
        b = FinalState("B")

    with pytest.raises(MachineError, match="exactly one InitialState, found none"):
        build(NoStart, [(NoStart.a, NoStart.b, open_it)])


def test_refuses_two_initial_states():
    class TwoStarts(States):
        a = InitialState("A")
        b = InitialState("B")
        c = FinalState("C")

    with pytest.raises(MachineError, match="exactly one InitialState, found a, b"):
        build(TwoStarts, [((TwoStarts.a, TwoStarts.b), TwoStarts.c, open_it)])


def test_refuses_an_unreachable_state():
    rows = [(Door.closed, Door.removed, remove_it), (Door.open, Door.removed, remove_it)]
    with pytest.raises(MachineError, match="not reachable from closed: open"):
        build(Door, rows)


def test_refuses_rows_leaving_a_final_state():
    with pytest.raises(MachineError, match="removed is a FinalState but has rows leaving it"):
        build(Door, [*DOOR_ROWS, (Door.removed, Door.closed, close_it)])


def test_refuses_a_stuck_state():
    with pytest.raises(MachineError, match="open has no rows leaving it"):
        build(Door, [(Door.closed, Door.open, open_it), (Door.closed, Door.removed, remove_it)])


def test_refuses_something_that_is_not_a_states_class():
    with pytest.raises(MachineError, match="is not a States class"):
        build(States, [])
    with pytest.raises(MachineError, match="is not a States class"):
        build("Order", [])


# ── Rows ──


def test_refuses_a_state_from_another_machine():
    with pytest.raises(MachineError, match=r"row 3: <Order.paid: 'paid'> is not a state of Door"):
        build(Door, [*DOOR_ROWS, (Door.closed, Order.paid, payment_cleared)])


@pytest.mark.parametrize("row", [(Door.closed, Door.open), "closed -> open", [Door.closed, Door.open, open_it]])
def test_refuses_a_row_that_is_not_a_triple(row):
    with pytest.raises(MachineError, match=r"row 3 is .*a row is a \(from, to, step\) tuple"):
        build(Door, [*DOOR_ROWS, row])


def test_refuses_an_empty_from_tuple():
    with pytest.raises(MachineError, match="row 3 has an empty from tuple"):
        build(Door, [*DOOR_ROWS, ((), Door.open, open_it)])


# ── Steps ──


@pytest.mark.parametrize("step", ["open_it", None, Door])
def test_refuses_a_step_that_is_not_a_function(step):
    with pytest.raises(MachineError, match="row 3: the step .* is not a function"):
        build(Door, [*DOOR_ROWS, (Door.closed, Door.open, step)])


def test_refuses_a_lambda_step():
    with pytest.raises(MachineError, match="row 3: the step .* has no name"):
        build(Door, [*DOOR_ROWS, (Door.closed, Door.open, lambda door: Door.open)])


def test_refuses_two_steps_with_one_name():
    def cancel(door):  # same name as the order machine's step, a different function
        return Door.removed

    def other():
        def cancel(door):
            return Door.removed

        return cancel

    with pytest.raises(MachineError, match="two different steps are named 'cancel'"):
        build(Door, [*DOOR_ROWS, (Door.closed, Door.removed, cancel), (Door.open, Door.removed, other())])


def test_one_step_on_several_rows_is_one_step():
    orders = build(Order, TRANSITIONS)
    assert orders.steps["cancel"] is cancel


# ── Backend ──


def test_refuses_a_machine_with_no_backend(monkeypatch):
    monkeypatch.delenv(BACKEND_ENV, raising=False)
    with pytest.raises(MachineError, match="order has no state backend"):
        Machine(Order, TRANSITIONS, load=lambda entity_id: None)


def test_takes_the_backend_from_the_environment(monkeypatch):
    monkeypatch.setenv(BACKEND_ENV, LOCMEM)
    orders = Machine(Order, TRANSITIONS, load=lambda entity_id: None)
    assert type(orders.backend).__module__ == "agentic_fsm.backends.locmem.base"


@pytest.mark.parametrize("path", ["nodots", "agentic_fsm.backends.missing", "agentic_fsm.backends.base"])
def test_a_bad_backend_path_fails_on_first_use(path):
    orders = Machine(Order, TRANSITIONS, load=lambda entity_id: None, backend=path)
    with pytest.raises(LookupError):
        orders.backend
