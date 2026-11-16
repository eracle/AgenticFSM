"""``start``, ``fire`` and ``advance``, run against every state backend."""

from datetime import UTC

import pytest

from agentic_fsm import AlreadyStarted, Machine, TransitionError

from .conftest import TRANSITIONS, Order, cancel, carrier_delivered, new_order, payment_cleared, ship


# ── start ──


def test_start_puts_an_entity_in_the_initial_state(orders):
    move = orders.start(1)
    assert (move.from_state, move.to_state, move.applied, move.step) == (None, "pending", True, None)
    assert orders.state_of(1) is Order.pending
    assert orders.in_state(Order.pending) == ["1"]
    assert orders.history(1) == [move]


def test_start_twice_is_refused(orders):
    orders.start(1)
    with pytest.raises(AlreadyStarted, match="order:1"):
        orders.start(1)
    assert len(orders.history(1)) == 1


def test_an_entity_that_never_started_has_no_state(orders):
    assert orders.state_of(1) is None
    assert orders.history(1) == []


# ── fire ──


def test_fire_moves_the_entity_and_logs_it(orders, table):
    table["1"] = new_order(payment_cleared=True)
    orders.start(1)

    move = orders.fire(1, payment_cleared)

    assert (move.step, move.from_state, move.to_state) == ("payment_cleared", "pending", "paid")
    assert move.applied and move.decided_by == "code" and move.reason is None
    assert move.at.tzinfo == UTC
    assert orders.state_of(1) is Order.paid
    assert orders.in_state(Order.pending) == []
    assert orders.history(1)[-1] == move


def test_fire_takes_a_step_by_name(orders, table):
    table["1"] = new_order(payment_cleared=True)
    orders.start(1)
    assert orders.fire(1, "payment_cleared").to_state == "paid"


def test_fire_gives_the_step_the_entity_in_hand_without_loading_it(orders, table):
    table["1"] = new_order()
    orders.start(1)

    move = orders.fire(1, payment_cleared, entity=new_order(payment_cleared=True))

    assert move.to_state == "paid" and move.applied


def test_fire_loads_the_entity_when_none_is_given(orders, table):
    table["1"] = new_order()
    orders.start(1)
    assert orders.fire(1, payment_cleared) is None


def test_a_step_that_returns_none_moves_and_logs_nothing(orders, table):
    table["1"] = new_order()
    orders.start(1)

    assert orders.fire(1, payment_cleared) is None

    assert orders.state_of(1) is Order.pending
    assert len(orders.history(1)) == 1


def test_fire_refuses_a_step_that_does_not_leave_the_state(orders, table):
    table["1"] = new_order(tracking_number="TRK1")
    orders.start(1)
    with pytest.raises(TransitionError, match="ship doesn't leave pending"):
        orders.fire(1, ship)


def test_fire_refuses_an_entity_that_has_not_started(orders, table):
    table["1"] = new_order()
    with pytest.raises(TransitionError, match="order:1 hasn't started"):
        orders.fire(1, payment_cleared)


def test_fire_refuses_a_step_the_machine_does_not_have(orders):
    def refund(order):
        return Order.cancelled

    orders.start(1)
    with pytest.raises(TransitionError, match="'refund' is not a step of order"):
        orders.fire(1, refund)
    with pytest.raises(TransitionError, match="'refund' is not a step of order"):
        orders.fire(1, "refund")


def test_a_step_that_raises_leaves_no_trace(orders):
    orders.start(1)  # no row in the table, so load raises
    with pytest.raises(KeyError):
        orders.fire(1, payment_cleared)
    assert orders.state_of(1) is Order.pending
    assert len(orders.history(1)) == 1


# ── Refusals ──


def machine_with(backend, table, step, rows):
    return Machine(Order, rows, load=lambda order_id: table[order_id], backend=backend)


def test_a_state_outside_the_steps_rows_is_refused_and_logged(backend, table):
    def payment_cleared(order):  # may only move pending -> paid
        return Order.shipped

    rows = [(Order.pending, Order.paid, payment_cleared), *TRANSITIONS[2:], TRANSITIONS[0]]
    orders = machine_with(backend, table, payment_cleared, rows)
    table["1"] = new_order()
    orders.start(1)

    move = orders.fire(1, payment_cleared)

    assert not move.applied
    assert (move.from_state, move.to_state) == ("pending", "shipped")
    assert move.reason == "payment_cleared may move pending only to paid"
    assert orders.state_of(1) is Order.pending
    assert orders.history(1)[-1] == move


