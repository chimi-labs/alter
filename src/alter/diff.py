"""Diff engine for AlterSchema.

Compares two ``AlterSchema`` instances and returns a list of
``SchemaChange`` objects describing what changed between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from alter.schema import AlterSchema, Column, Table


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

ChangeType = Literal[
    "add_table",
    "drop_table",
    "add_column",
    "drop_column",
    "modify_column",
    "add_relation",
    "drop_relation",
    "add_index",
    "drop_index",
    "add_enum",
    "drop_enum",
    "modify_enum",
]


@dataclass
class SchemaChange:
    """Describes a single schema change between two ``AlterSchema`` instances."""

    type: ChangeType
    table: str
    column: str | None = None
    details: dict = field(default_factory=dict)
    destructive: bool = False  # True for drops and type changes that lose data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def diff_schemas(old: AlterSchema, new: AlterSchema) -> list[SchemaChange]:
    """Return the list of changes needed to move *old* → *new*.

    Order: table-level changes first, then column-level, then relations,
    then indexes within each table.
    """
    changes: list[SchemaChange] = []

    old_tables = {t.name: t for t in old.tables}
    new_tables = {t.name: t for t in new.tables}

    # --- Tables added / removed -----------------------------------------
    for name in sorted(set(new_tables) - set(old_tables)):
        changes.append(SchemaChange(type="add_table", table=name))
        # Index events for columns in a brand-new table
        for col in new_tables[name].columns:
            if col.index and not col.primary_key:
                changes.append(
                    SchemaChange(type="add_index", table=name, column=col.name)
                )

    for name in sorted(set(old_tables) - set(new_tables)):
        changes.append(
            SchemaChange(type="drop_table", table=name, destructive=True)
        )

    # --- Columns within tables that exist in both -----------------------
    for name in sorted(set(old_tables) & set(new_tables)):
        changes.extend(_diff_columns(name, old_tables[name], new_tables[name]))

    # --- Relations -------------------------------------------------------
    old_rels = {_rel_key(r): r for r in old.relations}
    new_rels = {_rel_key(r): r for r in new.relations}

    for key in sorted(set(new_rels) - set(old_rels)):
        r = new_rels[key]
        changes.append(
            SchemaChange(
                type="add_relation",
                table=r.from_table,
                column=r.from_column,
                details={"to": f"{r.to_table}.{r.to_column}"},
            )
        )

    for key in sorted(set(old_rels) - set(new_rels)):
        r = old_rels[key]
        changes.append(
            SchemaChange(
                type="drop_relation",
                table=r.from_table,
                column=r.from_column,
                details={"to": f"{r.to_table}.{r.to_column}"},
                destructive=True,
            )
        )

    # --- Enums -------------------------------------------------------
    old_enums = {e.name: e for e in old.enums}
    new_enums = {e.name: e for e in new.enums}

    for name in sorted(set(new_enums) - set(old_enums)):
        changes.append(SchemaChange(type="add_enum", table=name))

    for name in sorted(set(old_enums) - set(new_enums)):
        changes.append(SchemaChange(type="drop_enum", table=name, destructive=True))

    for name in sorted(set(old_enums) & set(new_enums)):
        if old_enums[name].values != new_enums[name].values:
            changes.append(SchemaChange(type="modify_enum", table=name))

    return changes


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _rel_key(r: object) -> str:  # type: ignore[return]
    return f"{r.from_table}.{r.from_column}->{r.to_table}.{r.to_column}"  # type: ignore[attr-defined]


def _diff_columns(
    table_name: str, old_t: Table, new_t: Table
) -> list[SchemaChange]:
    changes: list[SchemaChange] = []
    old_cols = {c.name: c for c in old_t.columns}
    new_cols = {c.name: c for c in new_t.columns}

    # Added columns
    for col_name in sorted(set(new_cols) - set(old_cols)):
        col = new_cols[col_name]
        changes.append(
            SchemaChange(type="add_column", table=table_name, column=col_name)
        )
        if col.index and not col.primary_key:
            changes.append(
                SchemaChange(type="add_index", table=table_name, column=col_name)
            )

    # Dropped columns
    for col_name in sorted(set(old_cols) - set(new_cols)):
        changes.append(
            SchemaChange(
                type="drop_column",
                table=table_name,
                column=col_name,
                destructive=True,
            )
        )

    # Modified columns
    for col_name in sorted(set(old_cols) & set(new_cols)):
        old_c = old_cols[col_name]
        new_c = new_cols[col_name]

        mod_details = _column_diff(old_c, new_c)
        if mod_details:
            destructive = "type" in mod_details or "primary_key" in mod_details
            changes.append(
                SchemaChange(
                    type="modify_column",
                    table=table_name,
                    column=col_name,
                    details=mod_details,
                    destructive=destructive,
                )
            )

        # Index added / removed on an existing column
        old_idx = old_c.index and not old_c.primary_key
        new_idx = new_c.index and not new_c.primary_key
        if not old_idx and new_idx:
            changes.append(
                SchemaChange(type="add_index", table=table_name, column=col_name)
            )
        elif old_idx and not new_idx:
            changes.append(
                SchemaChange(type="drop_index", table=table_name, column=col_name)
            )

    return changes


def _column_diff(old_c: Column, new_c: Column) -> dict:
    """Return {field: (old_val, new_val)} for every attribute that changed."""
    diff: dict = {}
    for attr in ("type", "nullable", "default", "unique", "max_length", "foreign_key", "primary_key"):
        old_val = getattr(old_c, attr)
        new_val = getattr(new_c, attr)
        if old_val != new_val:
            diff[attr] = (old_val, new_val)
    return diff
