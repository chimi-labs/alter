"""SQL DDL exporter — generates CREATE TABLE statements from AlterSchema.

Produces valid, runnable Postgres DDL.  Column types are converted with
``alter_to_sql()``.  FOREIGN KEY constraints are emitted as table-level
constraints using the ``Relation`` objects in the schema.
"""

from __future__ import annotations

from alter.schema import AlterSchema, Column, Relation, Table
from alter.types import alter_to_sql


def export_sql(schema: AlterSchema) -> str:
    """Export *schema* as Postgres ``CREATE TABLE`` SQL DDL.

    Generates one ``CREATE TABLE`` statement per table, with:

    - Column type, PRIMARY KEY, NOT NULL, UNIQUE, DEFAULT
    - Table-level ``FOREIGN KEY ... REFERENCES ... ON DELETE`` constraints
      derived from the schema's ``Relation`` objects.

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

    parts = [_table_to_sql(table, rel_index) for table in schema.tables]
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _table_to_sql(
    table: Table, rel_index: dict[tuple[str, str], list[Relation]]
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
            fk_lines.append(
                f"    FOREIGN KEY ({col.name})"
                f" REFERENCES {rel.to_table} ({rel.to_column})"
                f"{on_del}"
            )

    if multi_pk:
        col_lines.append(f"    PRIMARY KEY ({', '.join(pk_cols)})")

    all_defs = col_lines + fk_lines
    body = ",\n".join(all_defs)
    return f"CREATE TABLE {table.name} (\n{body}\n);"


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
        parts.append(f"DEFAULT {_format_default(col.default)}")

    return " ".join(parts)


def _format_default(default: str) -> str:
    """Format a default value for SQL output."""
    if default == "uuid4":
        return "gen_random_uuid()"
    if default == "utcnow":
        return "now()"
    if default.upper() in ("TRUE", "FALSE"):
        return default.upper()
    # Numeric literal — emit as-is
    try:
        float(default)
        return default
    except ValueError:
        pass
    # String literal — wrap in single quotes
    return f"'{default}'"
