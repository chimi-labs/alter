"""Canonical type map: Python ↔ .alter ↔ SQL DDL.

This module is the single source of truth for all type conversions. Every
component (parser, generator, SQL importer, SQL exporter) imports from here.
No type logic lives anywhere else.

Built-in .alter types are always lowercase (``"string"``, ``"uuid"``, ``"int"``).
Enum type names are PascalCase (``"Role"``, ``"SubscriptionStatus"``). When a
column type does not appear in ``TYPE_MAP``, it is treated as an enum reference.

Conversion directions::

    Python type string  ←→  .alter type string  ←→  SQL DDL type string

Examples::

    python_to_alter("uuid.UUID")   → "uuid"
    alter_to_python("uuid")        → "uuid.UUID"
    alter_to_sql("string", 255)    → "VARCHAR(255)"
    sql_to_alter("TEXT")           → "text"
"""

from __future__ import annotations

from typing import NamedTuple


class TypeEntry(NamedTuple):
    """Maps one .alter type to its Python and SQL equivalents."""

    python_type: str   # e.g. "uuid.UUID", "str", "int"
    sql_type: str      # e.g. "UUID", "VARCHAR", "INTEGER"


# ---------------------------------------------------------------------------
# Primary type map: .alter type key → (Python type, SQL base type)
# ---------------------------------------------------------------------------

TYPE_MAP: dict[str, TypeEntry] = {
    "uuid":     TypeEntry("uuid.UUID",  "UUID"),
    "string":   TypeEntry("str",        "VARCHAR"),   # use max_length → VARCHAR(N), else TEXT
    "text":     TypeEntry("str",        "TEXT"),
    "int":      TypeEntry("int",        "INTEGER"),
    "bigint":   TypeEntry("int",        "BIGINT"),
    "float":    TypeEntry("float",      "DOUBLE PRECISION"),
    "decimal":  TypeEntry("Decimal",    "NUMERIC"),
    "bool":     TypeEntry("bool",       "BOOLEAN"),
    "datetime": TypeEntry("datetime",   "TIMESTAMPTZ"),
    "date":     TypeEntry("date",       "DATE"),
    "time":     TypeEntry("time",       "TIME"),
    "json":     TypeEntry("dict",       "JSONB"),
    "bytes":    TypeEntry("bytes",      "BYTEA"),
}

# ---------------------------------------------------------------------------
# Reverse maps for fast lookup
# ---------------------------------------------------------------------------

# Python type → .alter type (primary mapping only, qualified names)
_PYTHON_TO_ALTER: dict[str, str] = {
    entry.python_type: alter_type
    for alter_type, entry in TYPE_MAP.items()
}

# Additional Python aliases that map to alter types
_PYTHON_ALIASES: dict[str, str] = {
    "str":               "string",
    "int":               "int",
    "float":             "float",
    "bool":              "bool",
    "dict":              "json",
    "bytes":             "bytes",
    "Decimal":           "decimal",
    "datetime":          "datetime",
    "date":              "date",
    "time":              "time",
    "UUID":              "uuid",
    # common qualified names
    "datetime.datetime": "datetime",
    "datetime.date":     "date",
    "datetime.time":     "time",
    "decimal.Decimal":   "decimal",
}

# SQL type → .alter type (uppercase keys; prefix matching handled in sql_to_alter)
_SQL_TO_ALTER: dict[str, str] = {
    "UUID":                      "uuid",
    "VARCHAR":                   "string",
    "CHARACTER VARYING":         "string",
    "CHAR":                      "string",
    "TEXT":                      "text",
    "INTEGER":                   "int",
    "INT":                       "int",
    "INT4":                      "int",
    "INT8":                      "bigint",
    "BIGINT":                    "bigint",
    "BIGSERIAL":                 "bigint",
    "SERIAL":                    "int",
    "FLOAT":                     "float",
    "REAL":                      "float",
    "DOUBLE PRECISION":          "float",
    "NUMERIC":                   "decimal",
    "DECIMAL":                   "decimal",
    "BOOLEAN":                   "bool",
    "BOOL":                      "bool",
    "TIMESTAMP WITH TIME ZONE":  "datetime",
    "TIMESTAMPTZ":               "datetime",
    "TIMESTAMP":                 "datetime",
    "DATE":                      "date",
    "TIME":                      "time",
    "JSONB":                     "json",
    "JSON":                      "json",
    "BYTEA":                     "bytes",
}


# ---------------------------------------------------------------------------
# Conversion functions
# ---------------------------------------------------------------------------


def alter_to_python(alter_type: str) -> str:
    """Convert an .alter type string to its Python type annotation string.

    For enum types (PascalCase), returns the name as-is since enum classes
    are defined by the user and resolved at code generation time.

    Args:
        alter_type: An .alter built-in type key (e.g. ``"uuid"``, ``"string"``).

    Returns:
        Python type annotation string (e.g. ``"uuid.UUID"``, ``"str"``).

    Raises:
        KeyError: if the type is not a known built-in and is not PascalCase.
    """
    if alter_type in TYPE_MAP:
        return TYPE_MAP[alter_type].python_type
    # PascalCase enum name → pass through
    if alter_type and alter_type[0].isupper():
        return alter_type
    raise KeyError(
        f"Unknown .alter type '{alter_type}'. "
        "Expected a built-in type or a PascalCase enum name."
    )


