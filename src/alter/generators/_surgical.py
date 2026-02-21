"""Surgical class update utilities for ORM model generators.

Provides helpers to compare and patch Python class source code at the
Field()-line level, preserving all non-schema content (docstrings,
``Relationship()`` definitions, inline comments, hand-written kwarg ordering).

Public entry point: ``surgical_update_class(class_source, schema_field_lines)``.
"""

from __future__ import annotations

import ast
import re


# ---------------------------------------------------------------------------
# AST-based field statement extraction
# ---------------------------------------------------------------------------

def _get_field_stmts(class_source: str) -> list[tuple[str, int, int]]:
    """Return (col_name, start_lineno, end_lineno) for each AnnAssign that
    calls ``Field`` or ``mapped_column`` in the first class found in *class_source*.

    Line numbers are 1-indexed and relative to *class_source*.
    Handles multi-line Field() calls correctly because AST tracks end_lineno.
    """
    try:
        tree = ast.parse(class_source)
    except SyntaxError:
        return []

    cls = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)),
        None,
    )
    if cls is None:
        return []

    results: list[tuple[str, int, int]] = []
    for stmt in cls.body:
        if not isinstance(stmt, ast.AnnAssign):
            continue
        if not isinstance(stmt.value, ast.Call):
            continue
        func = stmt.value.func
        fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if fname in ("Field", "mapped_column"):
            col = stmt.target.id if isinstance(stmt.target, ast.Name) else None
            if col:
                results.append((col, stmt.lineno, stmt.end_lineno))
    return results


# ---------------------------------------------------------------------------
# Semantic Field() kwargs comparison
# ---------------------------------------------------------------------------

def _parse_field_kwargs(line: str) -> dict[str, str] | None:
    """Parse ``Field(pk=True, default_factory=uuid.uuid4)`` from *line* into a
    ``{kwarg_name: ast.unparse(value)}`` dict.  Returns ``None`` on failure.

    Positional args are stored as ``__pos_0``, ``__pos_1``, … so they still
    participate in equality comparison.
    """
    for sep in ("= Field(", "= mapped_column("):
        pos = line.find(sep)
        if pos != -1:
            rhs = line[pos + 2:].strip()
            break
    else:
        return None

    try:
        tree = ast.parse(rhs, mode="eval")
    except SyntaxError:
        return None

    if not isinstance(tree.body, ast.Call):
        return None

    kw: dict[str, str] = {}
    for i, arg in enumerate(tree.body.args):
        kw[f"__pos_{i}"] = ast.unparse(arg)
    for k in tree.body.keywords:
        if k.arg:
            kw[k.arg] = ast.unparse(k.value)
    return kw


def _field_lhs(line: str) -> str:
    """Return the ``name: Type`` portion of a field line (normalised whitespace)."""
    for sep in ("= Field(", "= mapped_column("):
        pos = line.find(sep)
        if pos != -1:
            return " ".join(line[:pos].split())
    return line.strip()


def _field_kwargs_equal(existing: str, new: str) -> bool:
    """Semantic equality: same ``name: Type`` LHS **and** same Field() kwargs
    (order-independent).  Falls back to stripped string comparison if AST
    parsing fails.
    """
    if existing.rstrip() == new.rstrip():
        return True
    if _field_lhs(existing) != _field_lhs(new):
        return False
    ekw = _parse_field_kwargs(existing)
    nkw = _parse_field_kwargs(new)
    if ekw is None or nkw is None:
        return existing.rstrip() == new.rstrip()
    return ekw == nkw


# ---------------------------------------------------------------------------
# Update-needed check
# ---------------------------------------------------------------------------

def _col_name_from_generated(line: str) -> str | None:
    """Extract the column name from a generated ``    name: Type = Field(...)`` line."""
    m = re.match(r"\s*(\w+)\s*:", line)
    return m.group(1) if m else None


