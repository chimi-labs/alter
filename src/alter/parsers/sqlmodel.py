"""SQLModel parser — converts SQLModel Python files into AlterSchema objects.

Uses Python's ``ast`` module for static analysis. **Never imports or executes
the user's model files** — no side effects, no dependency on the user's venv.
"""

from __future__ import annotations

import ast
import warnings as _warnings_module
from pathlib import Path
from typing import Any

from alter.errors import ParseError
from alter.parsers.base import (
    BaseParser,
    ParseResult,
    _FileResult,
    _const_bool,
    _get_table_schema,
    _is_enum_class,
    _make_relation,
    _node_to_type_str,
    _parse_enum_class,
    deduplicate_tables,
    extract_imports,
    iter_py_files,
    resolve_module_to_path,
)
from alter.schema import AlterSchema, Column, EnumDef, Relation, Table
from alter.types import python_to_alter


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------


class SQLModelParser(BaseParser):
    """Parses SQLModel ORM model files into .alter schema objects.

    Works by walking the AST of each Python file. Detects:

    * Enum subclasses → ``EnumDef`` entries
    * ``class X(SQLModel, table=True)`` → ``Table`` entries
    * ``Field(...)`` keyword arguments → ``Column`` properties
    * ``Field(foreign_key="t.col")`` → ``Relation`` entries
    * ``Relationship(...)`` and ``list["X"]`` type hints → skipped
    * ``__tablename__`` class attribute → table name override

    Cross-file support
    ------------------
    ``parse_directory()`` runs a **two-phase** scan:

    1. Pre-scan every ``.py`` file (regardless of ORM imports) to collect all
       enum definitions and non-table SQLModel base classes globally.
    2. Parse only ORM files (those that pass ``detect_orm``), injecting the
       global enum + base-class context so that:

       * Columns typed with an imported enum resolve correctly instead of
         falling back to ``"string"``.
       * Tables that inherit from base classes (e.g. ``UUIDBase``,
         ``TimestampedBase``) include the base-class fields.

    ``parse_file_result()`` performs the same resolution by following
    ``from … import …`` statements transitively.
    """

    # ------------------------------------------------------------------
    # ORM detection
    # ------------------------------------------------------------------

    def detect_orm(self, path: Path) -> bool:
        """Return True if the file imports from ``sqlmodel``."""
        try:
            source = path.read_text(encoding="utf-8")
            return "from sqlmodel import" in source or "import sqlmodel" in source.lower()
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Public parse methods
    # ------------------------------------------------------------------

    def parse_file(self, path: Path) -> list[Table]:
        """Parse a single SQLModel file and return its Table definitions.

        Raises:
            ParseError: if the file cannot be read or contains a syntax error.
        """
        result = self._parse_file_internal(path)
        return result.tables

    def parse_file_result(self, path: Path) -> ParseResult:
        """Parse a single SQLModel file and return the full result (tables + enums).

        Follows ``from … import …`` statements transitively to resolve enums
        and base classes defined in sibling/parent files.

        Raises:
            ParseError: if the file cannot be read or contains a syntax error.
        """
        search_roots = self._search_roots(path)
        ext_enums, ext_bases = self._resolve_imports(path, search_roots)

        file_result = self._parse_file_internal(
            path, known_enums=ext_enums, known_bases=ext_bases
        )

        # Include imported enums that are actually used by columns in the schema
        schema_enums = list(file_result.enums)
        local_enum_names = {e.name for e in schema_enums}
        used_types = {col.type for t in file_result.tables for col in t.columns}
        for name, enum_def in ext_enums.items():
            if name in used_types and name not in local_enum_names:
                schema_enums.append(enum_def)

        schema = AlterSchema(
            orm="sqlmodel",
            tables=file_result.tables,
            enums=schema_enums,
            relations=file_result.relations,
        )
        return ParseResult(schema=schema, warnings=file_result.warnings)

    def parse_directory(self, directory: Path) -> ParseResult:
        """Recursively parse all Python files under *directory*.

        Uses a two-phase approach:

        * **Phase 1** — pre-scan *all* ``.py`` files (no ORM-detection filter)
          to build a global map of enum definitions and non-table SQLModel base
          classes.  Enum files that have no SQLModel import (e.g. ``enums.py``)
          are included here.

        * **Phase 2** — parse only the ORM files (``detect_orm`` returns True),
          injecting the global enum/base-class context collected in Phase 1.

        Files with syntax errors are logged to ``ParseResult.skipped_files``.
        """
        schema = AlterSchema(orm="sqlmodel", strict=False)
        all_warnings: list[str] = []
        skipped: list[Path] = []

        # list() materialises the result so that both Phase 1 and Phase 2 can
        # iterate it independently.  iter_py_files already returns a list, but
        # the explicit wrap guards against any future change to a generator.
        py_files = list(iter_py_files(directory))

        # ------------------------------------------------------------------
        # Phase 1a — collect all enum definitions from every .py file
        # ------------------------------------------------------------------
        global_enums, parsed_trees = self._phase1_collect_enums(py_files)

        # ------------------------------------------------------------------
        # Phase 1b — collect non-table SQLModel base classes (with enums)
        # ------------------------------------------------------------------
        global_bases: dict[str, list[Column]] = {}

        for py_file, tree in parsed_trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and _is_sqlmodel_base_class(node):
                    cols = _extract_base_class_columns(node, global_enums)
                    if cols:
                        global_bases.setdefault(node.name, cols)

        # ------------------------------------------------------------------
        # Phase 2 — parse ORM files with global context
        # ------------------------------------------------------------------
        for py_file in py_files:
            if not self.detect_orm(py_file):
                continue
            try:
                file_result = self._parse_file_internal(
                    py_file,
                    known_enums=global_enums,
                    known_bases=global_bases,
                )
                schema.tables.extend(file_result.tables)
                schema.relations.extend(file_result.relations)
                all_warnings.extend(file_result.warnings)
                # Only add enums defined in this file (avoid duplicating globals)
                for enum_def in file_result.enums:
                    if not any(e.name == enum_def.name for e in schema.enums):
                        schema.enums.append(enum_def)
            except ParseError as exc:
                all_warnings.append(str(exc))
                skipped.append(py_file)
            except Exception as exc:  # noqa: BLE001
                all_warnings.append(f"Unexpected error parsing {py_file}: {exc}")
                skipped.append(py_file)

        # Add global enums not already in schema (e.g. from enum-only files).
        # The post-filter below will remove any that are not actually referenced
        # by a table column, so it is safe to add them all here first.
        existing_enum_names = {e.name for e in schema.enums}
        for enum_def in global_enums.values():
            if enum_def.name not in existing_enum_names:
                schema.enums.append(enum_def)

        schema.tables = deduplicate_tables(schema.tables, all_warnings)

        # Post-filter: only keep enums that are actually referenced by at least
        # one column in a parsed table.  This removes enums swept up from DTO
        # files, Pydantic-only models, and utility scripts that share a directory
        # with the real SQLModel models.
        referenced_types: set[str] = {
            col.type for table in schema.tables for col in table.columns
        }
        schema.enums = [e for e in schema.enums if e.name in referenced_types]

        return ParseResult(schema=schema, warnings=all_warnings, skipped_files=skipped)

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    def _resolve_imports(
        self, path: Path, search_roots: list[Path]
    ) -> tuple[dict[str, EnumDef], dict[str, list[Column]]]:
        """Follow ``from … import …`` chains to collect external enums and bases.

        Uses BFS over the import graph (circular imports are safe via a visited
        set).  Two sub-passes are performed so that enums are available when
        base-class column types are resolved.
        """
        deps = self._collect_import_deps(path, search_roots)

        # Sub-pass A: collect enums (with file_path)
        ext_enums: dict[str, EnumDef] = {}
        for _dep_path, dep_fp, dep_tree in deps:
            for node in ast.walk(dep_tree):
                if isinstance(node, ast.ClassDef) and _is_enum_class(node):
                    enum_def = _parse_enum_class(node, file_path=dep_fp)
                    ext_enums.setdefault(node.name, enum_def)

        # Sub-pass B: collect non-table base classes (enums now available)
        ext_bases: dict[str, list[Column]] = {}
        for _dep_path, _dep_fp, dep_tree in deps:
            for node in ast.walk(dep_tree):
                if isinstance(node, ast.ClassDef) and _is_sqlmodel_base_class(node):
                    cols = _extract_base_class_columns(node, ext_enums)
                    if cols:
                        ext_bases.setdefault(node.name, cols)

        return ext_enums, ext_bases

    def _parse_file_internal(
        self,
        path: Path,
        known_enums: dict[str, EnumDef] | None = None,
        known_bases: dict[str, list[Column]] | None = None,
    ) -> _FileResult:
        """Parse one file; return tables, enums, relations, and warnings.

        Args:
            known_enums: External enum definitions (from other files) to merge
                into the per-file enum context.  Local definitions take
                precedence over external ones with the same name.
            known_bases: External non-table base class column lists (keyed by
                class name) to inherit from when a table class inherits them.
        """
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

        # Merge external enums; local definitions override external ones
        merged_enums: dict[str, EnumDef] = dict(known_enums) if known_enums else {}

        # First pass: collect enum class names defined in this file
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _is_enum_class(node):
                enum_def = _parse_enum_class(node, file_path=file_path)
                merged_enums[enum_def.name] = enum_def  # local overrides external
                result.enums.append(enum_def)

        # Second pass: collect table classes
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _is_sqlmodel_table(node):
                try:
                    table, relations, warns = _parse_table_class(
                        node, file_path, merged_enums, known_bases=known_bases or {}
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
# AST helper functions — SQLModel-specific
# ---------------------------------------------------------------------------


def _is_sqlmodel_table(node: ast.ClassDef) -> bool:
    """Return True if the class is ``class X(SQLModel, table=True)``."""
    for kw in node.keywords:
        if (
            kw.arg == "table"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
        ):
            return True
    return False


def _is_sqlmodel_base_class(node: ast.ClassDef) -> bool:
    """Return True if class inherits from SQLModel but is NOT a table.

    These are mixin/base classes (e.g. ``UUIDBase``, ``TimestampedBase``)
    whose fields are inherited by concrete table classes.
    """
    if _is_sqlmodel_table(node):
        return False
    for base in node.bases:
        name = base.id if isinstance(base, ast.Name) else (
            base.attr if isinstance(base, ast.Attribute) else None
        )
        if name == "SQLModel":
            return True
    return False


def _extract_base_class_columns(
    node: ast.ClassDef,
    known_enums: dict[str, EnumDef],
) -> list[Column]:
    """Extract Column objects from a non-table SQLModel base class body."""
    columns: list[Column] = []
    for stmt in node.body:
        if not isinstance(stmt, ast.AnnAssign):
            continue
        if not isinstance(stmt.target, ast.Name):
            continue

        field_name = stmt.target.id
        annotation = stmt.annotation
        value = stmt.value

        if value is not None and _is_relationship_call(value):
            continue
        if _annotation_is_list(annotation):
            continue

        try:
            alter_type, is_optional = _resolve_annotation(annotation, known_enums)
        except Exception:  # noqa: BLE001
            _warnings_module.warn(
                f"alterdb: could not resolve type for base-class field "
                f"'{node.name}.{field_name}' — column skipped. "
                "Consider using a supported type or sa_column=Column(JSON).",
                stacklevel=2,
            )
            continue

        if alter_type == "_relationship":
            continue

        col, _ = _parse_field_call(field_name, alter_type, is_optional, value, known_enums)
        columns.append(col)
    return columns


def _get_tablename(node: ast.ClassDef) -> str:
    """Return __tablename__ value or the class name lowercased."""
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "__tablename__":
                    if isinstance(stmt.value, ast.Constant) and isinstance(
                        stmt.value.value, str
                    ):
                        return stmt.value.value
    return node.name.lower()


def _parse_table_class(
    node: ast.ClassDef,
    file_path: str,
    known_enums: dict[str, EnumDef],
    known_bases: dict[str, list[Column]] | None = None,
) -> tuple[Table, list[Relation], list[str]]:
    """Parse a SQLModel table class into a Table, its Relations, and warnings.

    Inherited columns (from base classes listed in *known_bases*) are prepended
    before the class body's own fields.  A locally-defined field with the same
    name overrides the inherited one.
    """
    table_name = _get_tablename(node)
    schema_name = _get_table_schema(node)
    relations: list[Relation] = []
    warns: list[str] = []

    # ------------------------------------------------------------------
    # Collect inherited columns from base classes (in MRO order)
    # ------------------------------------------------------------------
    inherited_columns: list[Column] = []
    inherited_names: set[str] = set()

    # Extract Python base class names (positional bases only, excluding keywords like table=True)
    base_names: list[str] = []
    if known_bases:
        for base in node.bases:
            base_name = (
                base.id if isinstance(base, ast.Name)
                else (base.attr if isinstance(base, ast.Attribute) else None)
            )
            if base_name and base_name in known_bases:
                base_names.append(base_name)
                for col in known_bases[base_name]:
                    if col.name not in inherited_names:
                        # Mark as inherited so the generator can skip emitting them
                        inherited_columns.append(col.model_copy(update={"inherited": True}))
                        inherited_names.add(col.name)
                        if col.foreign_key:
                            rel = _make_relation(table_name, col)
                            if rel is not None:
                                relations.append(rel)

    # Also capture non-base-class positional bases (e.g. SQLModel itself)
    all_base_names: list[str] = []
    for base in node.bases:
        base_name = (
            base.id if isinstance(base, ast.Name)
            else (base.attr if isinstance(base, ast.Attribute) else None)
        )
        if base_name:
            all_base_names.append(base_name)

    # ------------------------------------------------------------------
    # Parse local fields from the class body
    # ------------------------------------------------------------------
    local_columns: list[Column] = []

    for stmt in node.body:
        if not isinstance(stmt, ast.AnnAssign):
            continue
        if not isinstance(stmt.target, ast.Name):
            continue

        field_name = stmt.target.id
        annotation = stmt.annotation
        value = stmt.value  # could be Field(...), Relationship(...), or None

        # Skip relationship back-references
        if value is not None and _is_relationship_call(value):
            continue
        if _annotation_is_list(annotation):
            continue

        # Resolve type and nullability from the annotation
        try:
            alter_type, is_optional = _resolve_annotation(annotation, known_enums)
        except Exception:  # noqa: BLE001
            alter_type = "string"
            is_optional = False
            warns.append(
                f"  {table_name}.{field_name}: unresolvable type hint, defaulting to string"
            )

        # Skip relationship back-ref annotations (Optional["ModelName"])
        if alter_type == "_relationship":
            continue

        # Build the column from Field() kwargs
        col, extra_warns = _parse_field_call(
            field_name, alter_type, is_optional, value, known_enums
        )
        warns.extend(extra_warns)
        local_columns.append(col)

        # Generate a Relation if this column has a foreign_key
        if col.foreign_key:
            rel = _make_relation(table_name, col)
            if rel is not None:
                relations.append(rel)

    # ------------------------------------------------------------------
    # Merge: inherited first, local overrides by name
    # ------------------------------------------------------------------
    local_names = {c.name for c in local_columns}
    columns = [c for c in inherited_columns if c.name not in local_names]
    columns.extend(local_columns)

    table = Table(
        name=table_name,
        file_path=file_path,
        columns=columns,
        bases=all_base_names,
        schema_name=schema_name,
    )
    return table, relations, warns


def _is_relationship_call(value: ast.expr) -> bool:
    """Return True if the value is a ``Relationship(...)`` call."""
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if isinstance(func, ast.Name) and func.id == "Relationship":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "Relationship":
        return True
    return False


def _annotation_is_list(annotation: ast.expr) -> bool:
    """Return True if the annotation is a ``list[X]`` *relationship* collection.

    Returns False for ``list[str]``, ``List[Any]``, ``list[dict]``, etc. —
    those are JSON-array columns, not relationship back-references.  Only
    ``list[ModelClass]``, ``List[ModelClass]``, and ``list["ModelClass"]``
    (forward-ref strings) are treated as relationship collections.
    """
    if isinstance(annotation, ast.Subscript):
        val = annotation.value
        if isinstance(val, ast.Name) and val.id in ("list", "List"):
            # Only skip when the element type is a model class reference.
            return not _is_primitive_element(annotation.slice)
    return False


# Names that are clearly NOT SQLModel relationship target classes. When one of
# these appears as the element type of List[X] / list[X] the annotation means a
# JSON array column, not a relationship back-reference.
_LIST_PRIMITIVE_NAMES: frozenset[str] = frozenset({
    # builtins
    "str", "int", "float", "bool", "bytes",
    "dict", "list", "set", "tuple",
    "None", "NoneType", "type", "object",
    # typing helpers
    "Any", "Dict", "List", "Set", "Tuple",
    "Sequence", "Mapping", "Optional", "Union",
})


def _is_primitive_element(node: ast.expr) -> bool:
    """Return True if *node* is clearly a primitive/typing type, not a model class.

    Used to distinguish ``List[Any]`` / ``List[str]`` (→ json_array) from
    ``List[OrderItem]`` (→ relationship back-reference).

    * An ``ast.Name`` whose id is in ``_LIST_PRIMITIVE_NAMES`` → primitive.
    * An ``ast.Subscript`` (e.g. ``Dict[str, Any]``) → primitive (generic alias).
    * An ``ast.Tuple`` (e.g. multi-element type params) → primitive.
    * An ``ast.Constant`` string (forward-ref like ``"OrderItem"``) → NOT primitive
      (it's a model forward reference).
    * Anything else → NOT primitive (conservatively assumed to be a model class).
    """
    if isinstance(node, ast.Name):
        return node.id in _LIST_PRIMITIVE_NAMES
    if isinstance(node, (ast.Subscript, ast.Tuple)):
        return True  # generic alias like Dict[str, Any] or multi-param tuple
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return False  # forward-ref string like "OrderItem"
    return False


def _resolve_annotation(
    annotation: ast.expr, known_enums: dict[str, EnumDef]
) -> tuple[str, bool]:
    """Return (alter_type, is_nullable) for a type annotation node.

    Returns ``("_relationship", False)`` for back-reference types that should
    be skipped by the caller.
    """
    # Optional[X] → nullable, unwrap X
    if isinstance(annotation, ast.Subscript):
        val = annotation.value
        # Optional[X]
        if isinstance(val, ast.Name) and val.id in ("Optional",):
            inner = annotation.slice
            # Optional["ClassName"] → string constant → model ref, skip
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                return "_relationship", False
            inner_type, _ = _resolve_annotation(inner, known_enums)
            return inner_type, True

        # list[X] / List[X] — relationship collection OR json_array column.
        # Distinguish by the element type:
        #   List[Any], List[str], List[dict], List[Dict[...]], etc. → json_array
        #   List["OrderItem"], List[OrderItem] (model class)        → relationship
        if isinstance(val, ast.Name) and val.id in ("list", "List"):
            if _is_primitive_element(annotation.slice):
                return "json_array", False
            return "_relationship", False

        # Dict[K, V] → json (generic dict-like column)
        if isinstance(val, ast.Name) and val.id in ("dict", "Dict"):
            return "json", False

        # Fallthrough: try to resolve as-is
        type_str = _node_to_type_str(annotation)
        alter = _type_str_to_alter(type_str, known_enums)
        return alter, False

    # X | None (Python 3.10+ union syntax)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left = annotation.left
        right = annotation.right
        # Determine which side is None
        if isinstance(right, ast.Constant) and right.value is None:
            inner_type, _ = _resolve_annotation(left, known_enums)
            return inner_type, True
        if isinstance(left, ast.Constant) and left.value is None:
            inner_type, _ = _resolve_annotation(right, known_enums)
            return inner_type, True

    # Quoted forward ref as a Constant (e.g. "User" from Optional["User"])
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        name = annotation.value
        if name in known_enums:
            return name, False
        # It's a forward ref to a model class — relationship
        return "_relationship", False

    # Name or Attribute
    type_str = _node_to_type_str(annotation)
    alter = _type_str_to_alter(type_str, known_enums)
    return alter, False


def _type_str_to_alter(type_str: str, known_enums: dict[str, EnumDef]) -> str:
    """Convert a Python type string to an .alter type, with enum awareness."""
    # Check known enums first
    if type_str in known_enums:
        return type_str
    # Try the canonical type map
    try:
        return python_to_alter(type_str)
    except KeyError:
        return "string"  # fallback for unknown types


def _parse_field_call(
    field_name: str,
    alter_type: str,
    is_optional: bool,
    value: ast.expr | None,
    known_enums: dict[str, "EnumDef"] | None = None,
) -> tuple[Column, list[str]]:
    """Build a Column from the field name, resolved type, and Field() call node.

    Args:
        known_enums: Used for ``sa_column=Column(SQLEnum(...))`` type override —
            when the SA column expression names a known enum class, the alter
            type is updated to that enum name so the canvas shows the correct
            type instead of "string".
    """
    warns: list[str] = []
    nullable = is_optional
    primary_key = False
    unique = False
    index = False
    max_length: int | None = None
    foreign_key: str | None = None
    default: str | None = None
    extra_kwargs: dict[str, str] = {}

    if value is not None and isinstance(value, ast.Call):
        func = value.func
        func_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")

        if func_name == "Field":
            for kw in value.keywords:
                arg = kw.arg
                kw_val = kw.value

                if arg == "primary_key":
                    if _const_bool(kw_val) is True:
                        primary_key = True
                        nullable = False  # PKs are never null

                elif arg == "unique":
                    if _const_bool(kw_val) is True:
                        unique = True

                elif arg == "index":
                    if _const_bool(kw_val) is True:
                        index = True

                elif arg == "max_length":
                    v = _const_value(kw_val)
                    if isinstance(v, int):
                        max_length = v

                elif arg == "foreign_key":
                    v = _const_value(kw_val)
                    if isinstance(v, str):
                        foreign_key = v  # preserve verbatim — schema prefix must round-trip

                elif arg == "nullable":
                    v = _const_bool(kw_val)
                    if v is not None:
                        nullable = v

                elif arg == "default":
                    default, extra_nullable = _extract_default(kw_val)
                    if extra_nullable:
                        nullable = True

                elif arg == "default_factory":
                    default = _extract_default_factory(kw_val)

                elif arg in ("sa_column", "sa_type", "ge", "le", "gt", "lt",
                             "regex", "min_length"):
                    # Preserve as passthrough — re-emitted verbatim by the generator
                    try:
                        extra_kwargs[arg] = ast.unparse(kw_val)
                    except Exception:  # noqa: BLE001
                        pass

                elif arg in ("description", "title", "alias", "schema_extra"):
                    pass  # Metadata-only kwargs — not needed for round-trip

                else:
                    warns.append(
                        f"  Field({arg}=...) on '{field_name}' not mapped to schema"
                    )

    # ------------------------------------------------------------------
    # sa_column / sa_type type override (enum detection only)
    # ------------------------------------------------------------------
    sa_expr = extra_kwargs.get("sa_column") or extra_kwargs.get("sa_type", "")
    if sa_expr:
        import re as _re
        enum_match = _re.search(r'(?:SQLEnum|Enum)\((\w+)', sa_expr)
        if enum_match and known_enums and enum_match.group(1) in known_enums:
            alter_type = enum_match.group(1)

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
        extra_kwargs=extra_kwargs or None,
    )
    return col, warns


