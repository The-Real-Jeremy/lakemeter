"""Column types that work on both PostgreSQL and SQLite (``DB_BACKEND``).

On PostgreSQL these compile to exactly what the models used before
(native ``UUID``, ``JSON`` and ``JSONB``); on SQLite they are stored as text.
"""
from sqlalchemy import JSON, Uuid
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB


def UUID(as_uuid: bool = True) -> Uuid:  # noqa: N802 - drop-in for postgresql.UUID
    return Uuid(as_uuid=as_uuid)


JSONB = JSON().with_variant(_PG_JSONB(), "postgresql")

__all__ = ["JSON", "JSONB", "UUID"]


# ---------------------------------------------------------------------------
# SQLite: store and return values the way the PostgreSQL column would.
# ---------------------------------------------------------------------------
#
# Some model types differ from the real column types in schema.sql (for
# example ``line_items.fmapi_quantity`` is ``Numeric(18, 2)`` in the model but
# ``BIGINT`` in the database, and ``hours_per_month`` is ``Integer`` in the
# model but ``DECIMAL(10,2)``). PostgreSQL silently coerces on write and
# returns the column's own type on read. SQLite does neither, so on SQLite
# each numeric column gets a variant that applies the PostgreSQL column's
# rounding on write and returns what psycopg would return on read.

import re as _re
from decimal import Decimal as _Decimal

from sqlalchemy import Float as _Float, Integer as _Integer
from sqlalchemy.types import TypeDecorator as _TypeDecorator


class _PgStoredInteger(_TypeDecorator):
    """A PostgreSQL INT/BIGINT column on SQLite."""

    impl = _Integer
    cache_ok = True

    def process_bind_param(self, value, dialect):
        from app.services.pg_numeric import cast_int
        return cast_int(value, bits=64)

    def process_result_value(self, value, dialect):
        return None if value is None else int(value)


class _PgStoredNumeric(_TypeDecorator):
    """A PostgreSQL NUMERIC(p, s) column on SQLite (REAL storage, 15 digits)."""

    impl = _Float
    cache_ok = True

    def __init__(self, scale: int):
        super().__init__()
        self.scale = scale

    def process_bind_param(self, value, dialect):
        from app.services.pg_numeric import num, typmod
        return None if value is None else float(typmod(num(value), self.scale))

    def process_result_value(self, value, dialect):
        from app.services.pg_numeric import num, typmod
        if value is None:
            return None
        return typmod(num(float(value)), self.scale)


class _PgStoredDouble(_TypeDecorator):
    """A PostgreSQL DOUBLE PRECISION column on SQLite."""

    impl = _Float
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else float(value)

    def process_result_value(self, value, dialect):
        return None if value is None else float(value)


def _sqlite_variant(ddl_type: str):
    ddl_type = ddl_type.upper()
    numeric = _re.match(r"(?:NUMERIC|DECIMAL)\s*\(\s*\d+\s*,\s*(\d+)\s*\)", ddl_type)
    if numeric:
        return _PgStoredNumeric(int(numeric.group(1)))
    if ddl_type.startswith(("BIGINT", "INT", "SMALLINT")):
        return _PgStoredInteger()
    if ddl_type.startswith(("DOUBLE", "REAL", "FLOAT")):
        return _PgStoredDouble()
    return None


def apply_postgres_storage_semantics(metadata) -> int:
    """Give every numeric ORM column a SQLite variant matching schema.sql.

    Returns the number of columns given a variant. PostgreSQL is unaffected.
    """
    from app.sqlite_backend import ddl_column_types

    ddl = ddl_column_types()
    applied = 0
    for table in metadata.tables.values():
        for column in table.columns:
            declared = ddl.get(table.name, {}).get(column.name)
            if declared is None:
                continue
            variant = _sqlite_variant(declared)
            if variant is None:
                continue
            column.type = column.type.with_variant(variant, "sqlite")
            applied += 1
    return applied
