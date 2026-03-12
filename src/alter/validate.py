"""Schema validation for AlterSchema.

Call ``validate_schema(schema)`` to get a list of ``ValidationIssue`` objects.
Issues have severity ``"error"``, ``"warning"``, or ``"info"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from alter.schema import AlterSchema
from alter.types import TYPE_MAP


# ---------------------------------------------------------------------------
# Identifier validation helpers
# ---------------------------------------------------------------------------

# Valid SQL / Python identifier: starts with letter or underscore, followed by
# any combination of letters, digits, and underscores.
_VALID_IDENTIFIER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

# Common SQL reserved words that break DDL if used unquoted as identifiers.
# Not exhaustive — covers the keywords most likely to appear as accidental names.
_SQL_RESERVED: frozenset[str] = frozenset({
    "select", "from", "where", "insert", "update", "delete",
    "create", "drop", "alter", "table", "column", "index",
    "primary", "foreign", "key", "references", "constraint",
    "unique", "not", "null", "default", "check", "in", "and",
    "or", "order", "group", "by", "having", "limit", "offset",
    "join", "on", "as", "set", "values", "into",
})


Severity = Literal["error", "warning", "info"]


@dataclass
class ValidationIssue:
    """A single validation finding."""

    severity: Severity
    table: str
    message: str
    column: str | None = None


def _parse_fk_reference(fk: str) -> tuple[str | None, str, str]:
    """Parse a ``foreign_key`` string into ``(schema, table, column)``.

    Accepts both forms used by SQLAlchemy / SQLModel:

    * ``"table.column"``         → ``(None, "table", "column")``
    * ``"schema.table.column"``  → ``("schema", "table", "column")``

    Returns ``(None, "", "")`` for any other format (triggers a format error
    in the caller).
    """
    parts = fk.split(".")
    if len(parts) == 2:
        return None, parts[0], parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return None, "", ""


def validate_schema(schema: AlterSchema) -> list[ValidationIssue]:
    """Return all validation issues found in *schema*.

    Errors must be fixed before committing. Warnings are advisory.
    Info messages are suggestions.
    """
    issues: list[ValidationIssue] = []

    # Duplicate table names
    seen_table_names: set[str] = set()
    for table in schema.tables:
        if table.name in seen_table_names:
            issues.append(ValidationIssue(
                severity="error",
                table=table.name,
                message=f"Duplicate table name '{table.name}'",
            ))
        else:
            seen_table_names.add(table.name)

    # Build lookup structures
    table_names = {t.name for t in schema.tables}
    table_col_map: dict[str, set[str]] = {
        t.name: {c.name for c in t.columns} for t in schema.tables
    }
    enum_names = {e.name for e in schema.enums}
    known_types = set(TYPE_MAP.keys()) | enum_names

    for table in schema.tables:
        # Empty table name
        if not table.name or not table.name.strip():
            issues.append(ValidationIssue(
                severity="error", table=table.name or "<empty>",
                message="Table name is empty",
            ))
            continue

        # Invalid SQL identifier
        if not _VALID_IDENTIFIER_RE.match(table.name):
            issues.append(ValidationIssue(
                severity="error", table=table.name,
                message=(
                    f"Table name '{table.name}' is not a valid SQL identifier "
                    f"(must start with a letter or underscore and contain only "
                    f"alphanumeric characters and underscores)"
                ),
            ))
        elif table.name.lower() in _SQL_RESERVED:
            issues.append(ValidationIssue(
                severity="warning", table=table.name,
                message=(
                    f"Table name '{table.name}' is a SQL reserved word — "
                    f"may cause issues with DDL export and some databases"
                ),
            ))

        col_names: list[str] = []
        has_pk = False

        for col in table.columns:
            # Empty column name
            if not col.name or not col.name.strip():
                issues.append(ValidationIssue(
                    severity="error", table=table.name,
                    message="Column name is empty",
                ))
                continue

            # Invalid SQL identifier
            if not _VALID_IDENTIFIER_RE.match(col.name):
                issues.append(ValidationIssue(
                    severity="error", table=table.name, column=col.name,
                    message=(
                        f"Column name '{col.name}' is not a valid SQL identifier "
                        f"(must start with a letter or underscore and contain only "
                        f"alphanumeric characters and underscores)"
                    ),
                ))
            elif col.name.lower() in _SQL_RESERVED:
                issues.append(ValidationIssue(
                    severity="warning", table=table.name, column=col.name,
                    message=(
                        f"Column name '{col.name}' is a SQL reserved word — "
                        f"may cause issues with DDL export and some databases"
                    ),
                ))

            # Duplicate column names
            if col.name in col_names:
                issues.append(ValidationIssue(
                    severity="error", table=table.name, column=col.name,
                    message=f"Duplicate column name '{col.name}'",
                ))
            col_names.append(col.name)

            # Unknown type
            if col.type not in known_types:
                issues.append(ValidationIssue(
                    severity="error", table=table.name, column=col.name,
                    message=(
                        f"Unknown type '{col.type}' — must be a built-in alter type "
                        f"or a defined enum name"
                    ),
                ))

            # Primary key tracking
            if col.primary_key:
                has_pk = True

            # Dangling foreign key reference
            if col.foreign_key:
                _fk_schema, ref_table, ref_col = _parse_fk_reference(col.foreign_key)
                if not ref_table or not ref_col:
                    issues.append(ValidationIssue(
                        severity="error", table=table.name, column=col.name,
                        message=(
                            f"Foreign key '{col.foreign_key}' must be in "
                            f"'table.column' or 'schema.table.column' format"
                        ),
                    ))
                else:
                    # Table lookup uses the bare table name regardless of schema prefix.
                    if ref_table not in table_names:
                        issues.append(ValidationIssue(
                            severity="error", table=table.name, column=col.name,
                            message=(
                                f"Foreign key references unknown table '{ref_table}'"
                            ),
                        ))
                    elif ref_col not in table_col_map.get(ref_table, set()):
                        issues.append(ValidationIssue(
                            severity="error", table=table.name, column=col.name,
                            message=(
                                f"Foreign key references unknown column "
                                f"'{ref_table}.{ref_col}'"
                            ),
                        ))

                # Suggest index on FK columns
                if not col.index and not col.primary_key:
                    issues.append(ValidationIssue(
                        severity="info", table=table.name, column=col.name,
                        message=(
                            f"FK column '{col.name}' has no index — "
                            f"consider adding index=True for query performance"
                        ),
                    ))

        # Missing primary key
        if not has_pk:
            issues.append(ValidationIssue(
                severity="warning", table=table.name,
                message=f"Table '{table.name}' has no primary key",
            ))

    # Validate relation objects
    for rel in schema.relations:
        if rel.from_table not in table_names:
            issues.append(ValidationIssue(
                severity="error", table=rel.from_table,
                message=f"Relation references unknown from_table '{rel.from_table}'",
            ))
        if rel.to_table not in table_names:
            issues.append(ValidationIssue(
                severity="error", table=rel.from_table,
                message=f"Relation references unknown to_table '{rel.to_table}'",
            ))

    return issues
