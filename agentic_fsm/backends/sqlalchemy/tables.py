"""The two tables behind the SQLAlchemy state backend.

Their names, columns, indexes and constraint match the Django backend's models, so a database
whose tables Django's migrations made can be read and written through this backend too.
"""

from datetime import UTC

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()


class UTCDateTime(TypeDecorator):
    """A moment stored in UTC and read back as an aware UTC datetime, on every database.

    Postgres keeps the zone; SQLite drops it, so a naive value read back is UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return value.astimezone(UTC) if value is not None else None

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# A BIGINT key autoincrements on Postgres; SQLite autoincrements only an INTEGER one.
_ID = BigInteger().with_variant(Integer, "sqlite")

log_entry = Table(
    "agentic_fsm_logentry",
    metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("machine", String(100), nullable=False),
    Column("entity", String(255), nullable=False),
    Column("step", String(255), nullable=True),
    Column("from_state", String(255), nullable=True),
    Column("to_state", String(255), nullable=False),
    Column("decided_by", String(255), nullable=False),
    Column("decision", JSON().with_variant(JSONB, "postgresql"), nullable=True),
    Column("at", UTCDateTime(), nullable=False),
    Index("agentic_fsm_log_entity", "machine", "entity", "id"),
)

current_state = Table(
    "agentic_fsm_currentstate",
    metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("machine", String(100), nullable=False),
    Column("entity", String(255), nullable=False),
    Column("state", String(255), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    UniqueConstraint("machine", "entity", name="agentic_fsm_one_state_per_entity"),
    Index("agentic_fsm_current_state", "machine", "state"),
)