@pytest.mark.parametrize("result", ["paid", 42])
def test_a_result_that_is_not_a_state_is_refused_and_logged(backend, table, result):
    def payment_cleared(order):
        return result

    rows = [(Order.pending, Order.paid, payment_cleared), *TRANSITIONS[2:], TRANSITIONS[0]]
    orders = machine_with(backend, table, payment_cleared, rows)
    table["1"] = new_order()
    orders.start(1)

    move = orders.fire(1, payment_cleared)

    assert not move.applied
    assert move.to_state == str(result)
    assert move.reason == f"{result!r} is not a state of Order"
    assert orders.state_of(1) is Order.pending


def test_a_move_that_loses_a_race_is_refused_and_logged(backend, table):
    """While payment_cleared runs, someone else cancels the order."""

    def payment_cleared(order):
        orders.fire(1, cancel)
        return Order.paid

    rows = [TRANSITIONS[0], (Order.pending, Order.paid, payment_cleared), *TRANSITIONS[2:]]
    orders = machine_with(backend, table, payment_cleared, rows)
    table["1"] = new_order(cancel_requested=True)
    orders.start(1)

    move = orders.fire(1, payment_cleared)

    assert not move.applied
    assert move.reason == "moved by someone else since it was read in pending"
    assert orders.state_of(1) is Order.cancelled
    assert [(m.to_state, m.applied) for m in orders.history(1)] == [
        ("pending", True),
        ("cancelled", True),
        ("paid", False),
    ]


# ── advance ──


def test_advance_makes_one_move_per_entity(orders, table):
    table["1"] = new_order(payment_cleared=True, tracking_number="TRK1", delivered=True)
    orders.start(1)

    assert [m.to_state for m in orders.advance()] == ["paid"]
    assert [m.to_state for m in orders.advance()] == ["shipped"]
    assert [m.to_state for m in orders.advance()] == ["delivered"]
    assert orders.advance() == []
    assert orders.state_of(1) is Order.delivered


def test_advance_tries_steps_in_list_order(orders, table):
    table["1"] = new_order(cancel_requested=True, payment_cleared=True)
    orders.start(1)

    assert [(m.step, m.to_state) for m in orders.advance()] == [("cancel", "cancelled")]


def test_advance_leaves_entities_with_nothing_to_do(orders, table):
    table["1"] = new_order()
    table["2"] = new_order(payment_cleared=True)
    orders.start(1)
    orders.start(2)

    moves = orders.advance()

    assert [(m.entity, m.to_state) for m in moves] == [("2", "paid")]
    assert orders.in_state(Order.pending) == ["1"]


def test_advance_never_loads_a_finished_entity(orders, table):
    table["1"] = new_order(cancel_requested=True)
    orders.start(1)
    orders.advance()
    del table["1"]  # loading it now would raise

    assert orders.advance() == []


# ── Machines and stored state ──


def test_two_machines_share_a_backend_without_seeing_each_other(backend, table):
    orders = Machine(Order, TRANSITIONS, load=table.get, backend=backend)
    returns = Machine(Order, TRANSITIONS, load=table.get, backend=backend, name="return")
    orders.start(1)

    assert returns.state_of(1) is None
    assert returns.in_state(Order.pending) == []
    returns.start(1)
    assert len(orders.history(1)) == 1


def test_a_stored_state_the_machine_no_longer_has_is_reported(orders, backend):
    backend.start("order", "1", "on_hold")
    with pytest.raises(TransitionError, match="order:1 is stored in 'on_hold'"):
        orders.state_of(1)


def test_in_state_refuses_another_machines_state(orders):
    with pytest.raises(TransitionError, match="is not a state of Order"):
        orders.in_state("pending")


def test_carrier_delivery_finishes_the_order(orders, table):
    table["1"] = new_order(payment_cleared=True, tracking_number="TRK1", delivered=True)
    orders.start(1)
    orders.fire(1, payment_cleared)
    orders.fire(1, ship)

    assert orders.fire(1, carrier_delivered).to_state == "delivered"
    with pytest.raises(TransitionError, match="carrier_delivered doesn't leave delivered"):
        orders.fire(1, carrier_delivered)
