"""SQLModel code generator.

Converts an ``AlterSchema`` into valid, runnable SQLModel Python source code.
Supports three modes:

- ``generate_models()``: full file generation from scratch
- ``update_models()``: surgical update — only modified classes are replaced,
  everything else (comments, blank lines, helper functions) is preserved
- ``preview_apply()``: dry run returning a unified diff of all changes
"""

from __future__ import annotations

import ast
import difflib
import keyword

from alter.generators._surgical import surgical_update_class, surgical_update_enum_class
from pathlib import Path

from alter.generators.base import BaseGenerator, _default_model_path
from alter.schema import AlterSchema, Column, EnumDef, Table
from alter.types import alter_to_python, is_enum_type


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _class_name(table_name: str) -> str:
    """``snake_case`` → ``PascalCase``."""
    return "".join(w.capitalize() for w in table_name.split("_"))


def _python_type(col: Column, enum_names: set[str]) -> str:
    """Return the Python type-hint string for *col*."""
    if col.type in enum_names:
        base = col.type
    else:
        base = alter_to_python(col.type)
    if col.nullable and not col.primary_key:
        return f"Optional[{base}]"
    return base


def _field_args(col: Column, enum_names: set[str]) -> str:
    """Return the Field() argument list (comma-separated, no outer parens).

    Canonical kwarg order (intentional — see design note below):
      1. primary_key
      2. default / default_factory
      3. foreign_key
      4. unique
      5. index
      6. max_length
      7. extra passthrough kwargs (sa_column, regex, ge/le, …)

    Design note — kwarg order normalisation:
      ``_field_args`` is used when *generating* a Field() from scratch (full
      file generation or appending a brand-new class).  For *surgical updates*
      of existing fields, ``_rebuild_field_line`` in ``_surgical.py`` is used
      instead and always preserves the original kwarg order.  Therefore the
      only time this canonical order becomes visible in a diff is when a model
      file is created from scratch or a completely new class is appended — a
      deliberate normalisation, not a bug.
    """
    args: list[str] = []

    if col.primary_key:
        args.append("primary_key=True")

    # default / default_factory
    if col.default and col.default.startswith("expr:"):
        # Verbatim expression preserved from source (e.g. lambda)
        args.append(f"default_factory={col.default[5:]}")
    elif col.default == "uuid4":
        args.append("default_factory=uuid.uuid4")
    elif col.default == "utcnow":
        args.append("default_factory=lambda: datetime.now(timezone.utc)")
    elif col.default == "now":
        args.append("default_factory=datetime.now")
    elif col.default == "list":
        args.append("default_factory=list")
    elif col.default == "{}":
        args.append("default_factory=dict")
    elif col.default == "[]":
        args.append("default_factory=list")
    elif col.default is not None:
        raw = col.default
        if raw == "true":
            args.append("default=True")
        elif raw == "false":
            args.append("default=False")
        elif raw.lstrip("-").isdigit():
            args.append(f"default={raw}")
        elif col.type in enum_names:
            # Enum member: reconstruct as EnumClass.member
            args.append(f"default={col.type}.{raw}")
        else:
            args.append(f'default="{raw}"')
    elif col.nullable and not col.primary_key:
        args.append("default=None")

    if col.foreign_key:
        args.append(f'foreign_key="{col.foreign_key}"')
    if col.unique:
        args.append("unique=True")
    if col.index:
        args.append("index=True")
    if col.max_length is not None:
        args.append(f"max_length={col.max_length}")

    # Passthrough kwargs (sa_column, regex, ge, le, etc.)
    if col.extra_kwargs:
        for k, v in col.extra_kwargs.items():
            args.append(f"{k}={v}")

    return ", ".join(args)


def _column_line(col: Column, enum_names: set[str]) -> str:
    """Return one ``    name: Type = Field(...)`` source line (no newline)."""
    type_hint = _python_type(col, enum_names)
    field_str = _field_args(col, enum_names)
    return f"    {col.name}: {type_hint} = Field({field_str})"


def _safe_member_name(name: str) -> str:
    """Return a safe Python identifier for an enum member name.

    If *name* is a Python keyword (e.g. ``"import"``, ``"class"``), a
    trailing underscore is appended to avoid a ``SyntaxError``.
    """
    return f"{name}_" if keyword.iskeyword(name) else name


