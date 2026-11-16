"""An example order machine. Its orders sit in a dict standing in for the inheritor's table,
and every test that takes ``orders`` runs once per state backend."""

from types import SimpleNamespace

import pytest

from agentic_fsm import FinalState, InitialState, Machine, State, States, get_backend


class Order(States):
    pending = InitialState("Placed, not paid yet")
    paid = State("Payment cleared, waiting to ship")
    shipped = State("Handed to the carrier")
    delivered = FinalState("The carrier delivered it")
    cancelled = FinalState("Cancelled before it shipped")


def cancel(order):
    return Order.cancelled if order.cancel_requested else None


def payment_cleared(order):
    return Order.paid if order.payment_cleared else None


def ship(order):
    return Order.shipped if order.tracking_number else None


def carrier_delivered(order):
    return Order.delivered if order.delivered else None


# fmt: off
TRANSITIONS = [
    # from                         to                step
    ((Order.pending, Order.paid),  Order.cancelled,  cancel),
    (Order.pending,                Order.paid,       payment_cleared),
    (Order.paid,                   Order.shipped,    ship),
    (Order.shipped,                Order.delivered,  carrier_delivered),
]
# fmt: on


def new_order(**facts):
    defaults = dict(cancel_requested=False, payment_cleared=False, tracking_number=None, delivered=False)
    return SimpleNamespace(**{**defaults, **facts})


@pytest.fixture(params=["locmem", pytest.param("django", marks=pytest.mark.django_db)])
def backend(request):
    return get_backend(f"agentic_fsm.backends.{request.param}")


@pytest.fixture
def table():
    """The inheritor's own rows: order id -> order."""
    return {}


@pytest.fixture
def orders(backend, table):
    return Machine(Order, TRANSITIONS, load=lambda order_id: table[order_id], backend=backend)
