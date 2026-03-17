"""Read-only query execution engine for PostgreSQL.

Provides safe, read-only SQL execution with timeout and row limits.
Independent of MCP, staging, and the .alter file — can be reused from CLI,
canvas, or tests.

Safety model (two layers):
  1. Application-level: sqlparse validates the SQL is a single SELECT/WITH/EXPLAIN
  2. Database-level: conn.set_session(readonly=True) (psycopg2 enforces before any transaction)

The database-level protection is the real safety net. The application-level
check exists only for better error messages.
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from decimal import Decimal
from typing import Any
from uuid import UUID

import sqlparse


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def _get_connection_string(connection_string: str | None = None) -> str:
    """Resolve connection string from argument or DATABASE_URL env var.

    Matches the pattern from ``introspect_db`` (mcp_server.py line 1249).
    """
    cs = connection_string or os.environ.get("DATABASE_URL")
    if not cs:
        raise ValueError(
            "No connection string provided and DATABASE_URL is not set. "
            "Pass connection_string or set DATABASE_URL."
        )
    return cs


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class QueryResult:
    """Result of a query execution."""

    columns: list[str]
    rows: list[tuple[Any, ...]]
    row_count: int  # actual rows returned (after truncation)
    truncated: bool  # True if there were more rows than row_limit
    execution_time_ms: float


# ---------------------------------------------------------------------------
# SQL validation
# ---------------------------------------------------------------------------


def _validate_sql(sql: str) -> None:
    """Validate that SQL is a single read-only statement.

    Two layers of protection:
      1. This function (app-level) — for clear error messages
      2. conn.set_session(readonly=True) (DB-level, applied before any transaction)

    Raises ValueError with a clear message on rejection.
    """
    stripped = sql.strip()
    if not stripped:
        raise ValueError("Empty SQL query.")

    statements = sqlparse.parse(stripped)
    # Filter out empty/whitespace-only statements (trailing semicolons produce these)
    non_empty = [s for s in statements if str(s).strip() and str(s).strip() != ";"]

    if len(non_empty) == 0:
        raise ValueError("Empty SQL query.")
    if len(non_empty) > 1:
        raise ValueError(
            "Only single SQL statements are allowed. "
            "Multiple statements separated by ';' are not permitted."
        )

    stmt = non_empty[0]
    stmt_type = stmt.get_type()

    # sqlparse returns "SELECT" for plain selects
    if stmt_type == "SELECT":
        return

    # WITH...SELECT and EXPLAIN are classified as UNKNOWN by sqlparse.
    # Check the first real keyword token.
    first = stmt.token_first(skip_cm=True, skip_ws=True)
    if first is not None and hasattr(first, "normalized"):
        first_word = first.normalized.upper()
        if first_word in ("SELECT", "WITH", "EXPLAIN"):
            return

    raise ValueError(
        f"Only SELECT, WITH...SELECT, and EXPLAIN queries are allowed. "
        f"Got statement type: {stmt_type}"
    )


# ---------------------------------------------------------------------------
# Type serialization
# ---------------------------------------------------------------------------


def _serialize_value(val: Any) -> Any:
    """Convert database values to JSON/display-safe types."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, dt_time):
        return val.isoformat()
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, (bytes, memoryview)):
        return f"<{len(val)} bytes>"
    return val


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------


def execute_query(
    sql: str,
    connection_string: str | None = None,
    row_limit: int = 100,
    timeout_ms: int = 30000,
) -> QueryResult:
    """Execute a read-only SQL query and return results.

    Args:
        sql: A SELECT query to execute.
        connection_string: A libpq-compatible connection string or URL.
            Falls back to the ``DATABASE_URL`` environment variable.
        row_limit: Maximum rows to return (hard-capped at 1000).
        timeout_ms: Statement timeout in milliseconds (default 30s).

    Returns:
        A QueryResult with columns, rows, and metadata.

    Raises:
        ImportError: if psycopg2 is not installed.
        ValueError: if the SQL is not a valid SELECT query.
        RuntimeError: if the database connection or query fails.
    """
    try:
        import psycopg2  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "psycopg2-binary is required for database queries.\n"
            "Install it with: pip install alterdb[db]"
        ) from exc

    cs = _get_connection_string(connection_string)

    # Validate SQL before touching the database
    _validate_sql(sql)

    # Hard cap
    row_limit = min(max(row_limit, 1), 1000)

    try:
        conn = psycopg2.connect(cs)
    except Exception as exc:
        raise RuntimeError(
            f"Could not connect to the database: {exc}\n"
            "Check that the connection string or DATABASE_URL is correct "
            "and the database is reachable."
        ) from exc

    try:
        # Safety: enforce read-only on the connection before any transaction begins.
        # set_session() must be called before the first execute() — it applies to
        # the connection itself, unlike SET default_transaction_read_only which only
        # affects future transactions (not the current implicit one).
        conn.set_session(readonly=True)
        cur = conn.cursor()
        # Safety: statement timeout
        cur.execute(f"SET statement_timeout = '{timeout_ms}';")

        t0 = time.monotonic()
        cur.execute(sql)
        elapsed_ms = (time.monotonic() - t0) * 1000

        columns = [desc[0] for desc in cur.description] if cur.description else []
        # Fetch one extra to detect truncation
        raw_rows = cur.fetchmany(row_limit + 1)
        truncated = len(raw_rows) > row_limit
        rows = [tuple(raw_rows[i]) for i in range(min(len(raw_rows), row_limit))]

        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            execution_time_ms=round(elapsed_ms, 2),
        )
    except Exception as exc:
        raise RuntimeError(f"Query failed: {exc}") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


