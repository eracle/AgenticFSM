"""``start``, ``start_all``, ``fire``, ``progress``, ``progress_all`` and ``due``, run against
every state backend."""

from datetime import UTC
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agentic_fsm import (
    AlreadyStarted,
    Decide,
    Decision,
    FinalState,
    InitialState,
    Machine,
    MachineError,
    ProgressError,
    State,
    States,
    Step,
    TransitionError,
    check,
    get_backend,
    is_check,
)
from agentic_fsm.backends.locmem.base import StateBackend as LocmemBackend

from .conftest import EDGES, Order, new_order, order_edges, order_machine, order_states


# ── start ──


def test_start_puts_an_entity_in_the_initial_state(orders):
    move = orders.start(1)
    assert (move.from_state, move.to_state) == (None, "pending")
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

    move = orders.fire(1)

    assert (move.from_state, move.to_state) == ("pending", "paid")
    assert move.decided_by == "code"
    assert move.at.tzinfo == UTC
    assert orders.state_of(1) is Order.paid
    assert orders.in_state(Order.pending) == []
    assert orders.history(1)[-1] == move


def test_fire_gives_the_step_the_entity_in_hand_without_loading_it(orders, table):
    table["1"] = new_order()
    orders.start(1)

    move = orders.fire(1, entity=new_order(payment_cleared=True))

    assert move.to_state == "paid"


def test_fire_loads_the_entity_when_none_is_given(orders, table):
    table["1"] = new_order()
    orders.start(1)
    assert orders.fire(1) is None


def test_a_step_that_returns_none_moves_and_logs_nothing(orders, table):
    table["1"] = new_order()
    orders.start(1)

    assert orders.fire(1) is None

    assert orders.state_of(1) is Order.pending
    assert len(orders.history(1)) == 1


def test_a_step_checks_in_the_order_its_body_is_written(orders, table):
    table["1"] = new_order(cancel_requested=True, payment_cleared=True)
    orders.start(1)

    assert orders.fire(1).to_state == "cancelled"


def test_fire_refuses_an_entity_that_has_not_started(orders, table):
    table["1"] = new_order()
    with pytest.raises(TransitionError, match="order:1 hasn't started"):
        orders.fire(1)


def test_fire_refuses_an_entity_in_a_final_state(orders, table):
    table["1"] = new_order(payment_cleared=True, tracking_number="TRK1", delivered=True)
    orders.start(1)
    assert [m.to_state for m in (orders.fire(1), orders.fire(1), orders.fire(1))] == ["paid", "shipped", "delivered"]

    with pytest.raises(TransitionError, match="order:1 is in delivered, a final state"):
        orders.fire(1)


def test_a_step_that_raises_leaves_no_trace(orders):
    orders.start(1)  # no row in the table, so load raises
    with pytest.raises(KeyError):
        orders.fire(1)
    assert orders.state_of(1) is Order.pending
    assert len(orders.history(1)) == 1


# ── A result the edges don't allow ──


def test_a_state_outside_the_states_edges_raises_and_writes_nothing(backend, table):
    def pending(order):  # pending has edges only to cancelled and paid
        return orders.states.shipped

    orders = order_machine(backend, table, pending=pending)
    table["1"] = new_order()
    orders.start(1)

    with pytest.raises(
        TransitionError, match="pending's step returned shipped, but pending has edges only to cancelled, paid"
    ):
        orders.fire(1)

    assert orders.state_of(1) is orders.states.pending
    assert len(orders.history(1)) == 1


@pytest.mark.parametrize("result", ["paid", 42, Order.paid])
def test_a_result_that_is_not_a_state_of_the_machine_raises_and_writes_nothing(backend, table, result):
    orders = order_machine(backend, table, pending=lambda order: result)
    table["1"] = new_order()
    orders.start(1)

    with pytest.raises(TransitionError, match="pending's step returned .*, which is not a state of Order"):
        orders.fire(1)

    assert orders.state_of(1) is orders.states.pending
    assert len(orders.history(1)) == 1


# ── A step that hands over ──


class Carrier(Step):
    """A step of the caller's own: the carrier's system says whether the parcel left."""

    decided_by = "carrier"

    def __init__(self):
        self.offered = []

    def run(self, entity, targets):
        self.offered.append(targets)
        return next(t for t in targets if t.value == "shipped") if entity.tracking_number else None