def alter_to_sql(alter_type: str, max_length: int | None = None) -> str:
    """Convert an .alter type string to its SQL DDL type string.

    For ``"string"`` types, ``max_length`` controls whether to emit
    ``VARCHAR(N)`` or ``TEXT``.

    Args:
        alter_type: An .alter built-in type key (e.g. ``"string"``, ``"uuid"``).
        max_length: Optional maximum character length (applies to ``"string"``).

    Returns:
        SQL DDL type string (e.g. ``"UUID"``, ``"VARCHAR(255)"``, ``"TEXT"``).

    Raises:
        KeyError: if the type is not a known built-in.
    """
    if alter_type not in TYPE_MAP:
        if alter_type and alter_type[0].isupper():
            # Enum type — SQL representation uses the enum name
            return alter_type
        raise KeyError(f"Unknown .alter type '{alter_type}'.")

    sql = TYPE_MAP[alter_type].sql_type
    if alter_type == "string":
        if max_length is not None:
            return f"VARCHAR({max_length})"
        return "TEXT"
    return sql


def python_to_alter(python_type: str) -> str:
    """Convert a Python type annotation string to an .alter type string.

    Handles ``Optional[X]`` by stripping the ``Optional`` wrapper. Handles
    common qualified names (``uuid.UUID``, ``datetime.datetime``, etc.).
    For user-defined enum types (PascalCase class names), returns the class
    name unchanged — it becomes an enum reference in the .alter file.

    Args:
        python_type: A Python type annotation as a string (e.g. ``"uuid.UUID"``,
                     ``"Optional[str]"``, ``"str"``).

    Returns:
        An .alter type string (e.g. ``"uuid"``, ``"string"``), or the original
        PascalCase name for enum references.

    Raises:
        KeyError: if the type cannot be mapped and is not a PascalCase name.
    """
    # Strip Optional[...] wrapper
    stripped = _strip_optional(python_type)

    # Check aliases first — they define the preferred mapping for ambiguous types
    # (e.g. "str" → "string" not "text", "int" → "int" not "bigint")
    if stripped in _PYTHON_ALIASES:
        return _PYTHON_ALIASES[stripped]

    # Check primary reverse map (qualified names like "uuid.UUID", "datetime.datetime")
    if stripped in _PYTHON_TO_ALTER:
        return _PYTHON_TO_ALTER[stripped]

    # PascalCase → treat as enum reference, return name unchanged
    if stripped and stripped[0].isupper():
        return stripped

    raise KeyError(
        f"Cannot map Python type '{python_type}' to an .alter type. "
        "Add a mapping to types.TYPE_MAP or register it as an enum."
    )


def sql_to_alter(sql_type: str) -> str:
    """Convert a SQL DDL type string to an .alter type string.

    Normalises the SQL type to uppercase and does prefix matching so that
    ``VARCHAR(255)`` matches ``VARCHAR``.

    Args:
        sql_type: A SQL DDL type string (e.g. ``"VARCHAR(255)"``, ``"UUID"``).

    Returns:
        An .alter type string (e.g. ``"string"``, ``"uuid"``).

    Raises:
        KeyError: if the SQL type is not recognised.
    """
    normalised = sql_type.strip().upper()
    # Strip parenthesised length/precision suffix: VARCHAR(255) → VARCHAR
    base = normalised.split("(")[0].strip()

    if base in _SQL_TO_ALTER:
        return _SQL_TO_ALTER[base]

    # Try full normalised string (handles multi-word types)
    if normalised in _SQL_TO_ALTER:
        return _SQL_TO_ALTER[normalised]

    raise KeyError(
        f"Cannot map SQL type '{sql_type}' to an .alter type. "
        "Consider adding it to types._SQL_TO_ALTER."
    )


def is_enum_type(alter_type: str) -> bool:
    """Return True if an .alter type string is an enum reference (PascalCase).

    Enum references are column types that are not in ``TYPE_MAP`` and start
    with an uppercase letter. The generator resolves them by looking up the
    corresponding ``EnumDef`` in the ``AlterSchema.enums`` list.

    Args:
        alter_type: An .alter type string.

    Returns:
        ``True`` if the type is not a built-in and starts with an uppercase letter.
    """
    return alter_type not in TYPE_MAP and bool(alter_type) and alter_type[0].isupper()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_optional(python_type: str) -> str:
    """Strip ``Optional[...]`` or ``Union[X, None]`` wrapper from a type string."""
    t = python_type.strip()
    if t.startswith("Optional[") and t.endswith("]"):
        return t[len("Optional["):-1].strip()
    # Handle Union[X, None] or Union[None, X]
    if t.startswith("Union[") and t.endswith("]"):
        inner = t[len("Union["):-1]
        parts = [p.strip() for p in inner.split(",")]
        non_none = [p for p in parts if p != "None"]
        if len(non_none) == 1:
            return non_none[0]
    return t
