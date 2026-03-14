"""PostgreSQL database introspection importer.

Connects to a live PostgreSQL database and imports its schema into an
``AlterSchema`` object.  Uses ``information_schema`` and ``pg_constraint``
— no ORM imports needed.

Usage::

    from alter.importers.database import import_from_database
    schema = import_from_database("postgresql://user:pass@localhost/mydb")
"""

from __future__ import annotations

import re
from collections import defaultdict
from alter.schema import AlterSchema, Column, Relation, Table

# Grid constants for auto-positioning imported tables
_GRID_COLS = 3
_GRID_COL_W = 290
_GRID_ROW_H = 310
_GRID_ORIGIN_X = 80
_GRID_ORIGIN_Y = 80

# Map PostgreSQL data type → alter schema type
_PG_TYPE_MAP: dict[str, str] = {
    "uuid": "uuid",
    "character varying": "string",
    "varchar": "string",
    "character": "string",
    "bpchar": "string",
    "text": "text",
    "integer": "int",
    "int4": "int",
    "int2": "int",
    "smallint": "int",
    "bigint": "bigint",
    "int8": "bigint",
    "double precision": "float",
    "float8": "float",
    "real": "float",
    "float4": "float",
    "numeric": "decimal",
    "decimal": "decimal",
    "boolean": "bool",
    "bool": "bool",
    "timestamp with time zone": "datetime",
    "timestamp without time zone": "datetime",
    "timestamptz": "datetime",
    "timestamp": "datetime",
    "date": "date",
    "time without time zone": "time",
    "time": "time",
    "json": "json",
    "jsonb": "json",
    "bytea": "bytes",
}