def format_results(result: QueryResult, output_format: str = "table") -> str:
    """Format a QueryResult as a human-readable string.

    Args:
        result: The query result to format.
        output_format: ``"table"`` (default), ``"json"``, or ``"csv"``.

    Returns:
        Formatted string representation of the results.
    """
    if not result.columns:
        return "(no results)"

    if output_format == "json":
        return _format_json(result)
    elif output_format == "csv":
        return _format_csv(result)
    else:
        return _format_table(result)


def _format_table(result: QueryResult) -> str:
    """ASCII table with aligned columns."""
    safe_rows = [
        [_serialize_value(v) for v in row]
        for row in result.rows
    ]
    # Column widths: max of header and all values
    widths = [len(c) for c in result.columns]
    for row in safe_rows:
        for i, val in enumerate(row):
            display = "NULL" if val is None else str(val)
            widths[i] = max(widths[i], len(display))

    # Header
    header = " | ".join(c.ljust(widths[i]) for i, c in enumerate(result.columns))
    separator = "-+-".join("-" * w for w in widths)

    lines = [header, separator]
    for row in safe_rows:
        line = " | ".join(
            ("NULL" if v is None else str(v)).ljust(widths[i])
            for i, v in enumerate(row)
        )
        lines.append(line)

    # Footer
    footer_parts = [f"{result.row_count} row{'s' if result.row_count != 1 else ''}"]
    if result.truncated:
        footer_parts.append("(truncated)")
    footer_parts.append(f"in {result.execution_time_ms}ms")
    lines.append(f"\n{' '.join(footer_parts)}")

    return "\n".join(lines)


def _format_json(result: QueryResult) -> str:
    """List of dicts, JSON-serializable."""
    rows = []
    for row in result.rows:
        rows.append({
            col: _serialize_value(val)
            for col, val in zip(result.columns, row)
        })
    return json.dumps(rows, indent=2, default=str)


