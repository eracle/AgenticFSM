"""``on_move``: listeners told of each move once it's committed, on every state backend.

The Django backend runs here with real commits (``transaction=True``): inside pytest-django's
usual test transaction nothing ever commits, so no listener would be told.
"""

import logging

import pytest

from agentic_fsm import Machine, TransitionError

from .conftest import TRANSITIONS, Order, backend_named, new_order, payment_cleared, ship

UNDOES = ["sqlalchemy", pytest.param("django", marks=pytest.mark.django_db(transaction=True))]


@pytest.fixture(params=["locmem", *UNDOES])
def backend(request):
    return backend_named(request.param)


@pytest.fixture
def told(orders):
    """Every move the machine's listeners were told of, in order."""
    moves = []
    orders.on_move(moves.append)
    return moves


# ── What a listener is told ──


def test_a_listener_is_told_of_a_start_and_of_each_move(orders, table, told):
    table["1"] = new_order(payment_cleared=True, tracking_number="1Z")

    started = orders.start(1)
    moves = orders.progress(1)

    assert told == [started, *moves]
    assert [move.to_state for move in told] == ["pending", "paid", "shipped"]


def test_on_move_is_a_decorator(orders, table):
    table["1"] = new_order()
    seen = []

    @orders.on_move
    def remember(move):
        seen.append(move.to_state)

    orders.start(1)

    assert seen == ["pending"]
    assert remember.__name__ == "remember"


def test_listeners_are_told_in_the_order_they_were_added(orders, table):
    table["1"] = new_order()
    calls = []
    orders.on_move(lambda move: calls.append("first"))
    orders.on_move(lambda move: calls.append("second"))

    orders.start(1)

    assert calls == ["first", "second"]


def test_a_step_that_says_not_now_tells_no_one(orders, table, told):
    table["1"] = new_order()
    orders.start(1)

    assert orders.fire(1, payment_cleared) is None
    assert len(told) == 1


def test_a_move_that_cant_be_made_tells_no_one(orders, table, told):
    table["1"] = new_order(tracking_number="1Z")
    orders.start(1)

    with pytest.raises(TransitionError):
        orders.fire(1, ship)
    assert len(told) == 1


def test_a_machine_with_no_listener_moves_as_before(orders, table):
    table["1"] = new_order(payment_cleared=True)
    orders.start(1)

    assert orders.fire(1, payment_cleared).to_state == "paid"


# ── When it is told ──


def test_a_listener_is_told_once_the_move_is_committed(orders, table):
    table["1"] = new_order(payment_cleared=True, tracking_number="1Z")
    seen = []

    @orders.on_move
    def ship_what_was_paid(move):
        seen.append(orders.state_of(move.entity))
        if move.to_state == Order.paid:
            orders.fire(move.entity, ship)

    orders.start(1)
    orders.fire(1, payment_cleared)

    assert seen == [Order.pending, Order.paid, Order.shipped]
    assert orders.state_of(1) is Order.shipped


def two_order_machine(backend, table, after_the_second=lambda: None):
    """A machine whose ``payment_cleared`` on order 1 also pays order 2, inside its own lock."""
    told, told_inside = [], []

    def payment_cleared(order):  # noqa: F811 — the row's step, under the name the rows use
        if order.pays_other:
            machine.fire(2, payment_cleared)
            told_inside.extend(told)
            after_the_second()
        return Order.paid

    rows = [(source, target, payment_cleared if step.__name__ == "payment_cleared" else step)
            for source, target, step in TRANSITIONS]
    machine = Machine(Order, rows, load=lambda order_id: table[order_id], backend=backend)
    table["1"] = new_order(pays_other=True)
    table["2"] = new_order(pays_other=False)
    machine.start_all([1, 2])
    machine.on_move(told.append)
    return machine, told, told_inside


def test_a_move_made_inside_another_lock_is_told_when_the_outer_one_ends(backend, table):
    machine, told, told_inside = two_order_machine(backend, table)

    machine.fire(1, "payment_cleared")

    assert told_inside == []
    assert [(move.entity, move.to_state) for move in told] == [("2", "paid"), ("1", "paid")]


@pytest.mark.parametrize("backend", UNDOES, indirect=True)
def test_a_move_that_is_undone_tells_no_one(backend, table):
    def fail():
        raise RuntimeError("the card was declined after all")

    machine, told, _ = two_order_machine(backend, table, after_the_second=fail)

    with pytest.raises(RuntimeError):
        machine.fire(1, "payment_cleared")

    assert told == []
    assert machine.state_of(2) is Order.pending


# ── A listener that raises ──


def test_a_listener_that_raises_is_logged_and_the_others_are_told(orders, table, caplog):
    table["1"] = new_order(payment_cleared=True)
    orders.start(1)
    seen = []

    @orders.on_move
    def broken(move):
        raise RuntimeError("the chat is down")

    orders.on_move(seen.append)

    with caplog.at_level(logging.ERROR, logger="agentic_fsm.machine"):
        move = orders.fire(1, payment_cleared)

    assert move.to_state == "paid"
    assert orders.state_of(1) is Order.paid
    assert seen == [move]
    assert "broken raised on order:1 reaching paid" in caplog.text
    assert "the chat is down" in caplog.text