def _enum_class_source(enum: EnumDef) -> str:
    """Return source for a ``class X(str, Enum):`` definition."""
    from alter.schema import EnumMember
    lines = [f"class {enum.name}(str, Enum):"]
    for v in enum.values:
        if isinstance(v, EnumMember):
            mname = _safe_member_name(v.member_name)
            lines.append(f'    {mname} = "{v.value}"')
        else:
            # Legacy plain-string fallback
            mname = _safe_member_name(v)
            lines.append(f'    {mname} = "{v}"')
    return "\n".join(lines)


def _model_class_source(
    table: Table,
    enum_names: set[str],
    class_name: str | None = None,
) -> str:
    """Return source for a SQLModel model class.

    Args:
        class_name: If provided, use this name instead of the conventional
            PascalCase of ``table.name``. Used by ``update_models()`` to
            preserve existing hand-written class names (e.g. ``User`` rather
            than the generated ``Users``).
    """
    name = class_name if class_name is not None else _class_name(table.name)
    # Use recorded base classes if available, otherwise fall back to SQLModel
    if table.bases:
        bases_str = ", ".join(table.bases) + ", table=True"
    else:
        bases_str = "SQLModel, table=True"
    lines: list[str] = [f"class {name}({bases_str}):"]
    # Emit __tablename__ (always — it's explicit and unambiguous)
    lines.append(f'    __tablename__ = "{table.name}"')
    # Emit __table_args__ when the table lives in a PostgreSQL schema
    if table.schema_name:
        lines.append(f'    __table_args__ = {{"schema": "{table.schema_name}"}}')
    lines.append("")
    # Only emit non-inherited columns — inherited ones live in the base classes
    for col in table.columns:
        if not col.inherited:
            lines.append(_column_line(col, enum_names))
    return "\n".join(lines)


def _build_imports(
    schema: AlterSchema,
    enum_names: set[str],
    emit_enum_names: set[str] | None = None,
) -> list[str]:
    """Return ordered import lines required by *schema*.

    Args:
        enum_names: All known enum names — used for type-hint resolution
            (``Optional[Role]`` vs ``Optional[str]``).
        emit_enum_names: If provided, only add ``from enum import Enum`` when
            this set is non-empty (i.e. when local enum classes will be defined
            in this file).  Defaults to *enum_names* when ``None``.
    """
    if emit_enum_names is None:
        emit_enum_names = enum_names

    needs_uuid = False
    datetime_names: set[str] = set()
    needs_timezone = False
    needs_optional = False
    needs_decimal = False

    for table in schema.tables:
        for col in table.columns:
            py = alter_to_python(col.type) if col.type not in enum_names else col.type
            if "uuid" in py.lower():
                needs_uuid = True
            if py in ("datetime", "date", "time"):
                datetime_names.add(py)
            if col.default == "utcnow":
                datetime_names.add("datetime")
                needs_timezone = True
            if col.nullable and not col.primary_key:
                needs_optional = True
            if py == "Decimal":
                needs_decimal = True

    lines: list[str] = []
    # stdlib
    if needs_uuid:
        lines.append("import uuid")
    if datetime_names:
        dt_imports = sorted(datetime_names)
        if needs_timezone:
            dt_imports = sorted(set(dt_imports) | {"timezone"})
        lines.append(f"from datetime import {', '.join(dt_imports)}")
    if emit_enum_names:
        lines.append("from enum import Enum")
    if needs_decimal:
        lines.append("from decimal import Decimal")
    if needs_optional:
        lines.append("from typing import Optional")
    lines.append("from sqlmodel import Field, SQLModel")
    return lines