def handing_over(backend, table, carrier):
    def paid(order):
        if order.cancel_requested:
            return orders.states.cancelled
        return carrier

    orders = order_machine(backend, table, paid=paid)
    return orders


def test_a_function_may_hand_over_to_another_step_which_decides(backend, table):
    carrier = Carrier()
    orders = handing_over(backend, table, carrier)
    table["1"] = new_order(payment_cleared=True, tracking_number="TRK1")
    orders.start(1)
    orders.fire(1)

    move = orders.fire(1)

    assert (move.from_state, move.to_state, move.decided_by) == ("paid", "shipped", "carrier")
    assert carrier.offered == [(orders.states.cancelled, orders.states.shipped)]


def test_a_function_that_settles_the_move_itself_never_runs_the_step_it_could_hand_to(backend, table):
    carrier = Carrier()
    orders = handing_over(backend, table, carrier)
    table["1"] = new_order(payment_cleared=True)
    orders.start(1)
    orders.fire(1)
    table["1"].cancel_requested = True

    move = orders.fire(1)

    assert (move.to_state, move.decided_by) == ("cancelled", "code")
    assert carrier.offered == []


def test_a_step_handed_to_may_say_not_now(backend, table):
    orders = handing_over(backend, table, Carrier())
    table["1"] = new_order(payment_cleared=True)
    orders.start(1)
    orders.fire(1)

    assert orders.fire(1) is None
    assert orders.state_of(1) is orders.states.paid


def test_a_state_may_be_given_a_step_object_directly(backend, table):
    orders = order_machine(backend, table, paid=Carrier())
    table["1"] = new_order(payment_cleared=True, tracking_number="TRK1")
    orders.start(1)

    assert [(m.to_state, m.decided_by) for m in orders.progress(1)] == [("paid", "code"), ("shipped", "carrier")]


def test_a_steps_effect_runs_with_its_result_before_the_move_is_written(backend, table):
    seen = []
    carrier = Carrier()
    carrier.effect = lambda order, result: seen.append((order.tracking_number, result, orders.state_of(1)))
    orders = order_machine(backend, table, paid=carrier)
    table["1"] = new_order(payment_cleared=True, tracking_number="TRK1")
    orders.start(1)
    orders.fire(1)

    orders.fire(1)

    assert seen == [("TRK1", orders.states.shipped, orders.states.paid)]


def test_an_effect_that_raises_leaves_nothing_written(backend, table):
    def effect(order, result):
        raise RuntimeError("the label printer is off")

    carrier = Carrier()
    carrier.effect = effect
    orders = order_machine(backend, table, paid=carrier)
    table["1"] = new_order(payment_cleared=True, tracking_number="TRK1")
    orders.start(1)
    orders.fire(1)

    with pytest.raises(RuntimeError):
        orders.fire(1)

    assert orders.state_of(1) is orders.states.paid
    assert len(orders.history(1)) == 2


# ── The lock ──


def test_a_second_fire_waits_for_the_first_and_reads_the_state_it_left(table):
    """While pending's step runs in one thread, another fires the same order."""
    backend = get_backend("agentic_fsm.backends.locmem")
    step_running, may_finish = Event(), Event()

    def pending(order):
        step_running.set()
        may_finish.wait(timeout=5)
        return orders.states.paid

    orders = order_machine(backend, table, pending=pending)
    table["1"] = new_order(cancel_requested=True)
    orders.start(1)

    first = Thread(target=orders.fire, args=(1,))
    first.start()
    step_running.wait(timeout=5)
    second = Thread(target=orders.fire, args=(1,))
    second.start()
    second.join(timeout=0.2)
    assert second.is_alive(), "the second fire should wait on the first's lock"

    may_finish.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert [(m.from_state, m.to_state) for m in orders.history(1)] == [
        (None, "pending"),
        ("pending", "paid"),
        ("paid", "cancelled"),
    ]


def test_a_step_that_raises_rolls_back_what_it_wrote(backend, table):
    def pending(order):
        if order.cancel_requested:
            return orders.states.cancelled
        orders.fire(2)  # a write inside the lock, undone with the step
        raise RuntimeError("the payment provider is down")

    orders = order_machine(backend, table, pending=pending)
    table["1"], table["2"] = new_order(), new_order(cancel_requested=True)
    orders.start(1)
    orders.start(2)

    with pytest.raises(RuntimeError):
        orders.fire(1)

    assert orders.state_of(1) is orders.states.pending
    if isinstance(backend, LocmemBackend):
        return  # memory has no transaction to undo the nested move
    assert orders.state_of(2) is orders.states.pending