def _extract_default(node: ast.expr) -> tuple[str | None, bool]:
    """Extract a default value string and whether it implies nullable.

    Returns (default_str | None, is_nullable).
    """
    # None literal → nullable
    if isinstance(node, ast.Constant) and node.value is None:
        return None, True
    # String literal
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    # Bool/int/float literal
    if isinstance(node, ast.Constant):
        return str(node.value).lower(), False
    # Enum member: Role.member → "member"
    if isinstance(node, ast.Attribute):
        return node.attr, False
    # Name constant (shouldn't appear in Python 3.8+ but be safe)
    if isinstance(node, ast.Name) and node.id in ("True", "False", "None"):
        val = node.id
        if val == "None":
            return None, True
        return val.lower(), False
    # Dict literal: default={} or default={"key": "val"}
    if isinstance(node, ast.Dict):
        return "{}", False
    # List literal: default=[] or default=[1, 2]
    if isinstance(node, ast.List):
        return "[]", False
    return None, False


def _extract_default_factory(node: ast.expr) -> str | None:
    """Extract the default_factory callable name or expression.

    * ``uuid.uuid4`` → ``"uuid4"``
    * ``datetime.utcnow`` → ``"utcnow"``
    * ``list`` → ``"list"``
    * ``lambda: str(uuid.uuid4())`` → ``"expr:lambda: str(uuid.uuid4())"``
    """
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    # Lambda or other complex callable — preserve the expression verbatim
    try:
        return f"expr:{ast.unparse(node)}"
    except Exception:  # noqa: BLE001
        return None


def _const_value(node: ast.expr) -> Any:
    """Return the Python value of a Constant node, or None."""
    if isinstance(node, ast.Constant):
        return node.value
    return None
