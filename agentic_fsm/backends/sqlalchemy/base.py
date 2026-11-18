"""State kept in any database SQLAlchemy reaches, through this package's two tables.

The tables are the Django backend's, so either backend can keep a machine in the same database.
Built with no engine, the backend connects to the ``SQLALCHEMY_URL`` setting; one engine is
made per URL and shared by every backend built from it. Given a function instead, it calls it
for the engine on first use. ``create_tables()`` makes the tables
where they don't exist yet.

``lock`` opens a transaction and takes the entity's current-state row with ``SELECT ... FOR
UPDATE``. A step that writes through ``connection()`` writes in that transaction, so its rows
commit with the move and roll back with it. A lock taken inside another (a step that fires a
second entity) shares the outer transaction through a savepoint. SQLite has no row lock and
ignores ``FOR UPDATE``; there the transaction holds the database once it writes.
"""

from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache, cached_property

from sqlalchemy import Connection, Engine, create_engine, insert, select, update
from sqlalchemy.exc import IntegrityError

from ...conf import ENV_PREFIX, settings
from ..base.base import AlreadyStarted, BaseStateBackend, Current, Move, utc_now
from .tables import current_state, log_entry, metadata

LOG_FIELDS = ("machine", "entity", "from_state", "to_state", "decided_by", "at", "decision")

# The connection of each engine's open lock in this thread or task; none outside a lock.
_open: ContextVar[dict[Engine, Connection]] = ContextVar("agentic_fsm_sqlalchemy_open", default={})
# What waits for each engine's open lock to commit, in call order.
_callbacks: ContextVar[dict[Engine, list[Callable[[], None]]]] = ContextVar(
    "agentic_fsm_sqlalchemy_callbacks", default={}
)


class StateBackend(BaseStateBackend):
    def __init__(self, engine: Engine | Callable[[], Engine] | None = None):
        """``engine`` is an engine, a function that makes one (for a machine declared at import
        whose database is known only when it runs), or ``None`` for the ``SQLALCHEMY_URL``
        setting's. Either of the last two is resolved on first use."""
        self._engine = engine

    @cached_property
    def engine(self) -> Engine:
        """The engine the state is kept through, resolved on first use."""
        if isinstance(self._engine, Engine):
            return self._engine
        if self._engine is not None:
            return self._engine()
        return _engine_for(settings.SQLALCHEMY_URL)

    def create_tables(self):
        """Create the log and current-state tables where they don't exist yet."""
        metadata.create_all(self.engine)

    def connection(self) -> Connection:
        """The connection of the lock this code runs inside, for a step's own writes."""
        connection = _open.get().get(self.engine)
        if connection is None:
            raise RuntimeError("connection() is only open inside a lock, i.e. while a step runs")
        return connection

    # ── Writes ──

    def start(self, machine, entity, state):
        move = Move(machine, entity, None, state, "code", utc_now())
        try:
            with self._transaction() as connection:
                connection.execute(
                    insert(current_state).values(
                        machine=machine, entity=entity, state=state, updated_at=move.at
                    )
                )
                _append(connection, move)
        except IntegrityError:
            raise AlreadyStarted(f"{machine}:{entity} has already started") from None
        return move

    @contextmanager
    def lock(self, machine, entity):
        with self._transaction() as connection:
            row = connection.execute(
                select(current_state.c.entity, current_state.c.state)
                .where(current_state.c.machine == machine, current_state.c.entity == entity)
                .with_for_update()
            ).first()
            yield Current(*row) if row else None

    def move(self, move):
        connection = self.connection()
        connection.execute(
            update(current_state)
            .where(current_state.c.machine == move.machine, current_state.c.entity == move.entity)
            .values(state=move.to_state, updated_at=move.at)
        )
        _append(connection, move)
        return move

    def on_commit(self, callback):
        callbacks = _callbacks.get().get(self.engine)
        if callbacks is None:
            callback()
        else:
            callbacks.append(callback)

    # ── Reads ──

    def current(self, machine, entity):
        with self._reading() as connection:
            row = connection.execute(
                select(current_state.c.entity, current_state.c.state).where(
                    current_state.c.machine == machine, current_state.c.entity == entity
                )
            ).first()
        return Current(*row) if row else None

    def in_state(self, machine, state):
        with self._reading() as connection:
            rows = connection.execute(
                select(current_state.c.entity)
                .where(current_state.c.machine == machine, current_state.c.state == state)
                .order_by(current_state.c.id)
            )
            return list(rows.scalars())

    def all_current(self, machine):
        with self._reading() as connection:
            rows = connection.execute(
                select(current_state.c.entity, current_state.c.state)
                .where(current_state.c.machine == machine)
                .order_by(current_state.c.id)
            )
            return [Current(*row) for row in rows]

    def history(self, machine, entity):
        columns = [log_entry.c[field] for field in LOG_FIELDS]
        with self._reading() as connection:
            rows = connection.execute(
                select(*columns)
                .where(log_entry.c.machine == machine, log_entry.c.entity == entity)
                .order_by(log_entry.c.id)
            )
            return [Move(**row) for row in rows.mappings()]

    # ── Connections ──

    @contextmanager
    def _transaction(self):
        """A transaction of its own, or a savepoint inside the lock already open here.

        The callbacks ``on_commit`` was given inside run once the transaction has committed;
        a savepoint that rolls back drops the ones given inside it.
        """
        outer = _open.get().get(self.engine)
        if outer is not None:
            callbacks = _callbacks.get()[self.engine]
            kept = len(callbacks)
            try:
                with outer.begin_nested():
                    yield outer
            except BaseException:
                del callbacks[kept:]
                raise
            return
        callbacks = []
        with self.engine.begin() as connection:
            open_token = _open.set({**_open.get(), self.engine: connection})
            callbacks_token = _callbacks.set({**_callbacks.get(), self.engine: callbacks})
            try:
                yield connection
            finally:
                _callbacks.reset(callbacks_token)
                _open.reset(open_token)
        for callback in callbacks:
            callback()

    @contextmanager
    def _reading(self):
        """The open lock's connection, so a read sees its writes; else a connection of its own."""
        outer = _open.get().get(self.engine)
        if outer is not None:
            yield outer
            return
        with self.engine.connect() as connection:
            yield connection


@cache
def _engine_for(url: str | None) -> Engine:
    if not url:
        raise LookupError(f"the sqlalchemy backend needs an engine: pass one or set {ENV_PREFIX}SQLALCHEMY_URL")
    return create_engine(url)


def _append(connection: Connection, move: Move):
    connection.execute(insert(log_entry).values({field: getattr(move, field) for field in LOG_FIELDS}))
