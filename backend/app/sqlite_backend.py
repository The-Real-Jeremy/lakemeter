"""SQLite backend (``DB_BACKEND=sqlite``) for deployments with no PostgreSQL.

Layout: every connection opens an in-memory main database and ATTACHes the
SQLite file as ``lakemeter``, so the app's ``lakemeter.<table>`` SQL and the
ORM's ``schema="lakemeter"`` work unchanged.

Bootstrap mirrors ``bootstrap/runner.py``: the same ``schema.sql`` (translated
to SQLite DDL), the same seeds, and the same pricing CSVs loaded *by position*
exactly as PostgreSQL ``COPY ... CSV HEADER`` does (the header row is skipped,
not matched, and an empty unquoted field is NULL). The PL/pgSQL functions are
not loaded; ``services/pricing_engine.py`` replaces them.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

logger = logging.getLogger(__name__)

DEFAULT_SQLITE_PATH = "/tmp/lakemeter/lakemeter.db"


def backend_name() -> str:
    return os.getenv("DB_BACKEND", "postgres").strip().lower()


def is_sqlite_backend() -> bool:
    return backend_name() == "sqlite"


def sqlite_path() -> Path:
    return Path(os.getenv("SQLITE_PATH", DEFAULT_SQLITE_PATH))


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", timeout=30, check_same_thread=False)
    connection.execute("ATTACH DATABASE ? AS lakemeter", (str(path),))
    connection.execute("PRAGMA lakemeter.journal_mode=WAL")
    connection.execute("PRAGMA lakemeter.synchronous=NORMAL")
    # PostgreSQL LIKE is case-sensitive; SQLite's is not unless told.
    connection.execute("PRAGMA case_sensitive_like=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def create_sqlite_engine(path: Path | None = None):
    path = path or sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        "sqlite://",
        creator=lambda: _connect(path),
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
    )
    return engine


# ---------------------------------------------------------------------------
# DDL translation
# ---------------------------------------------------------------------------

_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+lakemeter\.(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)
_CREATE_INDEX = re.compile(
    r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+lakemeter\.(\w+)",
    re.IGNORECASE,
)


def _translate_types(statement: str) -> str:
    statement = re.sub(r"\bUUID\b", "TEXT", statement)
    statement = re.sub(r"\bJSONB?\b", "TEXT", statement)
    statement = re.sub(r"\bTIMESTAMPTZ\b", "TIMESTAMP", statement)
    statement = re.sub(r"DEFAULT\s+NOW\(\)", "DEFAULT CURRENT_TIMESTAMP", statement,
                       flags=re.IGNORECASE)
    # SQLite foreign keys name a table in the same database, unqualified.
    statement = re.sub(r"REFERENCES\s+lakemeter\.", "REFERENCES ", statement)
    return _CREATE_INDEX.sub(r"CREATE INDEX IF NOT EXISTS lakemeter.\1 ON \2", statement)


def sqlite_schema_statements(schema_sql: str, boundary: str) -> list[str]:
    """``schema.sql`` as a list of single SQLite statements."""
    statements: list[str] = []
    for chunk in schema_sql.split(boundary):
        for raw in chunk.split(";"):
            lines = [line for line in raw.splitlines() if not line.strip().startswith("--")]
            statement = "\n".join(lines).strip()
            if not statement or re.match(r"CREATE\s+SCHEMA", statement, re.IGNORECASE):
                continue
            statements.append(_translate_types(statement))
    return statements


def _columns(cursor: Any, table: str) -> dict[str, str]:
    cursor.execute(f"PRAGMA lakemeter.table_info({table})")
    return {row[1]: (row[2] or "").upper() for row in cursor.fetchall()}


def execute_schema(cursor: Any, statements: list[str]) -> None:
    for statement in statements:
        match = _ADD_COLUMN.match(statement)
        if match:
            table, column, column_type = match.groups()
            if column not in _columns(cursor, table):
                cursor.execute(
                    f"ALTER TABLE lakemeter.{table} ADD COLUMN {column} {column_type}")
            continue
        cursor.execute(statement)


# ---------------------------------------------------------------------------
# COPY ... FROM STDIN WITH (FORMAT CSV, HEADER TRUE), by position
# ---------------------------------------------------------------------------

_QUOTED_EMPTY = re.compile(r'(^|,)""(,|$)', re.MULTILINE)
_TRUE = {"t", "true", "y", "yes", "on", "1"}
_FALSE = {"f", "false", "n", "no", "off", "0"}


def _converter(declared: str):
    declared = declared.upper()
    if declared.startswith("BOOL"):
        def to_bool(value: str):
            text = value.strip().lower()
            if text in _TRUE:
                return 1
            if text in _FALSE:
                return 0
            raise ValueError(f'invalid input syntax for type boolean: "{value}"')
        return to_bool
    if any(word in declared for word in ("DOUBLE", "REAL", "FLOAT")):
        return float
    if declared.startswith(("INT", "BIGINT", "SMALLINT")):
        return int
    return str


def copy_csv(cursor: Any, table: str, columns: tuple[str, ...], path: Path) -> int:
    declared = _columns(cursor, table)
    converters = [_converter(declared.get(column, "TEXT")) for column in columns]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO lakemeter.{table} ({', '.join(columns)}) VALUES ({placeholders})"
    rows = []
    raw = path.read_text(encoding="utf-8")
    if _QUOTED_EMPTY.search(raw):
        # COPY loads "" as an empty string but an unquoted empty field as
        # NULL; csv.reader cannot tell them apart, so refuse rather than guess.
        raise RuntimeError(f"{path.name} quotes an empty field; loader needs extending")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)  # HEADER TRUE: skipped, never matched to columns
        for line_number, record in enumerate(reader, start=2):
            if len(record) != len(columns):
                raise RuntimeError(
                    f"{path.name}:{line_number}: expected {len(columns)} fields, "
                    f"got {len(record)} (COPY would reject this row)")
            rows.append(tuple(
                None if value == "" else convert(value)
                for value, convert in zip(record, converters)))
    cursor.executemany(sql, rows)
    return len(rows)


class SqliteCursor:
    """A DB-API cursor that accepts the PostgreSQL bootstrap SQL in runner.py.

    Only the handful of constructs runner.py uses are translated: ``%s``
    placeholders, ``TRUNCATE TABLE`` and ``NOW()``.
    """

    def __init__(self, cursor: Any):
        self._cursor = cursor

    @staticmethod
    def _sql(statement: str) -> str:
        statement = re.sub(r"TRUNCATE\s+TABLE\s+", "DELETE FROM ", statement,
                           flags=re.IGNORECASE)
        statement = re.sub(r"\bNOW\(\)", "CURRENT_TIMESTAMP", statement)
        return statement.replace("%s", "?")

    def execute(self, statement: str, params: Any = ()):
        return self._cursor.execute(self._sql(statement), params)

    def executemany(self, statement: str, rows: Any):
        return self._cursor.executemany(self._sql(statement), rows)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        self._cursor.close()


_COLUMN_DEF = re.compile(r"^\s*(\w+)\s+([A-Za-z]+(?:\s+PRECISION)?(?:\s*\([^)]*\))?)")
_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+lakemeter\.(\w+)\s*\(",
                           re.IGNORECASE)
_NOT_A_COLUMN = {"PRIMARY", "CHECK", "UNIQUE", "FOREIGN", "CONSTRAINT"}
_ADD_COLUMN_LINE = re.compile(
    r"ALTER\s+TABLE\s+lakemeter\.(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+([^;\n]+)",
    re.IGNORECASE)


def _split_top_level(body: str) -> list[str]:
    parts, depth, current = [], 0, []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def ddl_column_types(schema_sql: str | None = None) -> dict[str, dict[str, str]]:
    """``{table: {column: declared PostgreSQL type}}`` parsed from schema.sql."""
    if schema_sql is None:
        schema_sql = (Path(__file__).resolve().parent / "bootstrap" / "sql"
                      / "schema.sql").read_text(encoding="utf-8")
    tables: dict[str, dict[str, str]] = {}
    for match in _CREATE_TABLE.finditer(schema_sql):
        depth, index = 1, match.end()
        while depth and index < len(schema_sql):
            depth += {"(": 1, ")": -1}.get(schema_sql[index], 0)
            index += 1
        columns = tables.setdefault(match.group(1), {})
        for part in _split_top_level(schema_sql[match.end():index - 1]):
            definition = _COLUMN_DEF.match(part)
            if definition and definition.group(1).upper() not in _NOT_A_COLUMN:
                columns.setdefault(definition.group(1), definition.group(2).strip())
    for match in _ADD_COLUMN_LINE.finditer(schema_sql):
        table, column, column_type = match.groups()
        tables.setdefault(table, {}).setdefault(column, column_type.strip())
    return tables