# ── progress_all ──


def test_progress_all_makes_one_move_per_entity(orders, table):
    table["1"] = new_order(payment_cleared=True, tracking_number="TRK1", delivered=True)
    orders.start(1)

    assert [m.to_state for m in orders.progress_all()] == ["paid"]
    assert [m.to_state for m in orders.progress_all()] == ["shipped"]
    assert [m.to_state for m in orders.progress_all()] == ["delivered"]
    assert orders.progress_all() == []
    assert orders.state_of(1) is Order.delivered


def test_progress_all_leaves_entities_with_nothing_to_do(orders, table):
    table["1"] = new_order()
    table["2"] = new_order(payment_cleared=True)
    orders.start(1)
    orders.start(2)

    moves = orders.progress_all()

    assert [(m.entity, m.to_state) for m in moves] == [("2", "paid")]
    assert orders.in_state(Order.pending) == ["1"]


def test_progress_all_never_loads_a_finished_entity(orders, table):
    table["1"] = new_order(cancel_requested=True)
    orders.start(1)
    orders.progress_all()
    del table["1"]  # loading it now would raise

    assert orders.progress_all() == []


def test_an_entity_that_raises_does_not_hold_the_others(backend, table):
    def pending(order):
        if order.payment_cleared == "provider down":
            raise RuntimeError("the payment provider is down")
        return orders.states.paid if order.payment_cleared else None

    orders = order_machine(backend, table, pending=pending)
    table["1"], table["2"], table["3"] = new_order(payment_cleared=True), new_order(
        payment_cleared="provider down"
    ), new_order(payment_cleared=True)
    for order_id in (1, 2, 3):
        orders.start(order_id)

    with pytest.raises(ProgressError) as raised:
        orders.progress_all()

    assert [(m.entity, m.to_state) for m in raised.value.moves] == [("1", "paid"), ("3", "paid")]
    assert list(raised.value.failures) == ["2"]
    assert isinstance(raised.value.failures["2"], RuntimeError)
    assert "2: RuntimeError: the payment provider is down" in str(raised.value)
    assert orders.in_state(orders.states.paid) == ["1", "3"]
    assert orders.in_state(orders.states.pending) == ["2"]


def test_an_entity_in_a_state_the_machine_no_longer_has_is_a_failure(orders, backend, table):
    table["1"] = new_order(payment_cleared=True)
    orders.start(1)
    backend.start("order", "2", "on_hold")

    with pytest.raises(ProgressError) as raised:
        orders.progress_all()

    assert [m.entity for m in raised.value.moves] == ["1"]
    assert isinstance(raised.value.failures["2"], TransitionError)


# ── start_all ──


def test_start_all_starts_only_the_entities_with_no_state(orders):
    orders.start(2)

    moves = orders.start_all([1, 2, 3, 1])

    assert [(m.entity, m.to_state) for m in moves] == [("1", "pending"), ("3", "pending")]
    assert orders.in_state(Order.pending) == ["2", "1", "3"]
    assert len(orders.history(2)) == 1


def test_start_all_leaves_a_moved_entity_where_it_is(orders, table):
    table["1"] = new_order(payment_cleared=True)
    orders.start(1)
    orders.fire(1)

    assert orders.start_all([1]) == []
    assert orders.state_of(1) is Order.paid


def test_start_all_skips_an_entity_another_caller_started_first(orders, backend, monkeypatch):
    real_all_current = backend.all_current

    def all_current_then_a_rival_starts(machine):
        found = real_all_current(machine)
        backend.start("order", "1", "pending")  # between the read and our start
        return found

    monkeypatch.setattr(backend, "all_current", all_current_then_a_rival_starts)

    assert [m.entity for m in orders.start_all([1, 2])] == ["2"]
    assert len(orders.history(1)) == 1


# ── progress ──


def test_progress_moves_one_entity_until_it_rests(orders, table):
    table["1"] = new_order(payment_cleared=True, tracking_number="TRK1", delivered=True)
    table["2"] = new_order(payment_cleared=True)
    orders.start(1)
    orders.start(2)

    moves = orders.progress(1)

    assert [(m.from_state, m.to_state) for m in moves] == [
        ("pending", "paid"),
        ("paid", "shipped"),
        ("shipped", "delivered"),
    ]
    assert orders.state_of(2) is Order.pending


