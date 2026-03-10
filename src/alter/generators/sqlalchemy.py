"""SQLAlchemy 2.0 code generator.

Converts an ``AlterSchema`` into valid SQLAlchemy 2.0 declarative-style
Python source code.  Supports the same three modes as the SQLModel generator:
``generate_models()``, ``update_models()``, and ``preview_apply()``.
"""

from __future__ import annotations

import ast
import difflib
from pathlib import Path

from alter.generators._surgical import surgical_update_class, surgical_update_enum_class

from alter.generators.base import BaseGenerator
from alter.schema import AlterSchema, Column, EnumDef, Table
from alter.types import alter_to_python, alter_to_sql, is_enum_type


# ---------------------------------------------------------------------------
# SQLAlchemy type-name map  (alter type → SA import name)
# ---------------------------------------------------------------------------

_SQLA_TYPE: dict[str, str] = {
    "uuid":     "Uuid",
    "string":   "String",
    "text":     "Text",
    "int":      "Integer",
    "bigint":   "BigInteger",
    "float":    "Float",
    "decimal":  "Numeric",
    "bool":     "Boolean",
    "datetime": "DateTime",
    "date":     "Date",
    "time":     "Time",
    "json":     "JSON",
    "bytes":    "LargeBinary",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _class_name(table_name: str) -> str:
    return "".join(w.capitalize() for w in table_name.split("_"))


def _mapped_type(col: Column, enum_names: set[str]) -> str:
    """Return the Python type used inside ``Mapped[...]``."""
    if col.type in enum_names:
        base = col.type
    else:
        base = alter_to_python(col.type)
    if col.nullable and not col.primary_key:
        return f"Optional[{base}]"
    return base


def _sa_type_expr(col: Column, enum_names: set[str]) -> str | None:
    """Return the SQLAlchemy type expression, e.g. ``String(255)`` or ``None``."""
    if col.type in enum_names:
        return None  # SA Enum handled via python_enum arg
    name = _SQLA_TYPE.get(col.type)
    if name is None:
        return None
    if name == "String" and col.max_length:
        return f"String({col.max_length})"
    return name


def _mapped_column_args(col: Column, enum_names: set[str]) -> str:
    """Return the ``mapped_column(...)`` argument list."""
    args: list[str] = []

    # SA type expression (first positional)
    type_expr = _sa_type_expr(col, enum_names)
    if type_expr:
        args.append(type_expr)

    if col.primary_key:
        args.append("primary_key=True")
    if col.foreign_key:
        args.append(f'ForeignKey("{col.foreign_key}")')
    if col.unique:
        args.append("unique=True")
    if col.index:
        args.append("index=True")
    if col.nullable and not col.primary_key:
        args.append("nullable=True")
    elif not col.nullable:
        args.append("nullable=False")

    # default
    if col.default and col.default.startswith("expr:"):
        args.append(f"default={col.default[5:]}")
    elif col.default == "uuid4":
        args.append("default=uuid.uuid4")
    elif col.default == "utcnow":
        args.append("default=datetime.utcnow")
    elif col.default == "now":
        args.append("default=datetime.now")
    elif col.default == "{}":
        args.append("default=dict")
    elif col.default == "[]":
        args.append("default=list")
    elif col.default is not None:
        raw = col.default
        if raw == "true":
            args.append("default=True")
        elif raw == "false":
            args.append("default=False")
        elif raw.lstrip("-").isdigit():
            args.append(f"default={raw}")
        elif col.type in enum_names:
            args.append(f"default={col.type}.{raw}")
        else:
            args.append(f'default="{raw}"')

    # Passthrough kwargs
    if col.extra_kwargs:
        for k, v in col.extra_kwargs.items():
            args.append(f"{k}={v}")

    return ", ".join(args)


def _column_line(col: Column, enum_names: set[str]) -> str:
    """Return one ``    name: Mapped[T] = mapped_column(...)`` line."""
    mapped_t = _mapped_type(col, enum_names)
    mc_args = _mapped_column_args(col, enum_names)
    return f"    {col.name}: Mapped[{mapped_t}] = mapped_column({mc_args})"


def _enum_class_source(enum: EnumDef) -> str:
    import keyword
    from alter.schema import EnumMember
    lines = [f"class {enum.name}(str, Enum):"]
    for v in enum.values:
        if isinstance(v, EnumMember):
            mname = f"{v.member_name}_" if keyword.iskeyword(v.member_name) else v.member_name
            lines.append(f'    {mname} = "{v.value}"')
        else:
            mname = f"{v}_" if keyword.iskeyword(v) else v
            lines.append(f'    {mname} = "{v}"')
    return "\n".join(lines)


def _model_class_source(
    table: Table,
    enum_names: set[str],
    class_name: str | None = None,
) -> str:
    """Return source for a SQLAlchemy model class.

    Args:
        class_name: If provided, use this name instead of the conventional
            PascalCase of ``table.name``. Used by ``update_models()`` to
            preserve existing hand-written class names (e.g. ``User`` rather
            than the generated ``Users``).
    """
    name = class_name if class_name is not None else _class_name(table.name)
    lines: list[str] = [f"class {name}(Base):"]
    lines.append(f'    __tablename__ = "{table.name}"')
    lines.append("")
    # Only emit non-inherited columns — inherited ones live in base classes
    for col in table.columns:
        if not col.inherited:
            lines.append(_column_line(col, enum_names))
    return "\n".join(lines)


def _collect_sa_type_imports(schema: AlterSchema, enum_names: set[str]) -> set[str]:
    """Return the set of SA type names that need to be imported."""
    names: set[str] = set()
    for table in schema.tables:
        for col in table.columns:
            if col.type in enum_names:
                continue
            name = _SQLA_TYPE.get(col.type)
            if name:
                names.add(name)
        # ForeignKey always needed if any FK exists
        if any(c.foreign_key for c in table.columns):
            names.add("ForeignKey")
    return names


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
        lines.insert(insert_after, "\n".join(missing) + "\n")
    else:
        lines.insert(0, "\n".join(missing) + "\n")

    return "".join(lines)


def _build_imports(
    schema: AlterSchema,
    enum_names: set[str],
    emit_enum_names: set[str] | None = None,
) -> list[str]:
    """Return ordered import lines required by *schema*.

    Args:
        enum_names: All known enum names for type-hint resolution.
        emit_enum_names: If provided, only add ``from enum import Enum`` when
            this set is non-empty.  Defaults to *enum_names* when ``None``.
    """
    if emit_enum_names is None:
        emit_enum_names = enum_names

    needs_uuid = False
    needs_datetime = False
    needs_optional = False
    needs_decimal = False

    for table in schema.tables:
        for col in table.columns:
            py = alter_to_python(col.type) if col.type not in enum_names else col.type
            if "uuid" in py.lower():
                needs_uuid = True
            if py in ("datetime", "date", "time"):
                needs_datetime = True
            if col.nullable and not col.primary_key:
                needs_optional = True
            if py == "Decimal":
                needs_decimal = True

    lines: list[str] = []
    if needs_uuid:
        lines.append("import uuid")
    if needs_datetime:
        lines.append("from datetime import datetime")
    if emit_enum_names:
        lines.append("from enum import Enum")
    if needs_decimal:
        lines.append("from decimal import Decimal")
    if needs_optional:
        lines.append("from typing import Optional")

    sa_types = _collect_sa_type_imports(schema, enum_names)
    if sa_types:
        sorted_types = ", ".join(sorted(sa_types))
        lines.append(f"from sqlalchemy import {sorted_types}")

    orm_imports = ["DeclarativeBase", "Mapped", "mapped_column"]
    lines.append(f"from sqlalchemy.orm import {', '.join(orm_imports)}")

    return lines


# ---------------------------------------------------------------------------
# SQLAlchemyGenerator
# ---------------------------------------------------------------------------


class SQLAlchemyGenerator(BaseGenerator):
    """Generates SQLAlchemy 2.0 Python source code from an ``AlterSchema``."""

    # ------------------------------------------------------------------
    # 1. Full generation
    # ------------------------------------------------------------------

    def generate_models(
        self,
        schema: AlterSchema,
        local_enum_names: set[str] | None = None,
    ) -> str:
        """Generate a complete SQLAlchemy models file from *schema*.

        Args:
            local_enum_names: If provided, only emit enum class definitions for
                names in this set.  Type resolution always uses all enums in
                *schema*.
        """
        all_enum_names: set[str] = {e.name for e in schema.enums}
        emit_enum_names = local_enum_names if local_enum_names is not None else all_enum_names
        parts: list[str] = []

        import_lines = _build_imports(schema, all_enum_names, emit_enum_names=emit_enum_names)
        parts.append("\n".join(import_lines))

        # Base class declaration
        parts.append("class Base(DeclarativeBase):\n    pass")

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
                for names in this set.  Type resolution always uses all enums in
                *schema*.
        """
        all_enum_names: set[str] = {e.name for e in schema.enums}
        emit_enum_names = local_enum_names if local_enum_names is not None else all_enum_names

        try:
            tree = ast.parse(existing_code)
        except SyntaxError:
            return self.generate_models(schema, local_enum_names=local_enum_names)

        lines = existing_code.splitlines(keepends=True)

        existing_classes: dict[str, tuple[int, int]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                existing_classes[node.name] = (node.lineno, node.end_lineno)

        # Build tablename → existing class name map by scanning __tablename__ assignments.
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

        replacements: list[tuple[int, int, list[str]]] = []
        for cls_name, (start, end) in existing_classes.items():
            if cls_name not in schema_classes:
                # Leave untouched — destructive removal requires explicit
                # confirmation at the CLI/MCP layer, not inside the generator.
                continue
            if cls_name in table_by_class:
                # Surgical update: only include non-inherited columns
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
                import keyword
                from alter.schema import EnumMember
                schema_value_lines = []
                for v in enum_by_class[cls_name].values:
                    if isinstance(v, EnumMember):
                        mname = f"{v.member_name}_" if keyword.iskeyword(v.member_name) else v.member_name
                        schema_value_lines.append(f'    {mname} = "{v.value}"')
                    else:
                        mname = f"{v}_" if keyword.iskeyword(v) else v
                        schema_value_lines.append(f'    {mname} = "{v}"')
                class_source = "".join(lines[start - 1 : end])
                patched = surgical_update_enum_class(class_source, schema_value_lines)
                if patched is None:
                    continue
                replacements.append((start, end, patched))

        replacements.sort(key=lambda r: r[0], reverse=True)
        for start, end, patched_lines in replacements:
            lines[start - 1 : end] = patched_lines

        result = "".join(lines)

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
        result = _ensure_imports(result, schema, all_enum_names, emit_enum_names=emit_enum_names)

        return result

    # ------------------------------------------------------------------
    # 3. Preview (dry-run diff)
    # ------------------------------------------------------------------

    def preview_apply(self, schema: AlterSchema, project_root: Path) -> str:
        """Return unified diff of all files that WOULD change. Writes nothing."""
        file_tables: dict[str, list[Table]] = {}
        for table in schema.tables:
            fp = table.file_path or "app/models.py"
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
