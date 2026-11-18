"""What only the sqlalchemy backend does: a step's own writes in the move's transaction, the
tables Django's models also describe, the ``SQLALCHEMY_URL`` setting, and the row lock on
Postgres when ``AGENTIC_FSM_TEST_POSTGRES_URL`` names one to test against."""

import os
from datetime import UTC, datetime
from threading import Event, Thread

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, insert, select

from agentic_fsm import Machine, get_backend
from agentic_fsm.backends.base.base import Move
from agentic_fsm.backends.django.models import CurrentState, LogEntry
from agentic_fsm.backends.sqlalchemy.base import StateBackend
from agentic_fsm.backends.sqlalchemy.tables import current_state, log_entry, metadata
from agentic_fsm.conf import settings

from .conftest import EDGES, Order, new_order, order_machine, sqlalchemy_backend

# The one instant both backends log their moves at, so the rows they store can be compared.
MOVED_AT = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
ON_THE_SECOND = MOVED_AT.replace(microsecond=0)

# The inheritor's own table, which a step writes to.
receipts_metadata = MetaData()
receipts = Table(
    "receipts",
    receipts_metadata,
    Column("id", Integer, primary_key=True),
    Column("order_id", String(255)),
)


def machine_writing_receipts(backend, table, fail=False):
    """An order machine whose pending step writes a receipt through the backend's connection."""

    def pending(order):
        backend.connection().execute(insert(receipts).values(order_id=order.id))
        if fail:
            raise RuntimeError("the payment provider is down")
        return orders.states.paid

    receipts_metadata.create_all(backend.engine)
    orders = order_machine(backend, table, pending=pending)
    return orders


def receipt_ids(backend):
    with backend.engine.connect() as connection:
        return list(connection.execute(select(receipts.c.order_id)).scalars())


# ── A step's own writes ──


def test_a_steps_writes_commit_with_the_move(table):
    backend = sqlalchemy_backend()
    orders = machine_writing_receipts(backend, table)
    table["1"] = new_order(id="1")
    orders.start(1)

    orders.fire(1)

    assert orders.state_of(1) is orders.states.paid
    assert receipt_ids(backend) == ["1"]


def test_a_step_that_raises_rolls_back_its_writes_and_the_move(table):
    backend = sqlalchemy_backend()
    orders = machine_writing_receipts(backend, table, fail=True)
    table["1"] = new_order(id="1")
    orders.start(1)

    with pytest.raises(RuntimeError):
        orders.fire(1)

    assert orders.state_of(1) is orders.states.pending
    assert receipt_ids(backend) == []


def test_connection_outside_a_lock_is_refused():
    with pytest.raises(RuntimeError, match="only open inside a lock"):
        sqlalchemy_backend().connection()


def test_a_read_inside_a_lock_sees_the_locks_own_writes(table):
    backend = sqlalchemy_backend()
    seen = []

    def pending(order):
        if order.cancel_requested:
            return orders.states.cancelled
        orders.fire(2)
        seen.append(orders.state_of(2))
        return orders.states.paid

    orders = order_machine(backend, table, pending=pending)
    table["1"], table["2"] = new_order(), new_order(cancel_requested=True)
    orders.start(1)
    orders.start(2)

    orders.fire(1)

    assert seen == [orders.states.cancelled]


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


STORED_LOG = (
    "SELECT machine, entity, from_state, to_state, decided_by, decision, CAST(at AS TEXT) "
    "FROM agentic_fsm_logentry ORDER BY id"
)
STORED_STATE = (
    "SELECT machine, entity, state, CAST(updated_at AS TEXT) FROM agentic_fsm_currentstate ORDER BY id"
)


def write_an_orders_moves(backend, at=MOVED_AT):
    """A start, a move with a decision and a move with none, through the backend's own calls."""
    backend.start("order", "1", "pending")
    for to_state, decision in (("paid", {"to": "paid", "note": "cleared"}), ("shipped", None)):
        with backend.lock("order", "1") as current:
            backend.move(Move("order", "1", current.state, to_state, "code", at, decision))


