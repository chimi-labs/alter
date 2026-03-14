"""SQL DDL exporter — generates CREATE TABLE statements from AlterSchema.

Produces valid, runnable Postgres DDL.  Column types are converted with
``alter_to_sql()``.  FOREIGN KEY constraints are emitted as table-level
constraints using the ``Relation`` objects in the schema.
"""

from __future__ import annotations

from alter.schema import AlterSchema, Column, Relation, Table
from alter.types import alter_to_sql


def _qualified_name(table: Table) -> str:
    """Return ``schema.table`` when *table* has a schema, else just ``table``."""
    if table.schema_name:
        return f"{table.schema_name}.{table.name}"
    return table.name


def export_sql(schema: AlterSchema) -> str:
    """Export *schema* as Postgres ``CREATE TABLE`` SQL DDL.

    Generates one ``CREATE TABLE`` statement per table, with:

    - Column type, PRIMARY KEY, NOT NULL, UNIQUE, DEFAULT
    - Table-level ``FOREIGN KEY ... REFERENCES ... ON DELETE`` constraints
      derived from the schema's ``Relation`` objects.
    - Schema-qualified table names (``schema.table``) when ``schema_name``
      is set on a table.

    Args:
        schema: The ``AlterSchema`` to export.

    Returns:
        A string of Postgres DDL statements separated by blank lines,
        ending with a trailing newline.  Returns an empty string if the
        schema has no tables.
    """
    # Build relation lookup: (from_table, from_column) → [Relation, ...]
    # A column can reference multiple tables (multiple FK constraints).
    rel_index: dict[tuple[str, str], list[Relation]] = {}
    for r in schema.relations:
        rel_index.setdefault((r.from_table, r.from_column), []).append(r)

    # Build table lookup by name so FK REFERENCES can use the qualified name.
    table_by_name: dict[str, Table] = {t.name: t for t in schema.tables}

    parts: list[str] = []
    for table in schema.tables:
        qualified = _qualified_name(table)
        parts.append(_table_to_sql(table, rel_index, table_by_name))
        for col in table.columns:
            if col.index and not col.primary_key:
                parts.append(
                    f"CREATE INDEX idx_{table.name}_{col.name}"
                    f" ON {qualified} ({col.name});"
                )
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _table_to_sql(
    table: Table,
    rel_index: dict[tuple[str, str], list[Relation]],
    table_by_name: dict[str, Table] | None = None,
) -> str:
    """Render one ``CREATE TABLE`` statement."""
    pk_cols = [c.name for c in table.columns if c.primary_key]
    multi_pk = len(pk_cols) > 1

    col_lines: list[str] = []
    fk_lines: list[str] = []

    for col in table.columns:
        col_lines.append("    " + _column_to_sql(col, inline_pk=not multi_pk))

        for rel in rel_index.get((table.name, col.name), []):
            on_del = f" ON DELETE {rel.on_delete}" if rel.on_delete else ""
            # Use the qualified name of the referenced table when available.
            ref_table_obj = (table_by_name or {}).get(rel.to_table)
            ref_name = _qualified_name(ref_table_obj) if ref_table_obj else rel.to_table
            fk_lines.append(
                f"    FOREIGN KEY ({col.name})"
                f" REFERENCES {ref_name} ({rel.to_column})"
                f"{on_del}"
            )

    if multi_pk:
        col_lines.append(f"    PRIMARY KEY ({', '.join(pk_cols)})")

    all_defs = col_lines + fk_lines
    body = ",\n".join(all_defs)
    return f"CREATE TABLE {_qualified_name(table)} (\n{body}\n);"


def _column_to_sql(col: Column, inline_pk: bool = True) -> str:
    """Render one column definition."""
    sql_type = alter_to_sql(col.type, col.max_length)
    parts: list[str] = [col.name, sql_type]

    if col.primary_key and inline_pk:
        parts.append("PRIMARY KEY")
    elif not col.nullable:
        parts.append("NOT NULL")

    if col.unique and not col.primary_key:
        parts.append("UNIQUE")

    if col.default is not None:
        sql_default = _format_default(col.default)
        if sql_default is not None:
            parts.append(f"DEFAULT {sql_default}")

    return " ".join(parts)


def _format_default(default: str) -> str | None:
    """Format a default value for SQL output.

    Returns ``None`` to omit the DEFAULT clause entirely when the stored
    default has no meaningful SQL equivalent (e.g. Python-only lambda
    expressions stored as ``expr:...``).
    """
    # Python-only default_factory expressions — no SQL equivalent
    if default.startswith("expr:"):
        return None
    if default == "uuid4":
        return "gen_random_uuid()"
    if default in ("utcnow", "now"):
        return "now()"
    # list/dict factory defaults → empty JSONB literal
    if default in ("list", "[]"):
        return "'[]'::jsonb"
    if default in ("dict", "{}"):
        return "'{}'::jsonb"
    if default.upper() in ("TRUE", "FALSE"):
        return default.upper()
    # Numeric literal — emit as-is
    try:
        float(default)
        return default
    except ValueError:
        pass
    # String literal — wrap in single quotes, doubling any internal quotes
    # (standard SQL escaping: don't → 'don''t').
    return "'{}'".format(default.replace("'", "''"))
