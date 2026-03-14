"""SQL DDL importer — parses CREATE TABLE statements into AlterSchema.

Uses ``sqlparse`` for tokenisation. Handles standard Postgres DDL as
produced by pgAdmin, DBeaver, DataGrip, and other tooling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import sqlparse
from sqlparse.sql import Parenthesis, Statement
from sqlparse.tokens import Keyword, DDL

from alter.schema import AlterSchema, Column, Relation, Table
from alter.types import sql_to_alter
from alter.layout import grid_position


@dataclass
class ImportResult:
    """Return value of :func:`import_sql`.

    Attributes:
        schema:   The parsed :class:`~alter.schema.AlterSchema`.
        warnings: Human-readable messages about columns whose SQL types were
                  not recognised and defaulted to ``"string"``.
    """

    schema: AlterSchema
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_DEFAULT_FILE_PATH = "app/models.py"


def import_sql(
    sql: str,
    orm: str = "sqlmodel",
    file_path: str = _DEFAULT_FILE_PATH,
) -> ImportResult:
    """Parse *sql* (one or more ``CREATE TABLE`` statements) → :class:`ImportResult`.

    Args:
        sql:       Raw SQL DDL string.
        orm:       ORM to record in the resulting schema (default ``"sqlmodel"``).
        file_path: Relative path to the ORM model file that will hold the
                   generated code.  Defaults to ``"app/models.py"``.  Callers
                   should pass ``schema.metadata.sqlmodel_module`` so that
                   ``alter apply`` writes to the correct location.

    Returns:
        An :class:`ImportResult` whose ``schema`` contains tables, columns,
        and relations extracted from the DDL (table positions are auto-assigned
        on a grid) and whose ``warnings`` lists any columns whose SQL type was
        not recognised and defaulted to ``"string"``.
    """
    tables: list[Table] = []
    relations: list[Relation] = []
    all_warnings: list[str] = []

    statements = sqlparse.parse(sql)
    table_index = 0

    for stmt in statements:
        if not _is_create_table(stmt):
            continue

        table_name, columns, fks, warnings = _parse_create_table(stmt)
        if table_name is None:
            continue

        all_warnings.extend(warnings)

        tables.append(
            Table(
                name=table_name,
                file_path=file_path,
                position=grid_position(table_index),
                columns=columns,
            )
        )
        table_index += 1

        for fk in fks:
            rel_name = f"{table_name}_{fk['from_col']}_fk"
            on_delete = fk.get("on_delete") or "CASCADE"
            relations.append(
                Relation(
                    name=rel_name,
                    from_table=table_name,
                    from_column=fk["from_col"],
                    to_table=fk["to_table"],
                    to_column=fk["to_col"],
                    on_delete=on_delete,
                )
            )

    return ImportResult(
        schema=AlterSchema(orm=orm, tables=tables, relations=relations),
        warnings=all_warnings,
    )


# ---------------------------------------------------------------------------
# Parsing internals
# ---------------------------------------------------------------------------


def _is_create_table(stmt: Statement) -> bool:
    """Return True if *stmt* is a CREATE TABLE statement."""
    tokens = [t for t in stmt.tokens if not t.is_whitespace]
    if len(tokens) < 2:
        return False
    return (
        tokens[0].ttype is DDL
        and tokens[0].normalized.upper() == "CREATE"
        and tokens[1].ttype is Keyword
        and tokens[1].normalized.upper() == "TABLE"
    )


# Matches: table_name (rest)  or  schema.table_name (rest)
_TABLE_NAME_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
    r'(?:"?(\w+)"?\.)?"?(\w+)"?',
    re.IGNORECASE,
)


def _parse_create_table(
    stmt: Statement,
) -> tuple[str | None, list[Column], list[dict], list[str]]:
    """Extract table name, columns, FK constraints, and warnings from a statement."""
    raw = str(stmt).strip()

    m = _TABLE_NAME_RE.search(raw)
    if not m:
        return None, [], [], []
    table_name = m.group(2)

    # Extract the body between the first ( and matching )
    paren = _find_paren(stmt)
    if paren is None:
        return table_name, [], [], []

    body = str(paren)[1:-1]  # strip outer ( )
    column_defs = _split_column_defs(body)

    columns: list[Column] = []
    fks: list[dict] = []
    warnings: list[str] = []
    pk_cols: set[str] = set()

    # First pass: collect table-level PRIMARY KEY constraints
    for defn in column_defs:
        defn_stripped = defn.strip()
        upper = defn_stripped.upper()
        if upper.startswith("PRIMARY KEY"):
            pk_cols.update(_extract_col_list(defn_stripped))
        elif upper.startswith("FOREIGN KEY"):
            fk = _parse_table_fk(defn_stripped, table_name)
            if fk:
                fks.append(fk)

    # Second pass: parse column definitions
    for defn in column_defs:
        defn_stripped = defn.strip()
        upper = defn_stripped.upper()
        if (
            upper.startswith("PRIMARY KEY")
            or upper.startswith("FOREIGN KEY")
            or upper.startswith("UNIQUE")
            or upper.startswith("CHECK")
            or upper.startswith("CONSTRAINT")
            or upper.startswith("INDEX")
            or upper.startswith("KEY")
        ):
            # Handle CONSTRAINT ... FOREIGN KEY at table level
            if "FOREIGN KEY" in upper and "CONSTRAINT" in upper:
                fk = _parse_table_fk(defn_stripped, table_name)
                if fk:
                    fks.append(fk)
            continue

        col, col_warnings = _parse_column_def(defn_stripped, pk_cols, table_name)
        if col:
            columns.append(col)
            warnings.extend(col_warnings)
            # Inline REFERENCES → add FK
            fk = _extract_inline_fk(defn_stripped, col.name)
            if fk:
                fks.append(fk)

    return table_name, columns, fks, warnings


def _find_paren(stmt: Statement) -> Optional[Parenthesis]:
    for token in stmt.tokens:
        if isinstance(token, Parenthesis):
            return token
    return None


def _split_column_defs(body: str) -> list[str]:
    """Split the CREATE TABLE body into individual column/constraint definitions.

    Respects nested parentheses (e.g. DEFAULT (now()), CHECK (...)).
    """
    defs: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            defs.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        last = "".join(current).strip()
        if last:
            defs.append(last)
    return [d for d in defs if d]


_INLINE_REFS_RE = re.compile(
    r'REFERENCES\s+"?(\w+)"?\s*\(\s*"?(\w+)"?\s*\)'
    r'(?:\s+ON\s+DELETE\s+(\w+(?:\s+\w+)?))?',
    re.IGNORECASE,
)


def _extract_inline_fk(defn: str, col_name: str) -> dict | None:
    m = _INLINE_REFS_RE.search(defn)
    if not m:
        return None
    return {
        "from_col": col_name,
        "to_table": m.group(1),
        "to_col": m.group(2),
        "on_delete": m.group(3).strip().upper() if m.group(3) else None,
    }


_TABLE_FK_RE = re.compile(
    r'FOREIGN\s+KEY\s*\(\s*"?(\w+)"?\s*\)\s*'
    r'REFERENCES\s+"?(\w+)"?\s*\(\s*"?(\w+)"?\s*\)'
    r'(?:\s+ON\s+DELETE\s+(\w+(?:\s+\w+)?))?',
    re.IGNORECASE,
)


def _parse_table_fk(defn: str, table_name: str) -> dict | None:
    m = _TABLE_FK_RE.search(defn)
    if not m:
        return None
    return {
        "from_col": m.group(1),
        "to_table": m.group(2),
        "to_col": m.group(3),
        "on_delete": m.group(4).strip().upper() if m.group(4) else None,
    }


def _extract_col_list(defn: str) -> list[str]:
    """Extract column names from PRIMARY KEY (col1, col2)."""
    m = re.search(r'\(([^)]+)\)', defn)
    if not m:
        return []
    return [c.strip().strip('"') for c in m.group(1).split(",")]


# Matches: col_name  TYPE[(size)] [constraints...]
_COL_DEF_RE = re.compile(
    r'^"?(\w+)"?\s+([A-Za-z][A-Za-z0-9 _]*)(?:\s*\(([^)]*)\))?(.*)$',
    re.DOTALL,
)

# Constraint keywords that can appear after the SQL type; used to split
# accidentally-consumed keywords out of the type token captured by _COL_DEF_RE.
_CONSTRAINT_SPLIT_RE = re.compile(
    r'\b(PRIMARY|NOT\s+NULL|UNIQUE|DEFAULT|REFERENCES|CHECK|CONSTRAINT)\b',
    re.IGNORECASE,
)

_DEFAULT_RE = re.compile(
    r"DEFAULT\s+"
    r"("
    r"'(?:[^']|'')*'"               # single-quoted string (handles '' escapes)
    r"|"
    r"\((?:[^()]*|\([^()]*\))*\)"   # parenthesized expression, one level of nesting
    r"|"
    r"\S+"                          # simple non-whitespace token
    r")",
    re.IGNORECASE,
)


def _parse_column_def(
    defn: str, pk_cols: set[str], table_name: str = ""
) -> tuple[Column | None, list[str]]:
    """Parse one column definition line into a ``Column`` and any warnings."""
    m = _COL_DEF_RE.match(defn)
    if not m:
        return None, []

    col_name = m.group(1)
    raw_type_full = m.group(2).strip()
    size_str = m.group(3)
    rest_tail = m.group(4) if m.group(4) else ""

    # The greedy type group may accidentally consume constraint keywords
    # (e.g. "UUID PRIMARY KEY").  Split at the first constraint keyword.
    csplit = _CONSTRAINT_SPLIT_RE.search(raw_type_full)
    if csplit:
        raw_type = raw_type_full[: csplit.start()].strip()
        rest = raw_type_full[csplit.start():].strip() + " " + rest_tail
    else:
        raw_type = raw_type_full
        rest = rest_tail

    rest_upper = rest.upper()

    # Resolve type — warn and fall back to "string" for unrecognised SQL types.
    warnings: list[str] = []
    try:
        alter_type = sql_to_alter(raw_type.upper())
    except KeyError:
        alter_type = "string"
        location = f"{table_name}.{col_name}" if table_name else col_name
        warnings.append(
            f"Unknown SQL type '{raw_type}' for column '{location}'"
            f" — defaulting to 'string'"
        )

    # max_length from size
    max_length: int | None = None
    if size_str and alter_type in ("string",):
        try:
            max_length = int(size_str.split(",")[0].strip())
        except ValueError:
            pass

    # Constraints
    primary_key = col_name in pk_cols or "PRIMARY KEY" in rest_upper
    nullable = "NOT NULL" not in rest_upper and not primary_key
    unique = "UNIQUE" in rest_upper

    # DEFAULT value
    # Search `defn` (the original full column definition text) rather than the
    # processed `rest`.  _COL_DEF_RE's optional size group `(?:\s*\(([^)]*)\))?`
    # can accidentally consume a parenthesised DEFAULT expression such as
    # `DEFAULT (1 + 2)`, stripping the parens before `rest` is assembled.
    # Searching the raw `defn` bypasses that mangling.
    default: str | None = None
    dm = _DEFAULT_RE.search(defn)
    if dm:
        raw_default = dm.group(1).strip()
        raw_upper_bare = raw_default.upper().rstrip("()")
        if raw_upper_bare in ("NOW", "CURRENT_TIMESTAMP"):
            default = "utcnow"
        elif raw_upper_bare in ("GEN_RANDOM_UUID", "UUID_GENERATE_V4"):
            default = "uuid4"
        elif raw_default.startswith("'") and raw_default.endswith("'"):
            default = raw_default[1:-1]
        else:
            default = raw_default

    # Skip REFERENCES part in the type string
    if "REFERENCES" in raw_type.upper():
        return None, warnings

    return Column(
        name=col_name,
        type=alter_type,
        primary_key=primary_key,
        nullable=nullable,
        unique=unique,
        default=default,
        max_length=max_length,
    ), warnings