def import_from_database(
    connection_string: str,
    schema: str = "public",
    orm: str = "sqlmodel",
) -> AlterSchema:
    """Introspect a live PostgreSQL database and return an ``AlterSchema``.

    Reads tables, columns, primary keys, unique constraints, foreign key
    relations, and indexes from the specified PostgreSQL schema.

    Args:
        connection_string: A ``libpq``-compatible connection string, e.g.
            ``"postgresql://user:pass@localhost/mydb"`` or
            ``"host=localhost dbname=mydb user=myuser"``.
        schema: PostgreSQL schema name to introspect.  Defaults to
            ``"public"``.  Use a custom value for databases that place
            tables in a non-default schema (e.g. ``"myapp"``,
            ``"analytics"``).  Tables from a non-``public`` schema will
            have ``schema_name`` set on the resulting ``Table`` objects so
            that generated SQL uses the fully-qualified ``schema.table``
            reference.
        orm: ORM backend to stamp on the returned ``AlterSchema`` —
            ``"sqlmodel"`` (default) or ``"sqlalchemy"``.  Callers should
            pass the current project's ORM so that ``alter apply`` generates
            code in the correct style.

    Returns:
        An ``AlterSchema`` with grid-positioned tables and relations.

    Raises:
        ImportError: if ``psycopg2`` is not installed.
        Exception: re-raised psycopg2 connection / query errors with context.
    """
    try:
        import psycopg2  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "psycopg2-binary is required for database introspection.\n"
            "Install it with: pip install psycopg2-binary"
        ) from exc

    try:
        conn = psycopg2.connect(connection_string)
    except Exception as exc:
        raise RuntimeError(
            f"Could not connect to the database: {exc}\n"
            "Check that DATABASE_URL is set correctly and the database is reachable."
        ) from exc

    try:
        return _introspect(conn, schema=schema, orm=orm)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def _introspect(conn: object, schema: str = "public", orm: str = "sqlmodel") -> AlterSchema:
    """Run all introspection queries and build the schema.

    Args:
        conn:   An open psycopg2 connection.
        schema: PostgreSQL schema name to inspect (default ``"public"``).
    """
    cursor = conn.cursor()  # type: ignore[attr-defined]

    # ── Table names ─────────────────────────────────────────────────────────
    cursor.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (schema,),
    )
    table_names: list[str] = [row[0] for row in cursor.fetchall()]

    # ── Columns ─────────────────────────────────────────────────────────────
    cursor.execute(
        """
        SELECT table_name, column_name, data_type, character_maximum_length,
               is_nullable, column_default, udt_name
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_name, ordinal_position
        """,
        (schema,),
    )
    col_rows: dict[str, list] = defaultdict(list)
    for row in cursor.fetchall():
        col_rows[row[0]].append(row)

    # ── Primary keys ─────────────────────────────────────────────────────────
    cursor.execute(
        """
        SELECT kcu.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema   = kcu.table_schema
        WHERE tc.table_schema   = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        """,
        (schema,),
    )
    pk_cols: set[tuple[str, str]] = {(r[0], r[1]) for r in cursor.fetchall()}

    # ── Unique constraints ───────────────────────────────────────────────────
    cursor.execute(
        """
        SELECT kcu.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema   = kcu.table_schema
        WHERE tc.table_schema   = %s
          AND tc.constraint_type = 'UNIQUE'
        """,
        (schema,),
    )
    uq_cols: set[tuple[str, str]] = {(r[0], r[1]) for r in cursor.fetchall()}

    # ── Foreign keys ─────────────────────────────────────────────────────────
    # Only single-column FK constraints are fetched.  Joining kcu (the
    # referencing side) with ccu (the referenced side) on constraint_name alone
    # produces a cartesian product for composite FKs — e.g. a two-column FK
    # (a, b) REFERENCES t(x, y) would yield four rows (a→x, a→y, b→x, b→y)
    # instead of two.  Since alter's data model stores FK references per column
    # (Column.foreign_key is a single string), composite FK constraints cannot
    # be represented accurately regardless; the subquery below skips them
    # entirely to avoid producing wrong per-column mappings.
    cursor.execute(
        """
        SELECT tc.table_name, kcu.column_name,
               ccu.table_name  AS foreign_table,
               ccu.column_name AS foreign_column,
               rc.delete_rule
        FROM information_schema.table_constraints      AS tc
        JOIN information_schema.key_column_usage       AS kcu
          ON tc.constraint_name  = kcu.constraint_name
         AND tc.table_schema     = kcu.table_schema
        JOIN information_schema.constraint_column_usage AS ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema    = tc.table_schema
        JOIN information_schema.referential_constraints  AS rc
          ON tc.constraint_name  = rc.constraint_name
        WHERE tc.table_schema   = %s
          AND tc.constraint_type = 'FOREIGN KEY'
          AND (
              SELECT count(*)
              FROM information_schema.key_column_usage k2
              WHERE k2.constraint_name = tc.constraint_name
                AND k2.table_schema    = tc.table_schema
          ) = 1
        ORDER BY tc.table_name, kcu.column_name
        """,
        (schema,),
    )
    fk_rows = cursor.fetchall()

    # Build a column-level FK map so Column.foreign_key is populated,
    # keeping the schema consistent with code-parsed schemas.
    fk_col_map: dict[tuple[str, str], str] = {
        (from_tbl, from_col): f"{to_tbl}.{to_col}"
        for (from_tbl, from_col, to_tbl, to_col, _del) in fk_rows
    }

    # ── Indexes ──────────────────────────────────────────────────────────────
    cursor.execute(
        """
        SELECT tablename, indexdef
        FROM pg_indexes
        WHERE schemaname = %s
        """,
        (schema,),
    )
    index_cols: set[tuple[str, str]] = set()
    for tablename, indexdef in cursor.fetchall():
        # Parse: "CREATE [UNIQUE] INDEX name ON table USING btree (col)"
        m = re.search(r"ON\s+\S+\s+USING\s+\w+\s+\((\w+)\)", indexdef, re.IGNORECASE)
        if m:
            index_cols.add((tablename, m.group(1)))
        else:
            m = re.search(r"\((\w+)\)", indexdef)
            if m:
                index_cols.add((tablename, m.group(1)))

    # ── Build Table objects ──────────────────────────────────────────────────
    # Tables from a non-public schema get schema_name set so generated SQL
    # uses fully-qualified references (e.g. "myapp"."users").
    set_schema_name = schema if schema != "public" else None

    tables: list[Table] = []
    for tname in table_names:
        cols: list[Column] = []
        for (_, cname, dtype, maxlen, nullable, default, udt) in col_rows.get(tname, []):
            is_pk = (tname, cname) in pk_cols
            is_uq = (tname, cname) in uq_cols and not is_pk
            is_idx = (tname, cname) in index_cols and not is_pk

            col = Column(
                name=cname,
                type=_pg_type(dtype, udt),
                primary_key=is_pk,
                nullable=(nullable == "YES") and not is_pk,
                unique=is_uq,
                index=is_idx,
                max_length=maxlen,
                default=_parse_pg_default(default),
                foreign_key=fk_col_map.get((tname, cname)),
            )
            cols.append(col)

        tables.append(
            Table(
                name=tname,
                columns=cols,
                **{"schema_name": set_schema_name} if set_schema_name else {},
            )
        )

    # ── Build Relation objects ───────────────────────────────────────────────
    _valid_on_delete = {"CASCADE", "SET NULL", "RESTRICT", "NO ACTION", "SET DEFAULT"}
    relations: list[Relation] = []
    for (from_tbl, from_col, to_tbl, to_col, del_rule) in fk_rows:
        on_delete = del_rule if del_rule in _valid_on_delete else "NO ACTION"
        relations.append(
            Relation(
                name=f"fk_{from_tbl}_{from_col}_{to_tbl}",
                from_table=from_tbl,
                from_column=from_col,
                to_table=to_tbl,
                to_column=to_col,
                on_delete=on_delete,  # type: ignore[arg-type]
            )
        )

    schema = AlterSchema(orm=orm, tables=tables, relations=relations)
    _auto_position(schema)
    return schema


# ---------------------------------------------------------------------------
# Type + default helpers
# ---------------------------------------------------------------------------


def _pg_type(dtype: str, udt: str) -> str:
    """Map a PostgreSQL data_type / udt_name to an alter schema type."""
    return _PG_TYPE_MAP.get(dtype) or _PG_TYPE_MAP.get(udt, "string")


def _parse_pg_default(pg_default: str | None) -> str | None:
    """Convert a PostgreSQL column default expression to an alter default value."""
    if pg_default is None:
        return None
    d = pg_default.lower()
    if "uuid_generate" in d or "gen_random_uuid" in d or "uuid_generate_v4" in d:
        return "uuid4"
    if "now()" in d or "current_timestamp" in d:
        return "now"
    if d in ("true", "false"):
        return d
    # Strip cast: 'value'::type → value
    m = re.match(r"^'(.*?)'::", pg_default)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Auto-layout
# ---------------------------------------------------------------------------


def _auto_position(schema: AlterSchema) -> None:
    """Assign a simple grid layout to tables that have no position."""
    for i, table in enumerate(schema.tables):
        if table.position.x == 0 and table.position.y == 0:
            col = i % _GRID_COLS
            row = i // _GRID_COLS
            table.position.x = _GRID_ORIGIN_X + col * _GRID_COL_W
            table.position.y = _GRID_ORIGIN_Y + row * _GRID_ROW_H
