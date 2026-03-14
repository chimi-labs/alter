"""Tests for alter.importers.database — schema introspection.

All tests use a fake psycopg2 cursor so no live database is needed.
The fake cursor records each ``execute()`` call so we can assert that
the correct schema name (``'public'`` or a custom one) is used in every
SQL query.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from alter.importers.database import _introspect, _parse_pg_default, _pg_type


# ---------------------------------------------------------------------------
# Fake psycopg2 plumbing
# ---------------------------------------------------------------------------


def _make_cursor(
    table_names: list[str] | None = None,
    col_rows: list[tuple] | None = None,
    pk_rows: list[tuple] | None = None,
    uq_rows: list[tuple] | None = None,
    fk_rows: list[tuple] | None = None,
    index_rows: list[tuple] | None = None,
) -> MagicMock:
    """Return a mock cursor whose fetchall() returns the given data in order."""
    table_names = table_names or []
    col_rows = col_rows or []
    pk_rows = pk_rows or []
    uq_rows = uq_rows or []
    fk_rows = fk_rows or []
    index_rows = index_rows or []

    call_results = [
        [(name,) for name in table_names],  # tables query
        col_rows,                            # columns query
        pk_rows,                             # primary keys query
        uq_rows,                             # unique constraints query
        fk_rows,                             # foreign keys query
        index_rows,                          # indexes query
    ]
    cursor = MagicMock()
    cursor.fetchall.side_effect = call_results
    return cursor


def _make_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


# ---------------------------------------------------------------------------
# Helpers: schema parameter is threaded through SQL queries
# ---------------------------------------------------------------------------


class TestIntrospectSchemaParameter:
    def test_default_schema_is_public(self) -> None:
        cursor = _make_cursor()
        conn = _make_conn(cursor)
        _introspect(conn)
        # Every execute() call should pass 'public' as the schema param.
        for call in cursor.execute.call_args_list:
            args, kwargs = call
            if len(args) >= 2:
                params = args[1]
                assert params == ("public",), (
                    f"Expected schema='public' but got {params!r} in SQL:\n{args[0]}"
                )

    def test_custom_schema_used_in_all_queries(self) -> None:
        cursor = _make_cursor()
        conn = _make_conn(cursor)
        _introspect(conn, schema="myapp")
        for call in cursor.execute.call_args_list:
            args, kwargs = call
            if len(args) >= 2:
                params = args[1]
                assert params == ("myapp",), (
                    f"Expected schema='myapp' but got {params!r} in SQL:\n{args[0]}"
                )

    def test_all_six_queries_receive_schema_param(self) -> None:
        """Every one of the six introspection queries must be parameterised."""
        cursor = _make_cursor()
        conn = _make_conn(cursor)
        _introspect(conn, schema="analytics")
        parameterised = [
            call for call in cursor.execute.call_args_list
            if len(call.args) >= 2
        ]
        assert len(parameterised) == 6, (
            f"Expected 6 parameterised queries, got {len(parameterised)}"
        )

    def test_no_hardcoded_public_in_sql(self) -> None:
        """SQL strings must not contain the literal string 'public'."""
        cursor = _make_cursor()
        conn = _make_conn(cursor)
        _introspect(conn, schema="tenant")
        for call in cursor.execute.call_args_list:
            sql = call.args[0]
            assert "'public'" not in sql, (
                f"Found hardcoded 'public' in SQL:\n{sql}"
            )


# ---------------------------------------------------------------------------
# Table construction from introspection data
# ---------------------------------------------------------------------------

# A minimal set of column rows for a 'users' table.
# Tuple shape: (table_name, col_name, data_type, char_max_len, is_nullable,
#               column_default, udt_name)
_USERS_COL_ROWS = [
    ("users", "id",    "uuid",               None, "NO",  None,                "uuid"),
    ("users", "email", "character varying",  255,  "NO",  None,                "varchar"),
    ("users", "score", "integer",            None, "YES", None,                "int4"),
]


class TestIntrospectBuildsSchema:
    def test_table_names_imported(self) -> None:
        cursor = _make_cursor(
            table_names=["orders", "users"],
            col_rows=_USERS_COL_ROWS,
            pk_rows=[("users", "id")],
        )
        result = _introspect(_make_conn(cursor))
        names = {t.name for t in result.tables}
        assert "orders" in names
        assert "users" in names

    def test_columns_mapped(self) -> None:
        cursor = _make_cursor(
            table_names=["users"],
            col_rows=_USERS_COL_ROWS,
            pk_rows=[("users", "id")],
        )
        result = _introspect(_make_conn(cursor))
        tbl = next(t for t in result.tables if t.name == "users")
        col_names = {c.name for c in tbl.columns}
        assert col_names == {"id", "email", "score"}

    def test_primary_key_flagged(self) -> None:
        cursor = _make_cursor(
            table_names=["users"],
            col_rows=_USERS_COL_ROWS,
            pk_rows=[("users", "id")],
        )
        result = _introspect(_make_conn(cursor))
        tbl = next(t for t in result.tables if t.name == "users")
        id_col = next(c for c in tbl.columns if c.name == "id")
        assert id_col.primary_key is True
        assert id_col.nullable is False

    def test_nullable_column(self) -> None:
        cursor = _make_cursor(
            table_names=["users"],
            col_rows=_USERS_COL_ROWS,
        )
        result = _introspect(_make_conn(cursor))
        tbl = next(t for t in result.tables if t.name == "users")
        score_col = next(c for c in tbl.columns if c.name == "score")
        assert score_col.nullable is True

    def test_relation_created_from_fk(self) -> None:
        cursor = _make_cursor(
            table_names=["posts"],
            col_rows=[
                ("posts", "id",      "integer", None, "NO",  None, "int4"),
                ("posts", "user_id", "uuid",    None, "NO",  None, "uuid"),
            ],
            pk_rows=[("posts", "id")],
            fk_rows=[("posts", "user_id", "users", "id", "CASCADE")],
        )
        result = _introspect(_make_conn(cursor))
        assert len(result.relations) == 1
        rel = result.relations[0]
        assert rel.from_table == "posts"
        assert rel.from_column == "user_id"
        assert rel.to_table == "users"
        assert rel.to_column == "id"
        assert rel.on_delete == "CASCADE"

    def test_tables_get_grid_positions(self) -> None:
        cursor = _make_cursor(table_names=["a", "b", "c", "d"])
        result = _introspect(_make_conn(cursor))
        positions = [(t.position.x, t.position.y) for t in result.tables]
        # All positions must be non-zero after auto-layout
        assert all(x > 0 and y > 0 for x, y in positions)
        # All positions must be unique
        assert len(set(positions)) == len(positions)


# ---------------------------------------------------------------------------
# schema_name set on non-public schema tables
# ---------------------------------------------------------------------------


class TestNonPublicSchemaName:
    def test_public_schema_tables_have_no_schema_name(self) -> None:
        cursor = _make_cursor(table_names=["users"])
        result = _introspect(_make_conn(cursor), schema="public")
        tbl = result.tables[0]
        assert not tbl.schema_name  # None or empty

    def test_custom_schema_tables_have_schema_name_set(self) -> None:
        cursor = _make_cursor(table_names=["invoices"])
        result = _introspect(_make_conn(cursor), schema="billing")
        tbl = result.tables[0]
        assert tbl.schema_name == "billing"

    def test_analytics_schema_name_propagated(self) -> None:
        cursor = _make_cursor(table_names=["events", "pageviews"])
        result = _introspect(_make_conn(cursor), schema="analytics")
        for tbl in result.tables:
            assert tbl.schema_name == "analytics"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestPgType:
    def test_uuid(self) -> None:
        assert _pg_type("uuid", "uuid") == "uuid"

    def test_varchar(self) -> None:
        assert _pg_type("character varying", "varchar") == "string"

    def test_integer(self) -> None:
        assert _pg_type("integer", "int4") == "int"

    def test_jsonb_via_udt(self) -> None:
        # data_type may be "USER-DEFINED"; udt_name carries the real type
        assert _pg_type("USER-DEFINED", "jsonb") == "json"

    def test_unknown_type_falls_back_to_string(self) -> None:
        assert _pg_type("some_unknown_type", "some_unknown_udt") == "string"


class TestParsePgDefault:
    def test_none_returns_none(self) -> None:
        assert _parse_pg_default(None) is None

    def test_uuid_generate_v4(self) -> None:
        assert _parse_pg_default("uuid_generate_v4()") == "uuid4"

    def test_gen_random_uuid(self) -> None:
        assert _parse_pg_default("gen_random_uuid()") == "uuid4"

    def test_now(self) -> None:
        assert _parse_pg_default("now()") == "now"

    def test_current_timestamp(self) -> None:
        assert _parse_pg_default("CURRENT_TIMESTAMP") == "now"

    def test_true_false(self) -> None:
        assert _parse_pg_default("true") == "true"
        assert _parse_pg_default("false") == "false"

    def test_cast_expression_stripped(self) -> None:
        assert _parse_pg_default("'admin'::character varying") == "admin"

    def test_unrecognised_returns_none(self) -> None:
        assert _parse_pg_default("nextval('seq')") is None