def test_progress_rests_where_the_step_says_not_now(orders, table):
    table["1"] = new_order(payment_cleared=True)
    orders.start(1)

    assert [m.to_state for m in orders.progress(1)] == ["paid"]
    assert orders.progress(1) == []
    assert orders.state_of(1) is Order.paid


def test_progress_loads_the_entity_afresh_after_each_move(orders, table):
    loads = []
    order = new_order(payment_cleared=True, tracking_number="TRK1")
    orders.load = lambda order_id: loads.append(order_id) or order
    orders.start(1)

    orders.progress(1)

    assert loads == ["1", "1", "1"]  # pending, paid, then shipped where nothing moves it


def test_progress_stops_when_it_comes_back_to_a_state_it_left(backend, table):
    def flip(order):  # a step that would bounce the order between two states for ever
        order.flips += 1
        return Shop.paid if order.flips % 2 else Shop.pending

    Shop = order_states(pending=flip, paid=flip)
    orders = Machine(
        Shop, [*order_edges(Shop), (Shop.paid, Shop.pending)], load=lambda order_id: table[order_id], backend=backend
    )
    table["1"] = SimpleNamespace(**vars(new_order()), flips=0)
    orders.start(1)

    moves = orders.progress(1)

    assert [(m.from_state, m.to_state) for m in moves] == [("pending", "paid"), ("paid", "pending")]


def test_progress_refuses_an_entity_that_has_not_started(orders):
    with pytest.raises(TransitionError, match="order:1 hasn't started"):
        orders.progress(1)


# ── Machines and stored state ──


def test_two_machines_share_a_backend_without_seeing_each_other(backend, table):
    orders = Machine(Order, EDGES, load=table.get, backend=backend)
    returns = Machine(Order, EDGES, load=table.get, backend=backend, name="return")
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


# ── due ──


def checked_orders(backend, table, **steps):
    """An order machine whose pending and shipped steps only read, and whose paid step charges."""

    @check
    def pending(order):
        if order.cancel_requested:
            return orders.states.cancelled
        return orders.states.paid if order.payment_cleared else None

    def charge_and_ship(order):
        """An outside effect: due can't try it."""
        order.charged = True
        return orders.states.shipped

    @check
    def shipped(order):
        return orders.states.delivered if order.delivered else None

    orders = order_machine(backend, table, **{"pending": pending, "paid": charge_and_ship, "shipped": shipped, **steps})
    return orders


def test_due_runs_the_checks_and_moves_nothing(backend, table):
    orders = checked_orders(backend, table)
    table["1"] = new_order(payment_cleared=True)
    table["2"] = new_order()
    table["3"] = new_order(cancel_requested=True)
    orders.start_all([1, 2, 3])

    assert orders.due() == ["1", "3"]
    assert orders.in_state(orders.states.pending) == ["1", "2", "3"]
    assert [len(orders.history(order_id)) for order_id in (1, 2, 3)] == [1, 1, 1]


def test_an_entity_whose_step_is_not_a_check_is_due_untried(backend, table):
    orders = checked_orders(backend, table)
    table["1"] = new_order(payment_cleared=True)
    orders.start(1)
    orders.fire(1)

    assert orders.due() == ["1"]
    assert not hasattr(table["1"], "charged")


def test_a_check_that_hands_over_to_a_step_that_is_not_one_is_due_without_running_it(backend, table):
    carrier = Carrier()

    @check
    def paid(order):
        return carrier if order.tracking_number else None

    orders = checked_orders(backend, table, paid=paid)
    table["1"], table["2"] = new_order(payment_cleared=True), new_order(payment_cleared=True, tracking_number="TRK1")
    orders.start_all([1, 2])
    orders.progress_all()

    assert orders.due() == ["2"]
    assert carrier.offered == []


def test_a_check_that_hands_over_to_a_step_that_only_reads_has_it_run(backend, table):
    carrier = Carrier()
    carrier.reads_only = True

    @check
    def paid(order):
        return carrier

    orders = checked_orders(backend, table, paid=paid)
    table["1"], table["2"] = new_order(payment_cleared=True), new_order(payment_cleared=True, tracking_number="TRK1")
    orders.start_all([1, 2])
    orders.progress_all()

    assert orders.due() == ["2"]
    assert len(carrier.offered) == 2
    assert orders.in_state(orders.states.paid) == ["1", "2"]


def test_a_finished_entity_is_never_due_nor_loaded(backend, table):
    orders = checked_orders(backend, table)
    table["1"] = new_order(cancel_requested=True)
    orders.start(1)
    orders.fire(1)
    del table["1"]  # loading it now would raise

    assert orders.due() == []