def _class_needs_update(class_source: str, schema_field_lines: list[str]) -> bool:
    """Return ``True`` if *class_source* needs to be patched to match
    *schema_field_lines*.

    Rules:
    - If any schema column is absent from the class → needs update.
    - If any schema column's Field() kwargs differ semantically → needs update.
    - kwarg-order-only differences do **not** trigger an update.
    - Extra columns in the file that are not in the schema → ignored (no update).
    - Non-schema lines (docstrings, Relationship, comments) → ignored entirely.
    """
    stmts = _get_field_stmts(class_source)
    src_lines = class_source.splitlines(keepends=True)

    # Build {col_name: full existing field text} from AST-located ranges
    existing: dict[str, str] = {}
    for col_name, start, end in stmts:
        existing[col_name] = "".join(src_lines[start - 1 : end]).rstrip()

    for line in schema_field_lines:
        col_name = _col_name_from_generated(line)
        if col_name is None:
            continue
        if col_name not in existing:
            return True  # new column
        if not _field_kwargs_equal(existing[col_name], line):
            return True  # changed column

    return False


# ---------------------------------------------------------------------------
# Surgical patcher
# ---------------------------------------------------------------------------

def _surgical_patch_class(
    class_source: str,
    schema_field_lines: list[str],
) -> list[str]:
    """Return a patched version of *class_source* as a list of lines.

    Algorithm:
    1. Use ``_get_field_stmts`` to locate each existing Field() statement by
       line range (handles multi-line calls).
    2. Walk source lines:
       - Field() lines whose kwargs match the schema → emitted verbatim (preserves
         hand-written kwarg order).
       - Field() lines whose kwargs differ → replaced with the schema version.
       - All other lines (docstring, Relationship, comments, __tablename__, blanks,
         class header) → emitted verbatim.
    3. New schema columns (absent from the file) are inserted immediately after
       the last existing Field() line, which places them before any Relationship()
       section.
    """
    stmts = _get_field_stmts(class_source)
    src_lines = class_source.splitlines(keepends=True)

    # Build {col_name: schema_line} for lookup
    schema_map: dict[str, str] = {}
    schema_order: list[str] = []
    for line in schema_field_lines:
        col_name = _col_name_from_generated(line)
        if col_name is not None:
            schema_map[col_name] = line
            schema_order.append(col_name)

    existing_col_names: set[str] = {col for col, _, _ in stmts}

    # Map each field stmt start-line (0-indexed) to its end-line (exclusive, 0-indexed)
    # so we can detect multi-line spans during the walk.
    stmt_ranges: dict[int, tuple[str, int]] = {
        start - 1: (col, end)
        for col, start, end in stmts
    }  # {start_0idx: (col_name, end_0idx_exclusive)}

    result: list[str] = []
    last_field_result_idx: int = -1
    i = 0

    while i < len(src_lines):
        if i in stmt_ranges:
            col_name, end = stmt_ranges[i]
            # Collect the full existing field text (may span multiple lines)
            existing_text = "".join(src_lines[i:end]).rstrip()
            if col_name in schema_map and not _field_kwargs_equal(existing_text, schema_map[col_name]):
                # Replace with schema version
                result.append(schema_map[col_name].rstrip() + "\n")
            else:
                # Keep verbatim (unchanged field — preserves kwarg order)
                for ln in src_lines[i:end]:
                    result.append(ln)
            last_field_result_idx = len(result) - 1
            i = end
        else:
            result.append(src_lines[i])
            i += 1

    # Insert new columns after the last Field() line
    new_cols = [name for name in schema_order if name not in existing_col_names]
    if new_cols:
        insert_at = last_field_result_idx + 1 if last_field_result_idx >= 0 else len(result)
        for col_name in new_cols:
            result.insert(insert_at, schema_map[col_name].rstrip() + "\n")
            insert_at += 1

    return result


# ---------------------------------------------------------------------------
# Enum class surgical update (preserves docstrings / comments)
# ---------------------------------------------------------------------------

