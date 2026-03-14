"""Mermaid ERD exporter — generates Mermaid diagram code from AlterSchema.

Produces a ``erDiagram`` block suitable for embedding in Markdown READMEs,
GitHub wikis, or documentation sites that render Mermaid diagrams.
"""

from __future__ import annotations

from alter.schema import AlterSchema, Column

# Mermaid cardinality notation for each relation type
_CARDINALITY: dict[str, str] = {
    "one-to-one":   "||--||",
    "one-to-many":  "||--o{",
    "many-to-one":  "}o--||",
    "many-to-many": "}o--o{",
}


def _mermaid_entity_name(table_name: str, schema_name: str | None) -> str:
    """Return a Mermaid-safe entity identifier for a table.

    Mermaid entity names must be plain identifiers (no dots).  When a table
    lives in a named schema we use ``schema_table`` (underscore-joined) so
    that:

    * Entity names remain valid Mermaid syntax across all renderer versions.
    * Tables with the same bare name in different schemas are kept distinct.

    A table without a schema keeps its original name unchanged so that
    existing diagrams are not affected.
    """
    if schema_name:
        return f"{schema_name}_{table_name}"
    return table_name


def export_mermaid(schema: AlterSchema) -> str:
    """Export *schema* as a Mermaid ERD diagram.

    Produces entity blocks for each table and relationship lines for each
    relation.  Column attribute annotations (``PK``, ``FK``, ``UK``) are
    included where applicable.

    Tables that declare a PostgreSQL schema via ``schema_name`` are rendered
    with a ``schema_table`` entity identifier so that multi-schema diagrams
    are unambiguous and the output remains valid Mermaid syntax.

    Args:
        schema: The ``AlterSchema`` to export.

    Returns:
        A Mermaid ERD string starting with ``erDiagram``, ending with a
        trailing newline.
    """
    # Build a lookup so relation lines can use the same qualified entity names.
    entity_name: dict[str, str] = {
        t.name: _mermaid_entity_name(t.name, t.schema_name) for t in schema.tables
    }

    lines: list[str] = ["erDiagram"]

    for table in schema.tables:
        ename = entity_name[table.name]
        lines.append(f"    {ename} {{")
        for col in table.columns:
            attr = _col_attr(col)
            lines.append(f"        {col.type} {col.name}{attr}")
        lines.append("    }")

    if schema.relations:
        lines.append("")
        for rel in schema.relations:
            syntax = _CARDINALITY.get(rel.type, "||--o{")
            label = rel.name or f"{rel.from_table}_{rel.from_column}"
            from_ename = entity_name.get(rel.from_table, rel.from_table)
            to_ename = entity_name.get(rel.to_table, rel.to_table)
            lines.append(
                f'    {from_ename} {syntax} {to_ename} : "{label}"'
            )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _col_attr(col: Column) -> str:
    """Return the Mermaid attribute suffix string for a column."""
    attrs: list[str] = []
    if col.primary_key:
        attrs.append("PK")
    if col.foreign_key:
        attrs.append("FK")
    if col.unique and not col.primary_key:
        attrs.append("UK")
    return (" " + ",".join(attrs)) if attrs else ""
