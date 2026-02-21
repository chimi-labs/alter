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


def export_mermaid(schema: AlterSchema) -> str:
    """Export *schema* as a Mermaid ERD diagram.

    Produces entity blocks for each table and relationship lines for each
    relation.  Column attribute annotations (``PK``, ``FK``, ``UK``) are
    included where applicable.

    Args:
        schema: The ``AlterSchema`` to export.

    Returns:
        A Mermaid ERD string starting with ``erDiagram``, ending with a
        trailing newline.
    """
    lines: list[str] = ["erDiagram"]

    for table in schema.tables:
        lines.append(f"    {table.name} {{")
        for col in table.columns:
            attr = _col_attr(col)
            lines.append(f"        {col.type} {col.name}{attr}")
        lines.append("    }")

    if schema.relations:
        lines.append("")
        for rel in schema.relations:
            syntax = _CARDINALITY.get(rel.type, "||--o{")
            label = rel.name or f"{rel.from_table}_{rel.from_column}"
            lines.append(
                f'    {rel.from_table} {syntax} {rel.to_table} : "{label}"'
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
    elif col.unique and not col.primary_key:
        attrs.append("UK")
    return (" " + ",".join(attrs)) if attrs else ""