def _get_enum_value_lines(class_source: str) -> dict[str, str]:
    """Return {value_name: line_text} for each ``name = "value"`` assignment
    in the first enum class in *class_source*."""
    try:
        tree = ast.parse(class_source)
    except SyntaxError:
        return {}
    cls = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)),
        None,
    )
    if cls is None:
        return {}
    src_lines = class_source.splitlines(keepends=True)
    result: dict[str, str] = {}
    for stmt in cls.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            continue
        if not isinstance(stmt.value, ast.Constant):
            continue
        name = stmt.targets[0].id
        line = "".join(src_lines[stmt.lineno - 1 : stmt.end_lineno])
        result[name] = line.rstrip()
    return result


def surgical_update_enum_class(
    class_source: str,
    schema_value_lines: list[str],
) -> list[str] | None:
    """Enum-specific surgical update — preserves docstrings and comments.

    Args:
        class_source: Full source text of the existing enum class.
        schema_value_lines: Generated ``    name = "value"`` lines from
            ``_enum_class_source()``.  No trailing newline required.

    Returns:
        ``None`` if the class is already up-to-date.
        A patched ``list[str]`` otherwise.
    """
    # Build expected value set from schema lines
    schema_values: dict[str, str] = {}
    for line in schema_value_lines:
        stripped = line.strip()
        m = re.match(r"(\w+)\s*=\s*", stripped)
        if m:
            schema_values[m.group(1)] = line

    existing_values = _get_enum_value_lines(class_source)

    # Needs update if any schema value is missing or has different text
    needs_update = False
    for val_name, schema_line in schema_values.items():
        if val_name not in existing_values:
            needs_update = True
            break
        if existing_values[val_name].rstrip() != schema_line.rstrip():
            needs_update = True
            break

    if not needs_update:
        return None

    # Surgical patch: keep all non-value lines (docstring, comments) verbatim
    src_lines = class_source.splitlines(keepends=True)
    stmts_by_lineno: dict[int, str] = {}  # 0-indexed start → val_name
    try:
        tree = ast.parse(class_source)
        cls = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)), None)
        if cls:
            for stmt in cls.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Constant)
                ):
                    stmts_by_lineno[stmt.lineno - 1] = stmt.targets[0].id
    except SyntaxError:
        pass

    result: list[str] = []
    last_value_idx: int = -1
    i = 0
    while i < len(src_lines):
        if i in stmts_by_lineno:
            val_name = stmts_by_lineno[i]
            if val_name in schema_values:
                result.append(schema_values[val_name].rstrip() + "\n")
            else:
                result.append(src_lines[i])
            last_value_idx = len(result) - 1
            i += 1
        else:
            result.append(src_lines[i])
            i += 1

    # Insert new values after the last existing value line
    new_vals = [v for v in schema_values if v not in existing_values]
    if new_vals:
        insert_at = last_value_idx + 1 if last_value_idx >= 0 else len(result)
        for val_name in new_vals:
            result.insert(insert_at, schema_values[val_name].rstrip() + "\n")
            insert_at += 1

    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def surgical_update_class(
    class_source: str,
    schema_field_lines: list[str],
) -> list[str] | None:
    """Decide whether *class_source* needs updating; if so, return patched lines.

    Args:
        class_source: Full source text of one class definition (joined lines).
        schema_field_lines: Canonical ``Field()`` / ``mapped_column()`` lines
            produced by the generator's ``_column_line()`` for every schema
            column.  No trailing newline required.

    Returns:
        ``None`` if the class is already up-to-date (no schema changes).
        A ``list[str]`` of patched lines (each ending with ``"\\n"``) otherwise.
        The caller should splice this list into the file's line array in place
        of the original class lines.
    """
    if not _class_needs_update(class_source, schema_field_lines):
        return None
    return _surgical_patch_class(class_source, schema_field_lines)
