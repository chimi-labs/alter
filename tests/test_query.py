"""Tests for src/alter/query.py — query execution engine.

Unit tests run without a database. Integration tests require
``docker compose up -d`` (PostgreSQL on port 5433).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from alter.query import (
    QueryResult,
    _serialize_value,
    _validate_sql,
    describe_table_data,
    execute_query,
    format_results,
    get_query_plan,
    get_table_relationships,
)

# ---------------------------------------------------------------------------
# Connection string for integration tests
# ---------------------------------------------------------------------------

_TEST_DSN = "postgresql://alter:alter@localhost:5433/alter_test"


# ===========================================================================
# Unit tests — no database required
# ===========================================================================


# ---------------------------------------------------------------------------
# SQL validation
# ---------------------------------------------------------------------------


class TestValidateSql:
    """Tests for _validate_sql (application-level safety check)."""

    def test_select_allowed(self) -> None:
        _validate_sql("SELECT 1")

    def test_select_with_where(self) -> None:
        _validate_sql("SELECT * FROM users WHERE id = 1")

    def test_select_with_trailing_semicolon(self) -> None:
        _validate_sql("SELECT 1;")

    def test_select_with_leading_whitespace(self) -> None:
        _validate_sql("  \n  SELECT 1")

    def test_cte_allowed(self) -> None:
        _validate_sql("WITH cte AS (SELECT 1) SELECT * FROM cte")

    def test_explain_allowed(self) -> None:
        _validate_sql("EXPLAIN SELECT 1")

    def test_explain_analyze_allowed(self) -> None:
        # EXPLAIN ANALYZE is a SELECT-like read operation
        _validate_sql("EXPLAIN ANALYZE SELECT 1")

    def test_subquery_in_select(self) -> None:
        _validate_sql("SELECT (SELECT count(*) FROM users) AS n")

    def test_insert_blocked(self) -> None:
        with pytest.raises(ValueError, match="(?i)select"):
            _validate_sql("INSERT INTO users (name) VALUES ('x')")

    def test_update_blocked(self) -> None:
        with pytest.raises(ValueError, match="(?i)select"):
            _validate_sql("UPDATE users SET name = 'x'")

    def test_delete_blocked(self) -> None:
        with pytest.raises(ValueError, match="(?i)select"):
            _validate_sql("DELETE FROM users")

    def test_drop_blocked(self) -> None:
        with pytest.raises(ValueError, match="(?i)select"):
            _validate_sql("DROP TABLE users")

    def test_alter_blocked(self) -> None:
        with pytest.raises(ValueError, match="(?i)select"):
            _validate_sql("ALTER TABLE users ADD COLUMN x TEXT")

    def test_truncate_blocked(self) -> None:
        with pytest.raises(ValueError, match="(?i)select"):
            _validate_sql("TRUNCATE users")

    def test_create_blocked(self) -> None:
        with pytest.raises(ValueError, match="(?i)select"):
            _validate_sql("CREATE TABLE x (id INT)")

    def test_multi_statement_blocked(self) -> None:
        with pytest.raises(ValueError, match="(?i)single"):
            _validate_sql("SELECT 1; DROP TABLE users")

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="(?i)empty"):
            _validate_sql("")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(ValueError, match="(?i)empty"):
            _validate_sql("   \n  ")

    def test_semicolon_only_rejected(self) -> None:
        with pytest.raises(ValueError, match="(?i)empty"):
            _validate_sql(";")


# ---------------------------------------------------------------------------
# Type serialization
# ---------------------------------------------------------------------------


class TestSerializeValue:
    """Tests for _serialize_value."""

    def test_none(self) -> None:
        assert _serialize_value(None) is None

    def test_datetime(self) -> None:
        dt = datetime(2024, 1, 15, 10, 30, 0)
        assert _serialize_value(dt) == "2024-01-15T10:30:00"

    def test_date(self) -> None:
        d = date(2024, 1, 15)
        assert _serialize_value(d) == "2024-01-15"

    def test_time(self) -> None:
        t = time(10, 30, 0)
        assert _serialize_value(t) == "10:30:00"

    def test_decimal(self) -> None:
        assert _serialize_value(Decimal("3.14")) == "3.14"

    def test_uuid(self) -> None:
        u = UUID("12345678-1234-5678-1234-567812345678")
        assert _serialize_value(u) == "12345678-1234-5678-1234-567812345678"

    def test_bytes(self) -> None:
        assert _serialize_value(b"\x00\x01\x02") == "<3 bytes>"

    def test_string_passthrough(self) -> None:
        assert _serialize_value("hello") == "hello"

    def test_int_passthrough(self) -> None:
        assert _serialize_value(42) == 42

    def test_float_passthrough(self) -> None:
        assert _serialize_value(3.14) == 3.14

    def test_bool_passthrough(self) -> None:
        assert _serialize_value(True) is True


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


class TestFormatResults:
    """Tests for format_results."""

    @pytest.fixture()
    def sample_result(self) -> QueryResult:
        return QueryResult(
            columns=["id", "name", "score"],
            rows=[(1, "Alice", Decimal("98.5")), (2, "Bob", None)],
            row_count=2,
            truncated=False,
            execution_time_ms=12.34,
        )

    def test_table_format_has_headers(self, sample_result: QueryResult) -> None:
        out = format_results(sample_result, output_format="table")
        assert "id" in out
        assert "name" in out
        assert "score" in out
        assert "Alice" in out
        assert "NULL" in out  # None renders as NULL

    def test_table_format_truncation_note(self) -> None:
        result = QueryResult(
            columns=["x"],
            rows=[(1,), (2,), (3,)],
            row_count=3,
            truncated=True,
            execution_time_ms=1.0,
        )
        out = format_results(result, output_format="table")
        assert "(truncated)" in out

    def test_json_format_valid(self, sample_result: QueryResult) -> None:
        out = format_results(sample_result, output_format="json")
        data = json.loads(out)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["name"] == "Alice"
        assert data[1]["name"] == "Bob"
        assert data[1]["score"] is None

    def test_csv_format_parseable(self, sample_result: QueryResult) -> None:
        out = format_results(sample_result, output_format="csv")
        lines = out.strip().split("\n")
        assert len(lines) == 3  # header + 2 data rows
        assert "id,name,score" in lines[0]

    def test_empty_result(self) -> None:
        result = QueryResult(
            columns=[],
            rows=[],
            row_count=0,
            truncated=False,
            execution_time_ms=0,
        )
        out = format_results(result)
        assert out == "(no results)"


# ===========================================================================
# Integration tests — require docker compose up -d
# ===========================================================================


@pytest.mark.integration
class TestExecuteQueryIntegration:
    """Integration tests for execute_query against a live PostgreSQL."""

    def test_select_literal(self) -> None:
        result = execute_query("SELECT 1 AS n", connection_string=_TEST_DSN)
        assert result.columns == ["n"]
        assert result.rows == [(1,)]
        assert result.row_count == 1
        assert not result.truncated

    def test_select_string(self) -> None:
        result = execute_query("SELECT 'hello' AS greeting", connection_string=_TEST_DSN)
        assert result.rows == [("hello",)]

    def test_row_limit_truncation(self) -> None:
        result = execute_query(
            "SELECT generate_series(1, 100) AS n",
            connection_string=_TEST_DSN,
            row_limit=5,
        )
        assert result.row_count == 5
        assert result.truncated is True

    def test_row_limit_no_truncation(self) -> None:
        result = execute_query(
            "SELECT generate_series(1, 3) AS n",
            connection_string=_TEST_DSN,
            row_limit=100,
        )
        assert result.row_count == 3
        assert result.truncated is False

    def test_read_only_blocks_write(self) -> None:
        """Even if app-level validation is bypassed, DB enforces read-only."""
        import psycopg2

        conn = psycopg2.connect(_TEST_DSN)
        # set_session must be called before any execute (before a transaction begins)
        conn.set_session(readonly=True)
        try:
            cur = conn.cursor()
            with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
                cur.execute("CREATE TABLE _test_readonly (id INT)")
        finally:
            conn.close()

    def test_cte_works(self) -> None:
        result = execute_query(
            "WITH nums AS (SELECT generate_series(1,3) AS n) SELECT * FROM nums",
            connection_string=_TEST_DSN,
        )
        assert result.row_count == 3

    def test_execution_time_populated(self) -> None:
        result = execute_query("SELECT 1", connection_string=_TEST_DSN)
        assert result.execution_time_ms >= 0

    def test_bad_connection_string_raises(self) -> None:
        with pytest.raises(RuntimeError, match="(?i)connect"):
            execute_query("SELECT 1", connection_string="postgresql://bad:bad@localhost:1/nope")


@pytest.mark.integration
class TestDescribeTableIntegration:
    """Integration tests for describe_table_data."""

    @pytest.fixture(autouse=True)
    def _setup_table(self) -> None:
        """Create a temp table for testing describe_table_data."""
        import psycopg2

        conn = psycopg2.connect(_TEST_DSN)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS _test_describe CASCADE")
            cur.execute("""
                CREATE TABLE _test_describe (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    score NUMERIC(5,2),
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            cur.execute("""
                INSERT INTO _test_describe (name, score)
                VALUES ('Alice', 95.5), ('Bob', 87.0), ('Charlie', 92.3)
            """)
        finally:
            conn.close()

    def test_returns_row_count(self) -> None:
        out = describe_table_data("_test_describe", connection_string=_TEST_DSN)
        assert "3" in out  # 3 rows

    def test_returns_column_info(self) -> None:
        out = describe_table_data("_test_describe", connection_string=_TEST_DSN)
        assert "name" in out
        assert "score" in out
        assert "created_at" in out

    def test_returns_sample_data(self) -> None:
        out = describe_table_data("_test_describe", connection_string=_TEST_DSN)
        assert "Alice" in out

    def test_nonexistent_table_raises(self) -> None:
        with pytest.raises(RuntimeError, match="(?i)_test_no_such_table"):
            describe_table_data("_test_no_such_table", connection_string=_TEST_DSN)


@pytest.mark.integration
class TestGetTableRelationshipsIntegration:
    """Integration tests for get_table_relationships."""

    @pytest.fixture(autouse=True)
    def _setup_tables(self) -> None:
        """Create temp tables with FK relationships."""
        import psycopg2

        conn = psycopg2.connect(_TEST_DSN)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS _test_posts CASCADE")
            cur.execute("DROP TABLE IF EXISTS _test_authors CASCADE")
            cur.execute("""
                CREATE TABLE _test_authors (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE _test_posts (
                    id SERIAL PRIMARY KEY,
                    title VARCHAR(200) NOT NULL,
                    author_id INT NOT NULL REFERENCES _test_authors(id) ON DELETE CASCADE
                )
            """)
        finally:
            conn.close()

    def test_outgoing_fk(self) -> None:
        out = get_table_relationships("_test_posts", connection_string=_TEST_DSN)
        assert "_test_authors" in out
        assert "author_id" in out

    def test_incoming_fk(self) -> None:
        out = get_table_relationships("_test_authors", connection_string=_TEST_DSN)
        assert "_test_posts" in out
        assert "author_id" in out

    def test_no_relationships(self) -> None:
        """Table with no FKs returns empty string."""
        import psycopg2

        conn = psycopg2.connect(_TEST_DSN)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DROP TABLE IF EXISTS _test_no_fks")
            cur.execute("CREATE TABLE _test_no_fks (id SERIAL PRIMARY KEY, val TEXT)")
        finally:
            conn.close()

        out = get_table_relationships("_test_no_fks", connection_string=_TEST_DSN)
        assert out == ""


@pytest.mark.integration
class TestGetQueryPlanIntegration:
    """Integration tests for get_query_plan."""

    def test_returns_plan(self) -> None:
        plan = get_query_plan("SELECT 1", connection_string=_TEST_DSN)
        assert "Result" in plan  # EXPLAIN for SELECT 1 shows "Result"

    def test_rejects_insert(self) -> None:
        with pytest.raises(ValueError, match="(?i)select"):
            get_query_plan("INSERT INTO x VALUES (1)", connection_string=_TEST_DSN)


# ===========================================================================
# MCP tool wrappers (unit tests — no DB for error paths)
# ===========================================================================


class TestMcpQueryTools:
    """Test MCP tool functions from mcp_server.py."""

    @pytest.fixture(autouse=True)
    def _init_mcp(self, tmp_path: Path) -> None:
        import alter.mcp_server as ms

        alter_path = tmp_path / "test.alter"
        ms.init_mcp(alter_path)

    def test_query_db_missing_url(self) -> None:
        from alter.mcp_server import query_db

        old = os.environ.pop("DATABASE_URL", None)
        try:
            with pytest.raises(ValueError, match="DATABASE_URL"):
                query_db(sql="SELECT 1")
        finally:
            if old is not None:
                os.environ["DATABASE_URL"] = old

    def test_describe_table_data_missing_url(self) -> None:
        from alter.mcp_server import describe_table_data as dt

        old = os.environ.pop("DATABASE_URL", None)
        try:
            with pytest.raises(ValueError, match="DATABASE_URL"):
                dt(table_name="users")
        finally:
            if old is not None:
                os.environ["DATABASE_URL"] = old

    def test_explain_query_missing_url(self) -> None:
        from alter.mcp_server import explain_query

        old = os.environ.pop("DATABASE_URL", None)
        try:
            with pytest.raises(ValueError, match="DATABASE_URL"):
                explain_query(sql="SELECT 1")
        finally:
            if old is not None:
                os.environ["DATABASE_URL"] = old