# ── tags ──


@check
def await_payment(parcel):
    return Parcel.packing if parcel.payment_cleared else None


def pack(parcel):
    return Parcel.labelling


def label(parcel):
    return Parcel.shipped


@check
def await_delivery(parcel):
    return Parcel.delivered if parcel.delivered else None


class Parcel(States):
    pending = InitialState("Placed, not paid yet", step=await_payment)
    packing = State("Paid: the warehouse packs it", step=pack, tags={"warehouse"})
    labelling = State("Packed: the warehouse labels it", step=label, tags={"warehouse", "printer"})
    shipped = State("Handed to the carrier", step=await_delivery, tags={"carrier"})
    delivered = FinalState("The carrier delivered it")


PARCEL_EDGES = [
    (Parcel.pending, Parcel.packing),
    (Parcel.packing, Parcel.labelling),
    (Parcel.labelling, Parcel.shipped),
    (Parcel.shipped, Parcel.delivered),
]


def parcels_in_every_state(backend, table):
    """Parcels 1 to 4, one in each unfinished state, each with a move it could make."""
    parcels = Machine(Parcel, PARCEL_EDGES, load=lambda parcel_id: table[parcel_id], backend=backend)
    for parcel_id in "1234":
        table[parcel_id] = new_order(payment_cleared=True, delivered=True)
    parcels.start_all("1234")
    for parcel_id, moves in (("2", 1), ("3", 2), ("4", 3)):
        for _ in range(moves):
            parcels.fire(parcel_id)
    return parcels


def test_progress_all_with_a_tag_moves_only_the_entities_in_its_states(backend, table):
    parcels = parcels_in_every_state(backend, table)

    moves = parcels.progress_all("warehouse")

    assert [(m.entity, m.to_state) for m in moves] == [("2", "labelling"), ("3", "shipped")]
    assert parcels.state_of(1) is Parcel.pending
    assert parcels.state_of(4) is Parcel.shipped


def test_a_state_may_carry_several_tags(backend, table):
    parcels = parcels_in_every_state(backend, table)

    assert [m.entity for m in parcels.progress_all("printer")] == ["3"]


def test_due_with_a_tag_lists_only_the_entities_in_its_states(backend, table):
    parcels = parcels_in_every_state(backend, table)

    assert parcels.due() == ["1", "2", "3", "4"]
    assert parcels.due("carrier") == ["4"]
    assert parcels.due("warehouse") == ["2", "3"]


def test_a_limit_stops_progress_all_after_that_many_moves_oldest_first(backend, table):
    parcels = parcels_in_every_state(backend, table)

    assert [m.entity for m in parcels.progress_all("warehouse", limit=1)] == ["2"]
    assert parcels.state_of(3) is Parcel.labelling
    assert [m.entity for m in parcels.progress_all(limit=2)] == ["1", "2"]


def test_an_entity_that_does_not_move_does_not_count_toward_the_limit(backend, table):
    parcels = parcels_in_every_state(backend, table)
    table["1"].payment_cleared = False  # "not now"
    del table["2"]  # loading it raises

    with pytest.raises(ProgressError) as raised:
        parcels.progress_all(limit=1)

    assert [m.entity for m in raised.value.moves] == ["3"]
    assert list(raised.value.failures) == ["2"]
    assert parcels.state_of(4) is Parcel.shipped


@pytest.mark.parametrize("limit", [0, -1])
def test_a_limit_below_one_is_refused(orders, limit):
    with pytest.raises(ValueError, match="limit must be at least 1"):
        orders.progress_all(limit=limit)


@pytest.mark.parametrize("call", ["progress_all", "due"])
def test_a_tag_no_state_carries_is_refused(backend, table, call):
    parcels = parcels_in_every_state(backend, table)

    with pytest.raises(MachineError, match="no state of parcel is tagged 'wharehouse'; its tags are carrier"):
        getattr(parcels, call)("wharehouse")
    assert parcels.state_of(2) is Parcel.packing


def test_check_marks_a_function_and_refuses_a_step_object():
    assert is_check(await_payment)
    assert not is_check(pack)
    assert Parcel.pending.step.reads_only
    assert not Parcel.packing.step.reads_only
    with pytest.raises(TypeError, match="can't be a check"):
        check(Decide(prompt=lambda order: "", schema=Decision))
