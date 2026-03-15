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


def _get_bare_field_stmts(class_source: str) -> list[tuple[str, int, int]]:
    """Return (col_name, start_lineno, end_lineno) for AnnAssign statements that
    are bare type annotations or have simple literal defaults — NOT ``Field()``
    or ``mapped_column()`` calls.

    Matches:
      body: str                              (no value — bare annotation)
      count: int = 0                         (ast.Constant literal default)
      optional_field: Optional[str] = None   (ast.Constant(None) default)

    Line numbers are 1-indexed and relative to *class_source*.
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
        if isinstance(stmt.value, ast.Call):
            continue  # Field() / mapped_column() — handled by _get_field_stmts
        if not isinstance(stmt.target, ast.Name):
            continue
        # Accept: no value (bare) or a simple constant/None literal
        if stmt.value is None or isinstance(stmt.value, ast.Constant):
            results.append((stmt.target.id, stmt.lineno, stmt.end_lineno))
    return results


def _bare_field_default(bare_text: str) -> str | None:
    """Return the ``ast.unparse``'d default value from a bare/simple-default field
    line, or ``None`` if the field has no explicit default.

    E.g. ``"    count: int = 0"``  →  ``"0"``
         ``"    opt: Optional[str] = None"``  →  ``"None"``
         ``"    body: str"``  →  ``None``
    """
    try:
        tree = ast.parse(bare_text.strip())
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            if node.value is None:
                return None
            if isinstance(node.value, ast.Constant):
                return ast.unparse(node.value)
    return None


def _bare_field_equivalent(bare_text: str, generated_line: str) -> bool:
    """Return ``True`` if a bare annotation / simple-default field line is
    semantically equivalent to a generated ``Field(...)`` line, meaning
    no schema change needs to be applied.

    Equivalence rules:
    * ``name: Type``  ↔  ``name: Type = Field()``          (no kwargs)
    * ``name: Type = None``  ↔  ``name: Type = Field(default=None)``
    * ``name: Type = <constant>``  ↔  ``name: Type = Field(default=<constant>)``
      (only when generated kwargs are exactly ``{default: <constant>}``)

    Any generated kwargs beyond a matching ``default`` (e.g. ``max_length``,
    ``unique``, ``index``, ``foreign_key``) are NOT equivalent — the field
    needs to be upgraded to a full ``Field(...)`` call.
    """
    gen_kw = _parse_field_kwargs(generated_line)
    if gen_kw is None:
        return False

    bare_default = _bare_field_default(bare_text)

    # Generated Field() has no kwargs at all, and bare field has no explicit
    # default → equivalent (plain non-nullable column without special constraints)
    if not gen_kw and bare_default is None:
        return True

    # Generated has exactly one kwarg: default=<X>.
    # Bare has the same constant as its explicit default → equivalent.
    if set(gen_kw.keys()) == {"default"} and bare_default is not None:
        return gen_kw["default"] == bare_default

    # All other cases (max_length, unique, index, FK, primary_key, etc.)
    # are NOT equivalent — the bare line needs to become a full Field() call.
    return False


# ---------------------------------------------------------------------------
# Semantic Field() kwargs comparison
# ---------------------------------------------------------------------------

def _parse_field_kwargs(line: str) -> dict[str, str] | None:
    """Parse ``Field(pk=True, default_factory=uuid.uuid4)`` from *line* into a
    ``{kwarg_name: ast.unparse(value)}`` dict.  Returns ``None`` on failure.

    Positional args are stored as ``__pos_0``, ``__pos_1``, … so they still
    participate in equality comparison.

    Handles multi-line field text by joining lines before parsing.
    """
    # Join multi-line text to a single line for parsing
    joined = " ".join(line.splitlines())
    for sep in ("= Field(", "= mapped_column("):
        pos = joined.find(sep)
        if pos != -1:
            rhs = joined[pos + 2:].strip()
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


def _parse_field_kwargs_raw_text(field_text: str) -> dict[str, str]:
    """Return ``{kwarg_name: "kwarg_name=raw_value"}`` for each keyword arg.

    Unlike :func:`_parse_field_kwargs`, which normalises values through
    ``ast.unparse``, this function extracts the **verbatim source text** of
    each keyword argument value using AST column-offset information.  This
    lets :func:`_rebuild_field_line` re-emit *unchanged* kwargs byte-for-byte,
    preserving the original quote style (e.g. ``foreign_key="user.id"`` rather
    than ``foreign_key='user.id'``).

    Only keyword arguments are included; positional args fall back to the
    ``ast.unparse``'d form in the caller.
    """
    # Join multi-line text to a single line (same approach as _parse_field_kwargs)
    joined = " ".join(field_text.splitlines())
    for sep in ("= Field(", "= mapped_column("):
        pos = joined.find(sep)
        if pos != -1:
            rhs = joined[pos + 2:].strip()
            break
    else:
        return {}

    try:
        tree = ast.parse(rhs, mode="eval")
    except SyntaxError:
        return {}

    if not isinstance(tree.body, ast.Call):
        return {}

    result: dict[str, str] = {}
    for kw in tree.body.keywords:
        if kw.arg is None:
            continue  # **kwargs splat — skip
        val_start = kw.value.col_offset
        val_end = kw.value.end_col_offset
        raw_val = rhs[val_start:val_end]
        result[kw.arg] = f"{kw.arg}={raw_val}"
    return result


def _field_lhs(line: str) -> str:
    """Return the ``name: Type`` portion of a field line (normalised whitespace).

    For multi-line fields, uses only the first line.
    """
    first_line = line.splitlines()[0] if "\n" in line else line
    for sep in ("= Field(", "= mapped_column("):
        pos = first_line.find(sep)
        if pos != -1:
            return " ".join(first_line[:pos].split())
    return line.strip()


def _strip_optional_from_annotation(ann: str) -> str:
    """Strip ``Optional[...]`` wrapper from a type-annotation string.

    ``"Optional[uuid.UUID]"`` → ``"uuid.UUID"``.
    Nested/complex types are returned unchanged.
    """
    t = ann.strip()
    if t.startswith("Optional[") and t.endswith("]"):
        return t[len("Optional["):-1].strip()
    return t


def _lhs_type_part(lhs: str) -> str:
    """Extract the type annotation part from ``"name: Type"``."""
    if ":" in lhs:
        return lhs.split(":", 1)[1].strip()
    return lhs


# ---------------------------------------------------------------------------
# Mutable-default equivalence helpers (Bug 8)
# ---------------------------------------------------------------------------

# Pairs of (kwarg_name, ast-unparsed value) that are semantically identical.
# Key = representation typically found in hand-written code.
# Value = representation generated by _field_args().
_MUTABLE_DEFAULT_EQUIV: dict[tuple[str, str], tuple[str, str]] = {
    ("default", "{}"): ("default_factory", "dict"),
    ("default", "[]"): ("default_factory", "list"),
}

# Value-level equivalences for ``default_factory``.
# Maps the user's hand-written form → the canonical form emitted by the generator.
# Used both in equality checks (no rewrite) and in rebuild (preserve user's form).
_DEFAULT_FACTORY_EQUIV: dict[str, str] = {
    # datetime.utcnow is deprecated in Python 3.12+; the generator emits the
    # modern lambda form.  Treat both as semantically identical so existing
    # hand-written code is never touched unless the field actually changes.
    #
    # NOTE: the canonical form uses the exact string produced by ast.unparse(),
    # which inserts a space between "lambda" and ":" (i.e. "lambda :").
    "datetime.utcnow": "lambda : datetime.now(timezone.utc)",
    # uuid4 (from `from uuid import uuid4`) and uuid.uuid4 (from `import uuid`)
    # are semantically identical.  Treat them as equal so existing hand-written
    # code using the direct-import form is never touched.
    "uuid4": "uuid.uuid4",
}


def _normalize_kw_for_eq(kw: dict[str, str]) -> dict[str, str]:
    """Normalise mutable-default equivalents to a single canonical form.

    ``default={}`` and ``default_factory=dict`` are semantically identical;
    normalise both to the ``default_factory`` form so that the equality check
    does not trigger a spurious rewrite.

    Also normalises ``default_factory`` *value* equivalences (e.g.
    ``datetime.utcnow`` → ``lambda: datetime.now(timezone.utc)``) so that
    fields using the deprecated form are not spuriously rewritten just because
    the generator produces the modern form.
    """
    result = dict(kw)
    for (old_key, old_val), (new_key, new_val) in _MUTABLE_DEFAULT_EQUIV.items():
        if result.get(old_key) == old_val:
            del result[old_key]
            result[new_key] = new_val
    if "default_factory" in result:
        val = result["default_factory"]
        result["default_factory"] = _DEFAULT_FACTORY_EQUIV.get(val, val)
    return result


def _extract_trailing_comment(existing_text: str) -> str:
    """Return any trailing ``  # …`` comment after the closing ``)`` of a field.

    Works for both single-line fields and multi-line fields (comment lives on
    the closing-paren line).  Returns ``""`` when there is no comment.
    """
    last_line = existing_text.rstrip().splitlines()[-1]
    close_idx = last_line.rfind(")")
    if close_idx == -1:
        return ""
    after = last_line[close_idx + 1:]
    m = re.search(r"(#.*)", after)
    return ("  " + m.group(1)) if m else ""


def _field_kwargs_equal(existing: str, new: str) -> bool:
    """Semantic equality: same ``name: Type`` LHS **and** same Field() kwargs
    (order-independent).  Falls back to stripped string comparison if AST
    parsing fails.

    Bug-3 fix: for primary-key fields the type annotation may be
    ``Optional[X]`` in the existing file even though the schema generates
    ``X``.  We treat those as equal to avoid spurious rewrites.
    """
    if existing.rstrip() == new.rstrip():
        return True

    existing_lhs = _field_lhs(existing)
    new_lhs = _field_lhs(new)

    if existing_lhs != new_lhs:
        # Allow Optional[X] vs X discrepancy on PK fields (Bug 3).
        # If stripping Optional from the existing annotation makes the LHS equal,
        # and the new field is a primary key, treat as matching.
        existing_type = _lhs_type_part(existing_lhs)
        new_type = _lhs_type_part(new_lhs)
        existing_name = existing_lhs.split(":")[0].strip() if ":" in existing_lhs else existing_lhs
        new_name = new_lhs.split(":")[0].strip() if ":" in new_lhs else new_lhs
        # Names must still match
        if existing_name != new_name:
            return False
        # Accept Optional[X] == X only when primary_key=True is in the new kwargs
        nkw = _parse_field_kwargs(new)
        if nkw and nkw.get("primary_key") == "True":
            if _strip_optional_from_annotation(existing_type) == new_type:
                # LHS now equivalent — fall through to kwargs comparison
                pass
            else:
                return False
        else:
            return False

    ekw = _parse_field_kwargs(existing)
    nkw = _parse_field_kwargs(new)
    if ekw is None or nkw is None:
        return existing.rstrip() == new.rstrip()
    return _normalize_kw_for_eq(ekw) == _normalize_kw_for_eq(nkw)


# ---------------------------------------------------------------------------
# Minimal-diff field replacement (Bugs 4 & 5)
# ---------------------------------------------------------------------------

def _get_kwarg_order(field_text: str) -> list[str]:
    """Return the ordered list of kwarg names from a Field() / mapped_column()
    call.  Positional args get synthetic names ``__pos_0`` etc."""
    joined = " ".join(field_text.splitlines())
    for sep in ("= Field(", "= mapped_column("):
        pos = joined.find(sep)
        if pos != -1:
            rhs = joined[pos + 2:].strip()
            break
    else:
        return []
    try:
        tree = ast.parse(rhs, mode="eval")
    except SyntaxError:
        return []
    if not isinstance(tree.body, ast.Call):
        return []
    order: list[str] = []
    for i, _ in enumerate(tree.body.args):
        order.append(f"__pos_{i}")
    for k in tree.body.keywords:
        if k.arg:
            order.append(k.arg)
    return order


def _rebuild_field_line(
    existing_text: str,
    new_schema_line: str,
) -> str:
    """Return a replacement for *existing_text* that applies changes from
    *new_schema_line* while preserving:

    * The original kwarg order for kwargs that haven't changed (Bug 5).
    * Multi-line formatting when the original spans multiple lines (Bug 4).
    * The original type annotation (LHS) verbatim (Bug 3 — keeps Optional[X]).

    Algorithm:
    1. Parse existing and new kwargs into dicts.
    2. Build the merged kwarg list: existing order first (keep/update each
       kwarg); append new kwargs not in original at the end; drop kwargs
       removed from schema.
    3. Reconstruct ``Field(...)`` from the merged list.
    4. If the original was multi-line, format the result as multi-line.
    """
    existing_kw = _parse_field_kwargs(existing_text) or {}
    new_kw = _parse_field_kwargs(new_schema_line) or {}
    existing_order = _get_kwarg_order(existing_text)

    # Raw (verbatim) kwarg text from the existing source — used to preserve
    # the original quote style for string values that haven't changed (Bug 17).
    existing_raw = _parse_field_kwargs_raw_text(existing_text)

    # Preserve any trailing inline comment (Bug 8 — fix 3)
    trailing_comment = _extract_trailing_comment(existing_text)

    # Determine leading indent from the existing text
    first_line = existing_text.splitlines()[0] if existing_text else new_schema_line
    indent = len(first_line) - len(first_line.lstrip())
    indent_str = first_line[:indent]

    # Detect the call keyword (Field or mapped_column)
    joined = " ".join(existing_text.splitlines())
    if "= mapped_column(" in joined:
        call_kw = "mapped_column"
    else:
        call_kw = "Field"

    # Use the LHS from the EXISTING field (preserves Optional[X] etc.) — Bug 3
    existing_lhs = _field_lhs(existing_text)

    # Build ordered merged kwargs:
    # - Walk existing order; update value if changed; skip if removed from new.
    # - Append any new kwargs not in original.
    merged: list[str] = []
    seen: set[str] = set()
    for key in existing_order:
        existing_val = existing_kw.get(key, "")
        if key in new_kw:
            val = new_kw[key]
            if key.startswith("__pos_"):
                merged.append(val)  # positional
            elif key == "default_factory" and _DEFAULT_FACTORY_EQUIV.get(existing_val) == val:
                # The existing value is a known equivalent of what the generator
                # produces (e.g. datetime.utcnow ↔ lambda: datetime.now(timezone.utc),
                # uuid4 ↔ uuid.uuid4).
                # Preserve the user's original form so we don't introduce noisy diffs.
                merged.append(f"default_factory={existing_val}")
            elif existing_val == val and key in existing_raw:
                # Value unchanged — preserve the original raw text verbatim so
                # that e.g. double-quoted strings (foreign_key="user.id") are
                # not silently rewritten to single-quoted form by ast.unparse
                # (Bug 17 — quote style preservation).
                merged.append(existing_raw[key])
            else:
                merged.append(f"{key}={val}")
            seen.add(key)
        else:
            # Bug 8 fix 2: mutable-default equivalent
            # e.g. existing default={} ↔ generated default_factory=dict
            # Keep the existing representation so we don't mutate the user's code.
            equiv = _MUTABLE_DEFAULT_EQUIV.get((key, existing_val))
            if equiv and equiv[0] in new_kw and new_kw[equiv[0]] == equiv[1]:
                merged.append(f"{key}={existing_val}")
                seen.add(equiv[0])  # prevent duplicate emission from new_kw loop
            # else: kwarg genuinely removed from schema — omit

    for key, val in new_kw.items():
        if key in seen:
            continue
        if key.startswith("__pos_"):
            merged.append(val)
        else:
            merged.append(f"{key}={val}")

    args_str = ", ".join(merged)

    # Preserve multi-line formatting (Bug 4): if original was multi-line,
    # emit each kwarg on its own line.
    original_lines = existing_text.splitlines()
    was_multiline = len(original_lines) > 1

    if was_multiline and merged:
        inner_indent = indent_str + "    "
        lines = [f"{indent_str}{existing_lhs} = {call_kw}("]
        for i, arg in enumerate(merged):
            comma = "," if i < len(merged) - 1 else ""
            lines.append(f"{inner_indent}{arg}{comma}")
        lines.append(f"{indent_str}){trailing_comment}")
        return "\n".join(lines)
    else:
        return f"{indent_str}{existing_lhs} = {call_kw}({args_str}){trailing_comment}"


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
    - Bare annotations / simple-default fields are treated as existing: no
      update is needed when they are equivalent to the generated Field() line.
      If the schema adds new constraints (max_length, unique, etc.) to a
      previously bare field, that DOES trigger an update.
    - Extra columns in the file that are not in the schema → ignored (no update).
    - Non-schema lines (docstrings, Relationship, comments) → ignored entirely.
    """
    stmts = _get_field_stmts(class_source)
    bare_stmts = _get_bare_field_stmts(class_source)
    src_lines = class_source.splitlines(keepends=True)

    # Build {col_name: full existing field text} for Field()-style fields
    existing: dict[str, str] = {}
    for col_name, start, end in stmts:
        existing[col_name] = "".join(src_lines[start - 1 : end]).rstrip()

    # Build {col_name: full existing bare-field text}
    bare_existing: dict[str, str] = {}
    for col_name, start, end in bare_stmts:
        bare_existing[col_name] = "".join(src_lines[start - 1 : end]).rstrip()

    for line in schema_field_lines:
        col_name = _col_name_from_generated(line)
        if col_name is None:
            continue
        if col_name in existing:
            if not _field_kwargs_equal(existing[col_name], line):
                return True  # changed Field() column
        elif col_name in bare_existing:
            if not _bare_field_equivalent(bare_existing[col_name], line):
                return True  # schema adds constraints not reflected in bare field
        else:
            return True  # genuinely new column

    # Reverse check: if any Field()-style column in the file is NOT in the
    # schema, the class needs updating (that column was deleted from the schema).
    # We intentionally do NOT check bare annotations here — those may be
    # hand-written non-ORM helper attributes and should never be auto-deleted.
    schema_col_names = {
        c
        for line in schema_field_lines
        if (c := _col_name_from_generated(line)) is not None
    }
    for col_name, _, _ in stmts:
        if col_name not in schema_col_names:
            return True

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
         hand-written kwarg order and multi-line formatting).
       - Field() lines whose kwargs differ → rebuilt via ``_rebuild_field_line``
         which preserves kwarg order, multi-line style, and the existing LHS
         type annotation (Bug 3/4/5).
       - All other lines (docstring, Relationship, comments, __tablename__, blanks,
         class header) → emitted verbatim.
    3. New schema columns (absent from the file) are inserted immediately after
       the last existing Field() line, which places them before any Relationship()
       section.
    """
    stmts = _get_field_stmts(class_source)
    bare_stmts = _get_bare_field_stmts(class_source)
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
    bare_col_names: set[str] = {col for col, _, _ in bare_stmts}

    # Map each field stmt start-line (0-indexed) to (col_name, end_0idx, is_bare).
    # is_bare=True means the existing line is a bare annotation / simple default
    # rather than a Field() call, and requires different update logic.
    stmt_ranges: dict[int, tuple[str, int, bool]] = {}
    for col, start, end in stmts:
        stmt_ranges[start - 1] = (col, end, False)
    for col, start, end in bare_stmts:
        stmt_ranges[start - 1] = (col, end, True)

    result: list[str] = []
    last_field_result_idx: int = -1
    i = 0

    while i < len(src_lines):
        if i in stmt_ranges:
            col_name, end, is_bare = stmt_ranges[i]
            # Collect the full existing field text (may span multiple lines)
            existing_text = "".join(src_lines[i:end]).rstrip()

            if is_bare:
                if col_name in schema_map and not _bare_field_equivalent(existing_text, schema_map[col_name]):
                    # Schema adds constraints not present in the bare annotation —
                    # replace the bare line with the full generated Field() line,
                    # preserving the existing indent.
                    indent = len(existing_text) - len(existing_text.lstrip())
                    indent_str = existing_text[:indent]
                    new_line = schema_map[col_name].lstrip()
                    result.append(indent_str + new_line + "\n")
                else:
                    # Bare field is equivalent or not in schema — keep verbatim.
                    # We never auto-delete bare annotations: they may be non-ORM
                    # helper attributes, not managed schema columns.
                    for ln in src_lines[i:end]:
                        result.append(ln)
                # Bare branch always produces output — update insertion marker.
                last_field_result_idx = len(result) - 1
            else:
                if col_name in schema_map and not _field_kwargs_equal(existing_text, schema_map[col_name]):
                    # Rebuild with original kwarg order / multi-line style / LHS preserved
                    rebuilt = _rebuild_field_line(existing_text, schema_map[col_name])
                    result.append(rebuilt.rstrip() + "\n")
                    last_field_result_idx = len(result) - 1
                elif col_name in schema_map:
                    # Keep verbatim (unchanged field — preserves kwarg order and formatting)
                    for ln in src_lines[i:end]:
                        result.append(ln)
                    last_field_result_idx = len(result) - 1
                # else: col_name not in schema_map → column deleted from schema;
                # omit from output.  Do NOT update last_field_result_idx so that
                # any new columns are still inserted after the last kept field.
            i = end
        else:
            result.append(src_lines[i])
            i += 1

    # Insert genuinely new columns (absent from both Field() and bare stmts) after
    # the last existing field line.
    all_existing_names = existing_col_names | bare_col_names
    new_cols = [name for name in schema_order if name not in all_existing_names]
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