def _format_csv(result: QueryResult) -> str:
    """CSV with headers."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(result.columns)
    for row in result.rows:
        writer.writerow([_serialize_value(v) for v in row])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Table relationships from live database
# ---------------------------------------------------------------------------


def get_table_relationships(
    table_name: str,
    schema_name: str = "public",
    connection_string: str | None = None,
) -> str:
    """Get FK relationships for a table from the live database.

    Queries ``information_schema`` for all foreign keys where this table is
    either the source (outgoing) or target (incoming).

    Args:
        table_name: Name of the table.
        schema_name: PostgreSQL schema (default "public").
        connection_string: A libpq-compatible connection string.
            Falls back to ``DATABASE_URL``.

    Returns:
        A human-readable summary of relationships, or empty string if none.
    """
    try:
        import psycopg2  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "psycopg2-binary is required for database queries.\n"
            "Install it with: pip install alterdb[db]"
        ) from exc

    cs = _get_connection_string(connection_string)

    try:
        conn = psycopg2.connect(cs)
    except Exception as exc:
        raise RuntimeError(
            f"Could not connect to the database: {exc}\n"
            "Check that the connection string or DATABASE_URL is correct."
        ) from exc

    try:
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '10000';")

        # Outgoing FKs: this table references others
        cur.execute(
            """
            SELECT
                kcu.column_name,
                ccu.table_name AS ref_table,
                ccu.column_name AS ref_column,
                rc.delete_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.table_schema = tc.table_schema
            JOIN information_schema.referential_constraints rc
              ON rc.constraint_name = tc.constraint_name
             AND rc.constraint_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = %s
              AND kcu.table_name = %s
            ORDER BY kcu.column_name
            """,
            (schema_name, table_name),
        )
        outgoing = cur.fetchall()

        # Incoming FKs: other tables reference this one
        cur.execute(
            """
            SELECT
                kcu.table_name AS src_table,
                kcu.column_name AS src_column,
                ccu.column_name AS ref_column,
                rc.delete_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.table_schema = tc.table_schema
            JOIN information_schema.referential_constraints rc
              ON rc.constraint_name = tc.constraint_name
             AND rc.constraint_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND ccu.table_schema = %s
              AND ccu.table_name = %s
            ORDER BY kcu.table_name, kcu.column_name
            """,
            (schema_name, table_name),
        )
        incoming = cur.fetchall()

    except Exception as exc:
        raise RuntimeError(
            f"Failed to query relationships for '{table_name}': {exc}"
        ) from exc
    finally:
        conn.close()

    if not outgoing and not incoming:
        return ""

    parts: list[str] = []
    if outgoing:
        parts.append("References (outgoing):")
        for col, ref_table, ref_col, delete_rule in outgoing:
            parts.append(f"  {table_name}.{col} → {ref_table}.{ref_col} ({delete_rule})")

    if incoming:
        parts.append("Referenced by (incoming):")
        for src_table, src_col, ref_col, delete_rule in incoming:
            parts.append(f"  {src_table}.{src_col} → {table_name}.{ref_col} ({delete_rule})")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Describe table
# ---------------------------------------------------------------------------


def describe_table_data(
    table_name: str,
    schema_name: str = "public",
    connection_string: str | None = None,
    sample_rows: int = 5,
) -> str:
    """Return row count, column info, relationships, and sample data for a table.

    Gets ALL information from the live database — no dependency on the
    .alter file or staging system.

    Args:
        table_name: Name of the table to describe.
        schema_name: PostgreSQL schema (default "public").
        connection_string: A libpq-compatible connection string.
            Falls back to ``DATABASE_URL``.
        sample_rows: Number of sample rows (hard-capped at 20).

    Returns:
        A formatted string with table metadata and sample data.
    """
    try:
        import psycopg2  # noqa: PLC0415
        from psycopg2 import sql as psql  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "psycopg2-binary is required for database queries.\n"
            "Install it with: pip install alterdb[db]"
        ) from exc

    cs = _get_connection_string(connection_string)
    sample_rows = min(max(sample_rows, 1), 20)

    try:
        conn = psycopg2.connect(cs)
    except Exception as exc:
        raise RuntimeError(
            f"Could not connect to the database: {exc}\n"
            "Check that the connection string or DATABASE_URL is correct."
        ) from exc

    try:
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '10000';")

        # Row count (both schema and table escaped as identifiers)
        count_query = psql.SQL("SELECT count(*) FROM {}.{}").format(
            psql.Identifier(schema_name),
            psql.Identifier(table_name),
        )
        cur.execute(count_query)
        total_count = cur.fetchone()[0]

        # Column info from information_schema
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema_name, table_name),
        )
        col_info = cur.fetchall()

        # Sample rows (both schema and table escaped as identifiers)
        sample_query = psql.SQL("SELECT * FROM {}.{} LIMIT %s").format(
            psql.Identifier(schema_name),
            psql.Identifier(table_name),
        )
        cur.execute(sample_query, (sample_rows,))
        columns = [desc[0] for desc in cur.description] if cur.description else []
        sample = cur.fetchall()

    except Exception as exc:
        raise RuntimeError(f"Failed to describe table '{table_name}': {exc}") from exc
    finally:
        conn.close()

    # Get relationship context (separate connection — previous one is closed)
    try:
        relationships = get_table_relationships(table_name, schema_name, connection_string)
    except Exception:
        relationships = ""

    # Format output
    parts: list[str] = []
    parts.append(f"Table: {schema_name}.{table_name} ({total_count:,} rows)")
    parts.append("")

    # Column details
    parts.append("Columns:")
    for cname, dtype, nullable, default in col_info:
        nullable_str = "NULL" if nullable == "YES" else "NOT NULL"
        default_str = f" DEFAULT {default}" if default else ""
        parts.append(f"  {cname}: {dtype} {nullable_str}{default_str}")
    parts.append("")

    # Relationships
    if relationships:
        parts.append("Relationships:")
        for line in relationships.split("\n"):
            if line and not line.endswith(":"):
                parts.append(f"  {line.strip()}")
            elif line.endswith(":"):
                parts.append(line)
        parts.append("")

    # Sample data
    if sample:
        parts.append(f"Sample data ({len(sample)} rows):")
        sample_result = QueryResult(
            columns=columns,
            rows=[tuple(r) for r in sample],
            row_count=len(sample),
            truncated=False,
            execution_time_ms=0,
        )
        parts.append(_format_table(sample_result))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Query plan
# ---------------------------------------------------------------------------


def get_query_plan(
    sql: str,
    connection_string: str | None = None,
) -> str:
    """Return the EXPLAIN output for a SQL query (without executing it).

    Args:
        sql: The SQL query to explain.
        connection_string: A libpq-compatible connection string.
            Falls back to ``DATABASE_URL``.

    Returns:
        The query plan as a string.
    """
    try:
        import psycopg2  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "psycopg2-binary is required for database queries.\n"
            "Install it with: pip install alterdb[db]"
        ) from exc

    cs = _get_connection_string(connection_string)
    _validate_sql(sql)

    try:
        conn = psycopg2.connect(cs)
    except Exception as exc:
        raise RuntimeError(
            f"Could not connect to the database: {exc}\n"
            "Check that the connection string or DATABASE_URL is correct."
        ) from exc

    try:
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '10000';")
        # ANALYZE false = don't actually run the query
        cur.execute(f"EXPLAIN (ANALYZE false, FORMAT TEXT) {sql}")
        rows = cur.fetchall()
        return "\n".join(row[0] for row in rows)
    except Exception as exc:
        raise RuntimeError(f"EXPLAIN failed: {exc}") from exc
    finally:
        conn.close()
