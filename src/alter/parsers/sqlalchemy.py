"""SQLAlchemy parser — converts SQLAlchemy Python files into AlterSchema objects.

Supports both SQLAlchemy 2.0 style (``Mapped[type]`` + ``mapped_column()``)
and 1.x style (``Column(Type, ...)``). Both styles may appear in the same file.

Uses Python's ``ast`` module. **Never imports or executes user files.**
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alter.errors import ParseError
from alter.parsers.base import (
    BaseParser,
    ParseResult,
    extract_imports,
    iter_py_files,
    resolve_module_to_path,
)
from alter.schema import AlterSchema, Column, EnumDef, Relation, Table
from alter.types import python_to_alter


# ---------------------------------------------------------------------------
# Internal dataclass for one-file parse results
# ---------------------------------------------------------------------------


@dataclass
class _FileResult:
    tables: list[Table] = field(default_factory=list)
    enums: list[EnumDef] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SQL type names used in SQLAlchemy 1.x Column() calls
# ---------------------------------------------------------------------------

_SQLA_TYPE_MAP: dict[str, str] = {
    "String":    "string",
    "Text":      "text",
    "Integer":   "int",
    "BigInteger":"bigint",
    "Float":     "float",
    "Numeric":   "decimal",
    "Boolean":   "bool",
    "DateTime":  "datetime",
    "Date":      "date",
    "Time":      "time",
    "JSON":      "json",
    "JSONB":     "json",
    "LargeBinary": "bytes",
    "UUID":      "uuid",
    # PostgreSQL dialect types
    "CHAR":      "string",
    "VARCHAR":   "string",
    "TEXT":      "text",
    "INTEGER":   "int",
    "BIGINT":    "bigint",
    "BOOLEAN":   "bool",
    "TIMESTAMP": "datetime",
    "TIMESTAMPTZ": "datetime",
}


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------


class SQLAlchemyParser(BaseParser):
    """Parses SQLAlchemy ORM model files into .alter schema objects.

    Handles:

    * **2.0 style**: ``class User(Base):`` with ``Mapped[T]`` annotations
      and ``mapped_column()`` calls.
    * **1.x style**: ``class User(Base):`` with plain ``Column(Type, ...)``
      assignments (no type annotations).
    * Both styles may coexist in a single file.
    * Enum subclasses → ``EnumDef`` entries.
    * ``ForeignKey("t.col")`` → ``Relation`` entries.
    * ``relationship()`` fields → skipped.

    Cross-file support
    ------------------
    ``parse_directory()`` uses a two-phase scan identical to the SQLModel
    parser so that enum-only files and base-class files are handled correctly.
    ``parse_file_result()`` follows ``from … import …`` statements to resolve
    enums defined in sibling files.
    """

    # ------------------------------------------------------------------
    # ORM detection
    # ------------------------------------------------------------------

    def detect_orm(self, path: Path) -> bool:
        """Return True if the file imports from ``sqlalchemy``."""
        try:
            source = path.read_text(encoding="utf-8")
            return (
                "from sqlalchemy" in source
                or "import sqlalchemy" in source.lower()
            )
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Public parse methods
    # ------------------------------------------------------------------

    def parse_file(self, path: Path) -> list[Table]:
        """Parse a single SQLAlchemy file and return its Table definitions."""
        result = self._parse_file_internal(path)
        return result.tables

    def parse_file_result(self, path: Path) -> ParseResult:
        """Parse a single SQLAlchemy file and return the full result (tables + enums).

        Follows ``from … import …`` statements to resolve enums from other files.
        """
        search_roots = self._search_roots(path)
        ext_enums, _ext_bases = self._resolve_imports(path, search_roots)

        file_result = self._parse_file_internal(path, known_enums=ext_enums)

        # Include imported enums that are actually used
        schema_enums = list(file_result.enums)
        local_enum_names = {e.name for e in schema_enums}
        used_types = {col.type for t in file_result.tables for col in t.columns}
        for name, enum_def in ext_enums.items():
            if name in used_types and name not in local_enum_names:
                schema_enums.append(enum_def)

        schema = AlterSchema(
            orm="sqlalchemy",
            tables=file_result.tables,
            enums=schema_enums,
            relations=file_result.relations,
        )
        return ParseResult(schema=schema, warnings=file_result.warnings)

    def parse_directory(self, directory: Path) -> ParseResult:
        """Recursively parse all Python files under *directory*.

        Uses a two-phase scan:

        * **Phase 1** — pre-scan all ``.py`` files to collect global enum
          definitions (with ``file_path`` tracking).
        * **Phase 2** — parse only ORM files, injecting the global enum
          context so that columns typed with imported enums resolve correctly.
        """
        schema = AlterSchema(orm="sqlalchemy")
        all_warnings: list[str] = []
        skipped: list[Path] = []

        py_files = iter_py_files(directory)

        # ------------------------------------------------------------------
        # Phase 1 — collect all enum definitions (enum-only files included)
        # ------------------------------------------------------------------
        global_enums: dict[str, EnumDef] = {}
        parsed_trees: dict[Path, ast.Module] = {}

        for py_file in py_files:
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(py_file))
                parsed_trees[py_file] = tree
                fp = self._relative_path(py_file)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and _is_enum_class(node):
                        enum_def = _parse_enum_class(node, file_path=fp)
                        global_enums.setdefault(node.name, enum_def)
            except Exception:  # noqa: BLE001
                pass

        # ------------------------------------------------------------------
        # Phase 2 — parse ORM files with global enum context
        # ------------------------------------------------------------------
        for py_file in py_files:
            if not self.detect_orm(py_file):
                continue
            try:
                file_result = self._parse_file_internal(
                    py_file, known_enums=global_enums
                )
                schema.tables.extend(file_result.tables)
                schema.relations.extend(file_result.relations)
                all_warnings.extend(file_result.warnings)
                for enum_def in file_result.enums:
                    if not any(e.name == enum_def.name for e in schema.enums):
                        schema.enums.append(enum_def)
            except ParseError as exc:
                all_warnings.append(str(exc))
                skipped.append(py_file)
            except Exception as exc:  # noqa: BLE001
                all_warnings.append(f"Unexpected error parsing {py_file}: {exc}")
                skipped.append(py_file)

        # Add global enums not already in schema
        existing_enum_names = {e.name for e in schema.enums}
        for enum_def in global_enums.values():
            if enum_def.name not in existing_enum_names:
                schema.enums.append(enum_def)

        from alter.parsers.base import deduplicate_tables  # avoid circular at top level
        schema.tables = deduplicate_tables(schema.tables, all_warnings)
        return ParseResult(schema=schema, warnings=all_warnings, skipped_files=skipped)

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    def _search_roots(self, path: Path) -> list[Path]:
        """Return candidate package root directories for resolving imports."""
        if self.project_root is not None:
            return [self.project_root]
        curr = path.parent
        while (curr / "__init__.py").exists() and curr != curr.parent:
            curr = curr.parent
        return [curr, path.parent]

    def _resolve_imports(
        self, path: Path, search_roots: list[Path]
    ) -> tuple[dict[str, EnumDef], dict[str, Any]]:
        """Follow import chains to collect external enum definitions.

        Returns (ext_enums, ext_bases).  SQLAlchemy base classes are not
        currently auto-inherited, so ext_bases is always empty here.
        """
        visited: set[Path] = {path.resolve()}
        deps: list[tuple[Path, str, ast.Module]] = []
        queue: list[Path] = []

        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            for imp in extract_imports(tree):
                dep = resolve_module_to_path(imp.module, search_roots, path, imp.level)
                if dep is not None and dep.resolve() not in visited:
                    queue.append(dep)
        except Exception:  # noqa: BLE001
            return {}, {}

        while queue:
            dep_path = queue.pop(0)
            dep_resolved = dep_path.resolve()
            if dep_resolved in visited:
                continue
            visited.add(dep_resolved)
            try:
                dep_src = dep_path.read_text(encoding="utf-8")
                dep_tree = ast.parse(dep_src)
                dep_fp = self._relative_path(dep_path)
                deps.append((dep_path, dep_fp, dep_tree))
                for imp in extract_imports(dep_tree):
                    tdep = resolve_module_to_path(
                        imp.module, search_roots, dep_path, imp.level
                    )
                    if tdep is not None and tdep.resolve() not in visited:
                        queue.append(tdep)
            except Exception:  # noqa: BLE001
                pass

        ext_enums: dict[str, EnumDef] = {}
        for _dep_path, dep_fp, dep_tree in deps:
            for node in ast.walk(dep_tree):
                if isinstance(node, ast.ClassDef) and _is_enum_class(node):
                    enum_def = _parse_enum_class(node, file_path=dep_fp)
                    ext_enums.setdefault(node.name, enum_def)

        return ext_enums, {}

    def _parse_file_internal(
        self,
        path: Path,
        known_enums: dict[str, EnumDef] | None = None,
    ) -> _FileResult:
        """Parse one file into tables, enums, relations, and warnings."""
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ParseError(f"Cannot read {path}: {exc}") from exc

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise ParseError(f"Syntax error in {path}: {exc}") from exc

        file_path = self._relative_path(path)
        result = _FileResult()

        # First pass: identify base class names (DeclarativeBase subclasses)
        base_class_names: set[str] = _find_declarative_bases(tree)

        # Merge external enums; local definitions take precedence
        merged_enums: dict[str, EnumDef] = dict(known_enums) if known_enums else {}

        # Second pass: collect enum classes
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _is_enum_class(node):
                enum_def = _parse_enum_class(node, file_path=file_path)
                merged_enums[enum_def.name] = enum_def  # local overrides external
                result.enums.append(enum_def)

        # Third pass: collect model classes
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not _is_model_class(node, base_class_names):
                continue
            try:
                table, relations, warns = _parse_model_class(
                    node, file_path, merged_enums
                )
                result.tables.append(table)
                result.relations.extend(relations)
                result.warnings.extend(warns)
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(
                    f"Skipped class '{node.name}' in {path}: {exc}"
                )

        return result


# ---------------------------------------------------------------------------
# AST helper functions — class detection
# ---------------------------------------------------------------------------


def _find_declarative_bases(tree: ast.Module) -> set[str]:
    """Find all class names that directly inherit from ``DeclarativeBase``."""
    base_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            base_str = _node_to_name(base)
            if base_str in ("DeclarativeBase", "declarative_base"):
                base_names.add(node.name)
                break
    # Always include "Base" as a conventional name even if not explicitly detected
    # (common pattern: Base = declarative_base())
    return base_names


def _is_model_class(node: ast.ClassDef, base_class_names: set[str]) -> bool:
    """Return True if this class is a SQLAlchemy model (not a base class).

    A class is considered a model if:
    - It inherits from a known base class name, OR
    - It has a ``__tablename__`` attribute AND has Column/mapped_column attributes

    This handles the common convention of class Base(DeclarativeBase) + class User(Base).
    """
    # Check inheritance from known base classes
    for base in node.bases:
        name = _node_to_name(base)
        if name in base_class_names:
            return True
        # Common convention: inherit from "Base"
        if name == "Base":
            return True

    # Fallback: has __tablename__ and column-like attributes
    has_tablename = _get_tablename_value(node) is not None
    has_columns = _has_columns(node)
    return has_tablename and has_columns


def _has_columns(node: ast.ClassDef) -> bool:
    """Return True if the class has Column() or mapped_column() attributes."""
    for stmt in node.body:
        # 2.0 style: mapped_column() annotation
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.value, ast.Call):
            func_name = _call_func_name(stmt.value)
            if func_name in ("mapped_column", "relationship"):
                return True
            # Mapped[X] annotation without call
            ann = stmt.annotation
            if isinstance(ann, ast.Subscript):
                outer = _node_to_name(ann.value)
                if outer == "Mapped":
                    return True
        # 1.x style: Column() assignment
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            func_name = _call_func_name(stmt.value)
            if func_name in ("Column", "relationship", "mapped_column"):
                return True
    return False


def _is_enum_class(node: ast.ClassDef) -> bool:
    """Return True if the class inherits from Enum."""
    for base in node.bases:
        name = _node_to_name(base)
        if name in ("Enum", "IntEnum", "StrEnum"):
            return True
    return False


def _parse_enum_class(node: ast.ClassDef, file_path: str | None = None) -> EnumDef:
    """Extract enum name, member names, and values."""
    from alter.schema import EnumMember
    values: list[EnumMember] = []
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    member_name = target.id
                    if isinstance(stmt.value, ast.Constant) and isinstance(
                        stmt.value.value, str
                    ):
                        values.append(EnumMember(member_name=member_name, value=stmt.value.value))
                    else:
                        values.append(EnumMember(member_name=member_name, value=member_name))
    return EnumDef(name=node.name, values=values, file_path=file_path)


# ---------------------------------------------------------------------------
# AST helper functions — model class parsing
# ---------------------------------------------------------------------------


def _parse_model_class(
    node: ast.ClassDef,
    file_path: str,
    known_enums: dict[str, EnumDef],
) -> tuple[Table, list[Relation], list[str]]:
    """Parse one SQLAlchemy model class into a Table, Relations, and warnings."""
    tablename = _get_tablename_value(node) or node.name.lower()
    columns: list[Column] = []
    relations: list[Relation] = []
    warns: list[str] = []

    for stmt in node.body:
        # --- 2.0 style: annotated assignments with Mapped[X] ---
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            field_name = stmt.target.id
            annotation = stmt.annotation
            value = stmt.value

            # Skip non-Mapped annotations (e.g. ClassVar)
            if not _is_mapped_annotation(annotation):
                continue

            # Skip relationships
            if value is not None and _is_relationship_call(value):
                continue

            try:
                col, rel, col_warns = _parse_mapped_column(
                    field_name, annotation, value, tablename, known_enums
                )
            except Exception as exc:  # noqa: BLE001
                warns.append(f"  Skipped {tablename}.{field_name}: {exc}")
                continue

            if col is None:
                continue  # relationship or skipped
            columns.append(col)
            if rel is not None:
                relations.append(rel)
            warns.extend(col_warns)

        # --- 1.x style: plain assignments with Column(...) ---
        elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            func_name = _call_func_name(stmt.value)
            if func_name not in ("Column",):
                continue
            for target in stmt.targets:
                if not isinstance(target, ast.Name):
                    continue
                field_name = target.id
                try:
                    col, rel, col_warns = _parse_column_call(
                        field_name, stmt.value, tablename, known_enums
                    )
                except Exception as exc:  # noqa: BLE001
                    warns.append(f"  Skipped {tablename}.{field_name}: {exc}")
                    continue
                if col is None:
                    continue
                columns.append(col)
                if rel is not None:
                    relations.append(rel)
                warns.extend(col_warns)

    table = Table(name=tablename, file_path=file_path, columns=columns)
    return table, relations, warns


def _is_mapped_annotation(annotation: ast.expr) -> bool:
    """Return True if the annotation is ``Mapped[X]``."""
    if isinstance(annotation, ast.Subscript):
        outer = _node_to_name(annotation.value)
        return outer == "Mapped"
    return False


def _is_relationship_call(value: ast.expr) -> bool:
    """Return True if the value is a ``relationship(...)`` call."""
    if not isinstance(value, ast.Call):
        return False
    name = _call_func_name(value)
    return name in ("relationship", "Relationship")


def _parse_mapped_column(
    field_name: str,
    annotation: ast.expr,
    value: ast.expr | None,
    table_name: str,
    known_enums: dict[str, EnumDef],
) -> tuple[Column | None, Relation | None, list[str]]:
    """Parse a 2.0-style ``field: Mapped[T] = mapped_column(...)`` declaration."""
    warns: list[str] = []

    # Unwrap Mapped[X]
    if not isinstance(annotation, ast.Subscript):
        return None, None, warns
    inner = annotation.slice

    # Resolve inner type and nullability
    alter_type, is_nullable = _resolve_mapped_type(inner, known_enums)
    if alter_type == "_relationship":
        return None, None, warns

    # Parse mapped_column() kwargs
    nullable = is_nullable
    primary_key = False
    unique = False
    index = False
    max_length: int | None = None
    foreign_key: str | None = None
    default: str | None = None

    if value is not None and isinstance(value, ast.Call):
        func_name = _call_func_name(value)
        if func_name == "mapped_column":
            for kw in value.keywords:
                if kw.arg == "primary_key" and _const_bool(kw.value) is True:
                    primary_key = True
                    nullable = False
                elif kw.arg == "unique" and _const_bool(kw.value) is True:
                    unique = True
                elif kw.arg == "index" and _const_bool(kw.value) is True:
                    index = True
                elif kw.arg == "nullable":
                    v = _const_bool(kw.value)
                    if v is not None:
                        nullable = v
                elif kw.arg == "default":
                    default, extra_null = _extract_default_value(kw.value)
                    if extra_null:
                        nullable = True

            # Check positional args for ForeignKey(...)
            for arg in value.args:
                fk = _extract_foreignkey_arg(arg)
                if fk:
                    foreign_key = fk

    col = Column(
        name=field_name,
        type=alter_type,
        primary_key=primary_key,
        nullable=nullable,
        unique=unique,
        index=index,
        max_length=max_length,
        foreign_key=foreign_key,
        default=default,
    )
    rel = _make_relation(table_name, col)
    return col, rel, warns


def _parse_column_call(
    field_name: str,
    call: ast.Call,
    table_name: str,
    known_enums: dict[str, EnumDef],
) -> tuple[Column | None, Relation | None, list[str]]:
    """Parse a 1.x-style ``field = Column(Type, ...)`` declaration."""
    warns: list[str] = []

    if not call.args:
        return None, None, warns

    # First positional arg is the SQL type
    type_arg = call.args[0]
    alter_type, max_length = _resolve_column_type_arg(type_arg, known_enums)

    nullable = True  # SQLAlchemy 1.x default is nullable=True
    primary_key = False
    unique = False
    index = False
    foreign_key: str | None = None
    default: str | None = None

    # Remaining positional args may include ForeignKey(...)
    for arg in call.args[1:]:
        fk = _extract_foreignkey_arg(arg)
        if fk:
            foreign_key = fk

    for kw in call.keywords:
        if kw.arg == "primary_key" and _const_bool(kw.value) is True:
            primary_key = True
            nullable = False
        elif kw.arg == "unique" and _const_bool(kw.value) is True:
            unique = True
        elif kw.arg == "index" and _const_bool(kw.value) is True:
            index = True
        elif kw.arg == "nullable":
            v = _const_bool(kw.value)
            if v is not None:
                nullable = v
        elif kw.arg == "default":
            default, extra_null = _extract_default_value(kw.value)
            if extra_null:
                nullable = True

    col = Column(
        name=field_name,
        type=alter_type,
        primary_key=primary_key,
        nullable=nullable,
        unique=unique,
        index=index,
        max_length=max_length,
        foreign_key=foreign_key,
        default=default,
    )
    rel = _make_relation(table_name, col)
    return col, rel, warns


# ---------------------------------------------------------------------------
# Type resolution helpers
# ---------------------------------------------------------------------------


def _resolve_mapped_type(
    inner: ast.expr, known_enums: dict[str, EnumDef]
) -> tuple[str, bool]:
    """Resolve the inner type of ``Mapped[X]`` to (alter_type, is_nullable)."""
    # Optional[X] → nullable
    if isinstance(inner, ast.Subscript):
        outer_name = _node_to_name(inner.value)
        if outer_name == "Optional":
            inner2_type, _ = _resolve_mapped_type(inner.slice, known_enums)
            return inner2_type, True
        # list[X] → relationship
        if outer_name in ("list", "List"):
            return "_relationship", False

    # X | None
    if isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.BitOr):
        if isinstance(inner.right, ast.Constant) and inner.right.value is None:
            t, _ = _resolve_mapped_type(inner.left, known_enums)
            return t, True
        if isinstance(inner.left, ast.Constant) and inner.left.value is None:
            t, _ = _resolve_mapped_type(inner.right, known_enums)
            return t, True

    # Quoted forward ref
    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
        name = inner.value
        if name in known_enums:
            return name, False
        return "_relationship", False

    type_str = _node_to_type_str(inner)
    return _type_str_to_alter(type_str, known_enums), False


def _resolve_column_type_arg(
    node: ast.expr, known_enums: dict[str, EnumDef]
) -> tuple[str, int | None]:
    """Resolve the SQLAlchemy type from a 1.x Column() first argument.

    Returns (alter_type, max_length).
    """
    max_length: int | None = None

    # String(255) or VARCHAR(255) — Call node with optional length arg
    if isinstance(node, ast.Call):
        type_name = _node_to_name(node.func)
        alter_type = _SQLA_TYPE_MAP.get(type_name, "string")
        # Extract length from first positional arg if present
        if node.args and isinstance(node.args[0], ast.Constant):
            v = node.args[0].value
            if isinstance(v, int):
                max_length = v
        return alter_type, max_length

    # Plain name: Integer, String, Boolean, etc.
    if isinstance(node, ast.Name):
        return _SQLA_TYPE_MAP.get(node.id, "string"), None

    # Attribute: sa.Integer, dialects.postgresql.UUID, etc.
    if isinstance(node, ast.Attribute):
        return _SQLA_TYPE_MAP.get(node.attr, "string"), None

    return "string", None


def _extract_foreignkey_arg(arg: ast.expr) -> str | None:
    """Extract ForeignKey("table.column") string from a Column positional arg."""
    if not isinstance(arg, ast.Call):
        return None
    func_name = _call_func_name(arg)
    if func_name != "ForeignKey":
        return None
    if arg.args and isinstance(arg.args[0], ast.Constant):
        return str(arg.args[0].value)  # preserve verbatim — schema prefix must round-trip
    return None


def _extract_default_value(node: ast.expr) -> tuple[str | None, bool]:
    """Extract a default value and whether it implies nullable."""
    if isinstance(node, ast.Constant) and node.value is None:
        return None, True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    if isinstance(node, ast.Constant):
        return str(node.value).lower(), False
    if isinstance(node, ast.Attribute):
        return node.attr, False
    # Dict literal: default={}
    if isinstance(node, ast.Dict):
        return "{}", False
    # List literal: default=[]
    if isinstance(node, ast.List):
        return "[]", False
    return None, False


# ---------------------------------------------------------------------------
# Shared utility functions
# ---------------------------------------------------------------------------


def _get_tablename_value(node: ast.ClassDef) -> str | None:
    """Return the __tablename__ string value if present, else None."""
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "__tablename__":
                    if isinstance(stmt.value, ast.Constant) and isinstance(
                        stmt.value.value, str
                    ):
                        return stmt.value.value
    return None


def _node_to_name(node: ast.expr) -> str:
    """Return the simple name of a Name or Attribute node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _node_to_type_str(node: ast.expr) -> str:
    """Convert an AST annotation to a dotted Python type string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_node_to_type_str(node.value)}.{node.attr}"
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Subscript):
        return f"{_node_to_type_str(node.value)}[{_node_to_type_str(node.slice)}]"
    return "unknown"


def _type_str_to_alter(type_str: str, known_enums: dict[str, EnumDef]) -> str:
    """Convert a Python type string to an .alter type string."""
    if type_str in known_enums:
        return type_str
    # Check SQLAlchemy type map first (for unqualified names like "UUID")
    if type_str in _SQLA_TYPE_MAP:
        return _SQLA_TYPE_MAP[type_str]
    try:
        return python_to_alter(type_str)
    except KeyError:
        return "string"


def _call_func_name(call: ast.Call) -> str:
    """Return the simple function name from a Call node."""
    return _node_to_name(call.func)


def _const_bool(node: ast.expr) -> bool | None:
    """Return a bool Constant value, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _make_relation(table_name: str, col: Column) -> Relation | None:
    """Build a Relation from a foreign_key column.

    ``col.foreign_key`` is stored verbatim (e.g. ``"table.col"`` or
    ``"schema.table.col"``).  ``Relation.to_table`` holds the unqualified
    table name so the canvas can render it without schema prefixes.
    """
    if not col.foreign_key:
        return None
    parts = col.foreign_key.rsplit(".", 1)
    if len(parts) != 2:
        return None
    to_table_raw, to_column = parts
    to_table = to_table_raw.rsplit(".", 1)[-1]
    return Relation(
        name=f"{table_name}_{col.name}_fkey",
        from_table=table_name,
        from_column=col.name,
        to_table=to_table,
        to_column=to_column,
        type="many-to-one",
        on_delete="CASCADE",
    )
