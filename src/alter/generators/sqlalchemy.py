"""SQLAlchemy 2.0 code generator.

Converts an ``AlterSchema`` into valid SQLAlchemy 2.0 declarative-style
Python source code.  Supports the same three modes as the SQLModel generator:
``generate_models()``, ``update_models()``, and ``preview_apply()``.
"""

from __future__ import annotations

import ast

from alter.generators._surgical import surgical_update_class, surgical_update_enum_class

from alter.generators.base import (
    BaseGenerator,
    _class_name,
    _collect_stdlib_imports,
    _safe_member_name,
    generate_enum_class,
)
from alter.schema import AlterSchema, Column, EnumDef, Table
from alter.types import alter_to_python


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
    "json":       "JSON",
    "json_array": "JSON",
    "bytes":      "LargeBinary",
}


# ---------------------------------------------------------------------------
# SQLAlchemy-specific column helpers
# ---------------------------------------------------------------------------


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
        args.append("default=lambda: datetime.now(timezone.utc)")
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


# ---------------------------------------------------------------------------
# SQLAlchemyGenerator
# ---------------------------------------------------------------------------


class SQLAlchemyGenerator(BaseGenerator):
    """Generates SQLAlchemy 2.0 Python source code from an ``AlterSchema``."""

    # ------------------------------------------------------------------
    # ORM-specific imports
    # ------------------------------------------------------------------

    def _build_imports(
        self,
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
        lines = _collect_stdlib_imports(schema, enum_names, emit_enum_names)

        sa_types = _collect_sa_type_imports(schema, enum_names)
        if sa_types:
            sorted_types = ", ".join(sorted(sa_types))
            lines.append(f"from sqlalchemy import {sorted_types}")

        orm_imports = ["DeclarativeBase", "Mapped", "mapped_column"]
        lines.append(f"from sqlalchemy.orm import {', '.join(orm_imports)}")

        return lines

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

        import_lines = self._build_imports(schema, all_enum_names, emit_enum_names=emit_enum_names)
        parts.append("\n".join(import_lines))

        # Base class declaration
        parts.append("class Base(DeclarativeBase):\n    pass")

        for enum in schema.enums:
            if enum.name in emit_enum_names:
                parts.append(generate_enum_class(enum))

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
                new_src = generate_enum_class(enum_by_class[cls_name])

            if not result.endswith("\n\n"):
                result = result.rstrip("\n") + "\n\n"
            result += new_src + "\n"

        # Ensure all imports needed by the (possibly updated) schema are present.
        result = self._insert_missing_imports(
            result, schema, all_enum_names, emit_enum_names=emit_enum_names
        )

        return result