def _imported_names(tree: ast.Module) -> set[str]:
    """Return the set of all names made available by import statements in *tree*."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname if alias.asname else alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname if alias.asname else alias.name)
    return names


def _missing_imports(
    schema: AlterSchema,
    enum_names: set[str],
    tree: ast.Module,
    emit_enum_names: set[str] | None = None,
) -> list[str]:
    """Return import lines that are needed but not yet present in the parsed file."""
    present = _imported_names(tree)
    needed = _build_imports(schema, enum_names, emit_enum_names=emit_enum_names)
    missing: list[str] = []
    for line in needed:
        # Parse the single import line to extract what names it provides
        try:
            line_tree = ast.parse(line)
        except SyntaxError:
            continue
        line_names: set[str] = set()
        for node in ast.walk(line_tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    line_names.add(alias.asname if alias.asname else alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    line_names.add(alias.asname if alias.asname else alias.name)
        # Only add the line if *none* of its provided names are already present
        if not line_names.issubset(present):
            missing.append(line)
    return missing


def _ensure_imports(
    code: str,
    schema: AlterSchema,
    enum_names: set[str],
    emit_enum_names: set[str] | None = None,
) -> str:
    """Insert any missing import lines immediately after the last existing import."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    missing = _missing_imports(schema, enum_names, tree, emit_enum_names=emit_enum_names)
    if not missing:
        return code

    import_nodes = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
    lines = code.splitlines(keepends=True)
    if import_nodes:
        insert_after = max(
            getattr(n, "end_lineno", None) or n.lineno for n in import_nodes
        )
        # insert_after is 1-indexed; list index is 0-indexed
        lines.insert(insert_after, "\n".join(missing) + "\n")
    else:
        lines.insert(0, "\n".join(missing) + "\n")

    return "".join(lines)


# ---------------------------------------------------------------------------
# SQLModelGenerator
# ---------------------------------------------------------------------------


