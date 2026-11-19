"""An example order machine. Its orders sit in a dict standing in for the inheritor's table,
and every test that takes ``orders`` runs once per state backend."""

from types import SimpleNamespace

import pytest

from agentic_fsm import FinalState, InitialState, Machine, State, States, get_backend


def order_states(**handlers):
    """The order's states. A test replaces a state's handler by naming the state: ``paid=my_handler``.

    Each call makes a class of its own, so a replaced handler returns the states of the class it
    was built into (``orders.states.paid``), not this module's ``Order``.
    """

    def await_payment(order):
        if order.cancel_requested:
            return Order.cancelled
        return Order.paid if order.payment_cleared else None

    def pack(order):
        if order.cancel_requested:
            return Order.cancelled
        return Order.shipped if order.tracking_number else None

    def await_delivery(order):
        return Order.delivered if order.delivered else None

    class Order(States):
        pending = InitialState("Placed, not paid yet", handler=handlers.get("pending", await_payment))
        paid = State("Payment cleared, waiting to ship", handler=handlers.get("paid", pack))
        shipped = State("Handed to the carrier", handler=handlers.get("shipped", await_delivery))
        delivered = FinalState("The carrier delivered it")
        cancelled = FinalState("Cancelled before it shipped")

    return Order


def order_edges(states):
    # fmt: off
    return [
        # from                           to
        ((states.pending, states.paid),  states.cancelled),
        (states.pending,                 states.paid),
        (states.paid,                    states.shipped),
        (states.shipped,                 states.delivered),
    ]
    # fmt: on


Order = order_states()
EDGES = order_edges(Order)


def order_machine(backend, table, **handlers):
    """An order machine on ``backend`` with the given states' handlers replaced."""
    states = order_states(**handlers)
    return Machine(states, order_edges(states), load=lambda order_id: table[order_id], backend=backend)


def new_order(**facts):
    defaults = dict(cancel_requested=False, payment_cleared=False, tracking_number=None, delivered=False)
    return SimpleNamespace(**{**defaults, **facts})


@pytest.fixture(params=["locmem", pytest.param("django", marks=pytest.mark.django_db), "sqlalchemy"])
def backend(request):
    return backend_named(request.param)


def backend_named(name):
    """A fresh backend: ``locmem``, ``django`` or ``sqlalchemy``."""
    if name == "sqlalchemy":
        return sqlalchemy_backend()
    return get_backend(f"agentic_fsm.backends.{name}")


def sqlalchemy_backend():
    """A sqlalchemy backend on a fresh in-memory SQLite database, its tables created.

    One connection is shared by the whole test (``StaticPool``), and ``autocommit=False`` lets
    Python's sqlite3 run the savepoints a nested lock opens.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from agentic_fsm.backends.sqlalchemy.base import StateBackend

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"autocommit": False})
    backend = StateBackend(engine)
    backend.create_tables()
    return backend


@pytest.fixture
def table():
    """The inheritor's own rows: order id -> order."""
    return {}


@pytest.fixture
def orders(backend, table):
    return Machine(Order, EDGES, load=lambda order_id: table[order_id], backend=backend)