@pytest.mark.django_db
@pytest.mark.parametrize("at", [MOVED_AT, ON_THE_SECOND])
def test_both_backends_store_the_same_rows(monkeypatch, at):
    """Row for row and value for value as SQLite keeps them, so a database written through
    one backend reads the same through the other."""
    from django.db import connection

    for module in ("django", "sqlalchemy"):
        monkeypatch.setattr(f"agentic_fsm.backends.{module}.base.utc_now", lambda: at)
    through_sqlalchemy = sqlalchemy_backend()
    write_an_orders_moves(through_sqlalchemy, at)
    write_an_orders_moves(get_backend("agentic_fsm.backends.django"), at)

    for query in (STORED_LOG, STORED_STATE):
        with connection.cursor() as cursor:
            by_django = [tuple(row) for row in cursor.execute(query).fetchall()]
        with through_sqlalchemy.engine.connect() as raw:
            by_sqlalchemy = [tuple(row) for row in raw.exec_driver_sql(query)]
        assert by_sqlalchemy == by_django


def test_a_move_with_no_decision_stores_null():
    backend = sqlalchemy_backend()
    write_an_orders_moves(backend)

    with backend.engine.connect() as raw:
        stored = raw.exec_driver_sql("SELECT decision IS NULL FROM agentic_fsm_logentry ORDER BY id")
        assert [bool(is_null) for (is_null,) in stored] == [True, False, True]


def test_create_tables_leaves_existing_tables_and_rows(table):
    backend = sqlalchemy_backend()
    orders = Machine(Order, EDGES, load=table.get, backend=backend)
    orders.start(1)

    backend.create_tables()

    assert orders.state_of(1) is Order.pending


# ── Settings ──


def test_the_backend_connects_to_the_sqlalchemy_url_setting(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'state.sqlite3'}"
    monkeypatch.setenv("AGENTIC_FSM_SQLALCHEMY_URL", url)

    backend = get_backend("agentic_fsm.backends.sqlalchemy")
    backend.create_tables()
    Machine(Order, EDGES, load=dict().get, backend=backend).start(1)

    assert str(backend.engine.url) == url
    assert get_backend("agentic_fsm.backends.sqlalchemy").engine is backend.engine
    with create_engine(url).connect() as connection:
        assert connection.execute(select(current_state.c.state)).scalars().all() == ["pending"]


def test_the_backend_without_an_engine_or_url_is_refused_on_first_use(monkeypatch):
    monkeypatch.delenv("AGENTIC_FSM_SQLALCHEMY_URL", raising=False)
    assert settings.SQLALCHEMY_URL is None
    backend = StateBackend()  # a machine declared at import builds this before any URL is set

    with pytest.raises(LookupError, match="AGENTIC_FSM_SQLALCHEMY_URL"):
        backend.engine


def test_an_engine_function_is_called_once_on_first_use(tmp_path):
    calls = []

    def make_engine():
        calls.append(1)
        return create_engine(f"sqlite:///{tmp_path / 'state.sqlite3'}")

    backend = StateBackend(make_engine)
    orders = Machine(Order, EDGES, load=dict().get, backend=backend)
    assert calls == []

    backend.create_tables()
    orders.start(1)

    assert orders.state_of(1) is Order.pending
    assert calls == [1]


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
    """While pending's step runs in one thread, another fires the same order."""
    step_running, may_finish = Event(), Event()

    def pending(order):
        step_running.set()
        may_finish.wait(timeout=5)
        return orders.states.paid

    orders = order_machine(postgres_backend, table, pending=pending)
    table["1"] = new_order(cancel_requested=True)
    orders.start(1)

    first = Thread(target=orders.fire, args=(1,))
    first.start()
    step_running.wait(timeout=5)
    second = Thread(target=orders.fire, args=(1,))
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
