"""What only the sqlalchemy backend does: a step's own writes in the move's transaction, the
tables Django's models also describe, the ``SQLALCHEMY_URL`` setting, and the row lock on
Postgres when ``AGENTIC_FSM_TEST_POSTGRES_URL`` names one to test against."""

import os
from threading import Event, Thread

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, insert, select

from agentic_fsm import Machine, get_backend
from agentic_fsm.backends.django.models import CurrentState, LogEntry
from agentic_fsm.backends.sqlalchemy.base import StateBackend
from agentic_fsm.backends.sqlalchemy.tables import current_state, log_entry, metadata
from agentic_fsm.conf import settings

from .conftest import TRANSITIONS, Order, cancel, new_order, sqlalchemy_backend

# The inheritor's own table, which a step writes to.
receipts_metadata = MetaData()
receipts = Table(
    "receipts",
    receipts_metadata,
    Column("id", Integer, primary_key=True),
    Column("order_id", String(255)),
)


def machine_writing_receipts(backend, table, fail=False):
    """An order machine whose payment step writes a receipt through the backend's connection."""

    def payment_cleared(order):
        backend.connection().execute(insert(receipts).values(order_id=order.id))
        if fail:
            raise RuntimeError("the payment provider is down")
        return Order.paid

    rows = [TRANSITIONS[0], (Order.pending, Order.paid, payment_cleared), *TRANSITIONS[2:]]
    receipts_metadata.create_all(backend.engine)
    return Machine(Order, rows, load=lambda order_id: table[order_id], backend=backend), payment_cleared


def receipt_ids(backend):
    with backend.engine.connect() as connection:
        return list(connection.execute(select(receipts.c.order_id)).scalars())


# ── A step's own writes ──


def test_a_steps_writes_commit_with_the_move(table):
    backend = sqlalchemy_backend()
    orders, payment_cleared = machine_writing_receipts(backend, table)
    table["1"] = new_order(id="1")
    orders.start(1)

    orders.fire(1, payment_cleared)

    assert orders.state_of(1) is Order.paid
    assert receipt_ids(backend) == ["1"]


def test_a_step_that_raises_rolls_back_its_writes_and_the_move(table):
    backend = sqlalchemy_backend()
    orders, payment_cleared = machine_writing_receipts(backend, table, fail=True)
    table["1"] = new_order(id="1")
    orders.start(1)

    with pytest.raises(RuntimeError):
        orders.fire(1, payment_cleared)

    assert orders.state_of(1) is Order.pending
    assert receipt_ids(backend) == []


def test_connection_outside_a_lock_is_refused():
    with pytest.raises(RuntimeError, match="only open inside a lock"):
        sqlalchemy_backend().connection()


def test_a_read_inside_a_lock_sees_the_locks_own_writes(table):
    backend = sqlalchemy_backend()
    seen = []

    def payment_cleared(order):
        orders.fire(2, cancel)
        seen.append(orders.state_of(2))
        return Order.paid

    rows = [TRANSITIONS[0], (Order.pending, Order.paid, payment_cleared), *TRANSITIONS[2:]]
    orders = Machine(Order, rows, load=lambda order_id: table[order_id], backend=backend)
    table["1"], table["2"] = new_order(), new_order(cancel_requested=True)
    orders.start(1)
    orders.start(2)

    orders.fire(1, payment_cleared)

    assert seen == [Order.cancelled]


# ── The tables ──


@pytest.mark.parametrize("table, model", [(log_entry, LogEntry), (current_state, CurrentState)])
def test_the_tables_are_the_django_backends(table, model):
    assert table.name == model._meta.db_table
    assert set(table.c.keys()) == {field.column for field in model._meta.concrete_fields}
    assert {column.name for column in table.c if column.nullable} == {
        field.column for field in model._meta.concrete_fields if field.null
    }
    names = {index.name for index in table.indexes} | {
        constraint.name for constraint in table.constraints if constraint.name
    }
    assert names >= {index.name for index in model._meta.indexes} | {
        constraint.name for constraint in model._meta.constraints
    }


def test_create_tables_leaves_existing_tables_and_rows(table):
    backend = sqlalchemy_backend()
    orders = Machine(Order, TRANSITIONS, load=table.get, backend=backend)
    orders.start(1)

    backend.create_tables()

    assert orders.state_of(1) is Order.pending


# ── Settings ──


def test_the_backend_connects_to_the_sqlalchemy_url_setting(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'state.sqlite3'}"
    monkeypatch.setenv("AGENTIC_FSM_SQLALCHEMY_URL", url)

    backend = get_backend("agentic_fsm.backends.sqlalchemy")
    backend.create_tables()
    Machine(Order, TRANSITIONS, load=dict().get, backend=backend).start(1)

    assert str(backend.engine.url) == url
    assert get_backend("agentic_fsm.backends.sqlalchemy").engine is backend.engine
    with create_engine(url).connect() as connection:
        assert connection.execute(select(current_state.c.state)).scalars().all() == ["pending"]


def test_the_backend_without_an_engine_or_url_is_refused(monkeypatch):
    monkeypatch.delenv("AGENTIC_FSM_SQLALCHEMY_URL", raising=False)
    assert settings.SQLALCHEMY_URL is None

    with pytest.raises(LookupError, match="AGENTIC_FSM_SQLALCHEMY_URL"):
        StateBackend()


# ── The row lock, on Postgres ──

POSTGRES_URL = os.environ.get("AGENTIC_FSM_TEST_POSTGRES_URL")


@pytest.fixture
def postgres_backend():
    if not POSTGRES_URL:
        pytest.skip("set AGENTIC_FSM_TEST_POSTGRES_URL to test the row lock on Postgres")
    engine = create_engine(POSTGRES_URL)
    metadata.drop_all(engine)
    backend = StateBackend(engine)
    backend.create_tables()
    yield backend
    metadata.drop_all(engine)
    engine.dispose()


def test_a_second_fire_waits_for_the_first_on_postgres(postgres_backend, table):
    """While payment_cleared runs in one thread, another tries to cancel the same order."""
    step_running, may_finish = Event(), Event()

    def payment_cleared(order):
        step_running.set()
        may_finish.wait(timeout=5)
        return Order.paid

    rows = [TRANSITIONS[0], (Order.pending, Order.paid, payment_cleared), *TRANSITIONS[2:]]
    orders = Machine(Order, rows, load=lambda order_id: table[order_id], backend=postgres_backend)
    table["1"] = new_order(cancel_requested=True)
    orders.start(1)

    first = Thread(target=orders.fire, args=(1, payment_cleared))
    first.start()
    step_running.wait(timeout=5)
    second = Thread(target=lambda: orders.fire(1, cancel))
    second.start()
    second.join(timeout=0.5)
    assert second.is_alive(), "the second fire should wait on the first's row lock"

    may_finish.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert [(m.from_state, m.to_state) for m in orders.history(1)] == [
        (None, "pending"),
        ("pending", "paid"),
        ("paid", "cancelled"),
    ]
