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


# ---------------------------------------------------------------------------
# FK introspection — composite FK guard
# ---------------------------------------------------------------------------


class TestFkCompositeGuard:
    """Verify correct FK handling and the composite-FK exclusion subquery."""

    def test_single_column_fk_creates_one_relation(self) -> None:
        """A single-column FK still produces exactly one Relation (no regression)."""
        cursor = _make_cursor(
            table_names=["posts", "users"],
            col_rows=[
                ("users", "id",      "uuid",    None, "NO",  None, "uuid"),
                ("posts", "id",      "uuid",    None, "NO",  None, "uuid"),
                ("posts", "user_id", "uuid",    None, "YES", None, "uuid"),
            ],
            pk_rows=[("users", "id"), ("posts", "id")],
            fk_rows=[("posts", "user_id", "users", "id", "CASCADE")],
        )
        result = _introspect(_make_conn(cursor))
        fk_rels = [r for r in result.relations if r.from_table == "posts"]
        assert len(fk_rels) == 1
        assert fk_rels[0].from_column == "user_id"
        assert fk_rels[0].to_table == "users"
        assert fk_rels[0].to_column == "id"

    def test_multiple_single_column_fks_on_same_table(self) -> None:
        """Multiple independent single-column FKs are all captured."""
        cursor = _make_cursor(
            table_names=["orders"],
            col_rows=[
                ("orders", "id",         "uuid", None, "NO",  None, "uuid"),
                ("orders", "user_id",    "uuid", None, "YES", None, "uuid"),
                ("orders", "product_id", "uuid", None, "YES", None, "uuid"),
            ],
            pk_rows=[("orders", "id")],
            fk_rows=[
                ("orders", "user_id",    "users",    "id", "CASCADE"),
                ("orders", "product_id", "products", "id", "CASCADE"),
            ],
        )
        result = _introspect(_make_conn(cursor))
        fk_rels = {r.from_column: r for r in result.relations if r.from_table == "orders"}
        assert "user_id" in fk_rels
        assert "product_id" in fk_rels
        assert fk_rels["user_id"].to_table == "users"
        assert fk_rels["product_id"].to_table == "products"

    def test_fk_sql_excludes_composite_constraints(self) -> None:
        """The FK query must contain the subquery that filters out multi-column FKs."""
        cursor = _make_cursor(table_names=["t"])
        # We only care about the SQL sent, not the result
        conn = _make_conn(cursor)
        _introspect(conn)

        # The 5th execute call is the FK query (0-indexed: tables, cols, pk, uq, fk)
        all_calls = cursor.execute.call_args_list
        assert len(all_calls) >= 5, "Expected at least 5 execute() calls"
        fk_sql = all_calls[4][0][0]   # positional arg 0 of the 5th call

        # Must contain the subquery that counts columns per constraint
        assert "count(*)" in fk_sql.lower() or "count" in fk_sql.lower()
        assert "= 1" in fk_sql

    def test_self_referencing_single_column_fk(self) -> None:
        """A self-referencing FK (same table, different column) is handled."""
        cursor = _make_cursor(
            table_names=["categories"],
            col_rows=[
                ("categories", "id",        "uuid", None, "NO",  None, "uuid"),
                ("categories", "parent_id", "uuid", None, "YES", None, "uuid"),
            ],
            pk_rows=[("categories", "id")],
            fk_rows=[("categories", "parent_id", "categories", "id", "SET NULL")],
        )
        result = _introspect(_make_conn(cursor))
        self_refs = [r for r in result.relations if r.from_table == "categories"]
        assert len(self_refs) == 1
        assert self_refs[0].from_column == "parent_id"
        assert self_refs[0].to_column == "id"
        assert self_refs[0].on_delete == "SET NULL"


# ---------------------------------------------------------------------------
# ORM parameter threading
# ---------------------------------------------------------------------------


class TestOrmParameter:
    """_introspect and import_from_database must stamp the caller-supplied ORM."""

    def test_default_orm_is_sqlmodel(self) -> None:
        cursor = _make_cursor(table_names=["users"])
        result = _introspect(_make_conn(cursor))
        assert result.orm == "sqlmodel"

    def test_sqlalchemy_orm_is_preserved(self) -> None:
        cursor = _make_cursor(table_names=["users"])
        result = _introspect(_make_conn(cursor), orm="sqlalchemy")
        assert result.orm == "sqlalchemy"

    def test_sqlmodel_orm_explicit(self) -> None:
        cursor = _make_cursor(table_names=["users"])
        result = _introspect(_make_conn(cursor), orm="sqlmodel")
        assert result.orm == "sqlmodel"

    def test_mcp_introspect_db_passes_project_orm(self, tmp_path) -> None:
        """introspect_db must use the current schema's ORM, not hardcode sqlmodel."""
        import alter.mcp_server as ms
        import alter.importers.database as db_mod
        from alter.schema import AlterSchema
        from unittest.mock import patch

        # Project schema uses SQLAlchemy
        alter_path = tmp_path / "schema.alter"
        AlterSchema(orm="sqlalchemy").save(alter_path)
        ms.init_mcp(alter_path)

        captured: list[str] = []

        def fake_import(cs: str, schema: str = "public", orm: str = "sqlmodel") -> AlterSchema:
            captured.append(orm)
            return AlterSchema(orm=orm)

        original = db_mod.import_from_database
        db_mod.import_from_database = fake_import  # type: ignore[assignment]
        try:
            from alter.mcp_server import introspect_db
            introspect_db(connection_string="postgresql://fake/db")
        except Exception:
            pass  # connection errors are fine; we only care about the ORM arg
        finally:
            db_mod.import_from_database = original

        assert captured, "import_from_database was never called"
        assert captured[0] == "sqlalchemy", (
            f"introspect_db passed orm='{captured[0]}' but project ORM is 'sqlalchemy'"
        )