class SQLModelGenerator(BaseGenerator):
    """Generates SQLModel Python source code from an ``AlterSchema``."""

    # ------------------------------------------------------------------
    # 1. Full generation
    # ------------------------------------------------------------------

    def generate_models(
        self,
        schema: AlterSchema,
        local_enum_names: set[str] | None = None,
    ) -> str:
        """Generate a complete models.py from *schema*.

        Args:
            local_enum_names: If provided, only emit enum class definitions for
                names in this set.  Type resolution always uses all enums in
                *schema*.  Pass this when the schema holds all project enums but
                the target file should only define a subset (e.g. because the
                others are imported from sibling modules).
        """
        all_enum_names: set[str] = {e.name for e in schema.enums}
        emit_enum_names = local_enum_names if local_enum_names is not None else all_enum_names
        parts: list[str] = []

        import_lines = _build_imports(schema, all_enum_names, emit_enum_names=emit_enum_names)
        parts.append("\n".join(import_lines))

        for enum in schema.enums:
            if enum.name in emit_enum_names:
                parts.append(_enum_class_source(enum))

        for table in schema.tables:
            parts.append(_model_class_source(table, all_enum_names))

        return "\n\n\n".join(parts) + "\n"

    # ------------------------------------------------------------------
    # 2. Surgical update
    # ------------------------------------------------------------------

    def update_models(
        self,
        schema: AlterSchema,
        existing_code: str,
        local_enum_names: set[str] | None = None,
    ) -> str:
        """Replace only changed model/enum classes; preserve everything else.

        Args:
            local_enum_names: If provided, only emit/update enum class definitions
                for names in this set.  Enums imported from other files must NOT
                be appended as new class definitions here even if they appear in
                *schema*.  Type resolution always uses all enums in *schema*.
        """
        all_enum_names: set[str] = {e.name for e in schema.enums}
        emit_enum_names = local_enum_names if local_enum_names is not None else all_enum_names

        try:
            tree = ast.parse(existing_code)
        except SyntaxError:
            return self.generate_models(schema, local_enum_names=local_enum_names)

        lines = existing_code.splitlines(keepends=True)

        # Map existing class name → (start_lineno, end_lineno) [1-indexed, inclusive]
        existing_classes: dict[str, tuple[int, int]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                existing_classes[node.name] = (node.lineno, node.end_lineno)

        # Build tablename → existing class name map by scanning __tablename__ assignments.
        # This handles hand-written models where class name differs from the convention
        # (e.g. class User with __tablename__ = "users" instead of class Users).
        tablename_to_class: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if (
                        isinstance(stmt, ast.Assign)
                        and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Name)
                        and stmt.targets[0].id == "__tablename__"
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)
                    ):
                        tablename_to_class[stmt.value.value] = node.name

        # What the schema wants — prefer existing class name (from __tablename__) over convention
        table_by_class: dict[str, Table] = {
            tablename_to_class.get(t.name, _class_name(t.name)): t
            for t in schema.tables
        }
        # Only consider locally-owned enums for class emission
        enum_by_class: dict[str, EnumDef] = {
            e.name: e for e in schema.enums if e.name in emit_enum_names
        }
        schema_classes: set[str] = set(table_by_class) | set(enum_by_class)

        # Build replacements: (start, end, patched_lines) — applied bottom-up
        replacements: list[tuple[int, int, list[str]]] = []
        for cls_name, (start, end) in existing_classes.items():
            if cls_name not in schema_classes:
                # Class exists in file but not in schema.
                # Leave it untouched — destructive removal requires explicit
                # confirmation at the CLI/MCP layer, not inside the generator.
                continue
            if cls_name in table_by_class:
                # Surgical update: preserve docstrings, Relationship lines,
                # comments, and hand-written Field() kwarg ordering.
                # Only include non-inherited columns — inherited ones live in base classes.
                schema_field_lines = [
                    _column_line(col, all_enum_names)
                    for col in table_by_class[cls_name].columns
                    if not col.inherited
                ]
                class_source = "".join(lines[start - 1 : end])
                patched = surgical_update_class(class_source, schema_field_lines)
                if patched is None:
                    continue  # no schema changes — leave class entirely untouched
                replacements.append((start, end, patched))
            else:
                # Enum class: surgical update preserves docstrings / comments
                from alter.schema import EnumMember
                schema_value_lines = []
                for v in enum_by_class[cls_name].values:
                    if isinstance(v, EnumMember):
                        mname = _safe_member_name(v.member_name)
                        schema_value_lines.append(f'    {mname} = "{v.value}"')
                    else:
                        mname = _safe_member_name(v)
                        schema_value_lines.append(f'    {mname} = "{v}"')
                class_source = "".join(lines[start - 1 : end])
                patched = surgical_update_enum_class(class_source, schema_value_lines)
                if patched is None:
                    continue
                replacements.append((start, end, patched))

        # Apply bottom-up to preserve line numbers
        replacements.sort(key=lambda r: r[0], reverse=True)
        for start, end, patched_lines in replacements:
            lines[start - 1 : end] = patched_lines

        result = "".join(lines)

        # Append new classes (exist in schema but not in file)
        for cls_name in sorted(schema_classes - set(existing_classes)):
            if cls_name in table_by_class:
                new_src = _model_class_source(
                    table_by_class[cls_name], all_enum_names, class_name=cls_name
                )
            else:
                new_src = _enum_class_source(enum_by_class[cls_name])

            if not result.endswith("\n\n"):
                result = result.rstrip("\n") + "\n\n"
            result += new_src + "\n"

        # Ensure all imports needed by the (possibly updated) schema are present.
        # This handles the case where a surgical update introduces a new type
        # (e.g. datetime, Optional) that wasn't imported in the existing file.
        result = _ensure_imports(result, schema, all_enum_names, emit_enum_names=emit_enum_names)

        return result

    # ------------------------------------------------------------------
    # 3. Preview (dry-run diff)
    # ------------------------------------------------------------------

    def preview_apply(self, schema: AlterSchema, project_root: Path) -> str:
        """Return unified diff of all files that WOULD change. Writes nothing."""
        # Group tables by file_path
        file_tables: dict[str, list[Table]] = {}
        for table in schema.tables:
            fp = table.file_path or _default_model_path(schema, project_root)
            file_tables.setdefault(fp, []).append(table)

        diffs: list[str] = []
        for rel_path, tables in file_tables.items():
            abs_path = project_root / rel_path
            # Only define enum classes that are owned by this file
            local_enum_names = {
                e.name for e in schema.enums
                if e.file_path is None or e.file_path == rel_path
            }
            sub = AlterSchema(
                version=schema.version,
                orm=schema.orm,
                dialect=schema.dialect,
                tables=tables,
                enums=schema.enums,
                relations=schema.relations,
            )
            if abs_path.exists():
                existing = abs_path.read_text(encoding="utf-8")
                updated = self.update_models(sub, existing, local_enum_names=local_enum_names)
            else:
                existing = ""
                updated = self.generate_models(sub, local_enum_names=local_enum_names)

            if updated == existing:
                continue

            diff = difflib.unified_diff(
                existing.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
            )
            diffs.append("".join(diff))

        return "".join(diffs)
