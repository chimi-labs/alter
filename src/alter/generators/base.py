"""Abstract base generator interface.

Both ORM backends (SQLModel, SQLAlchemy) implement this interface.
Use ``get_generator(orm)`` to get the right backend.
"""

from __future__ import annotations

import ast
import difflib
import keyword
from abc import ABC, abstractmethod
from pathlib import Path

from alter.schema import AlterSchema, EnumDef, Table
from alter.types import alter_to_python


# ---------------------------------------------------------------------------
# Shared pure-function helpers (no ORM dependency)
# ---------------------------------------------------------------------------


def _class_name(table_name: str) -> str:
    """``snake_case`` → ``PascalCase`` class name."""
    return "".join(w.capitalize() for w in table_name.split("_"))


def _safe_member_name(name: str) -> str:
    """Return a safe Python identifier for an enum member name.

    If *name* is a Python keyword (e.g. ``"import"``, ``"class"``), a
    trailing underscore is appended to avoid a ``SyntaxError``.
    """
    return f"{name}_" if keyword.iskeyword(name) else name


def generate_enum_class(enum: EnumDef) -> str:
    """Return source for a ``class X(str, Enum):`` definition.

    Used by both SQLModel and SQLAlchemy generators — the enum class syntax
    is identical for both ORMs.
    """
    from alter.schema import EnumMember  # avoid circular at module level
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


def _collect_stdlib_imports(
    schema: AlterSchema,
    enum_names: set[str],
    emit_enum_names: set[str] | None = None,
) -> list[str]:
    """Return stdlib import lines required by *schema*.

    Covers: ``uuid``, ``datetime``/``date``/``time``/``timezone``,
    ``from enum import Enum``, ``from decimal import Decimal``,
    ``from typing import Optional``.

    Both ORM generators share this identical preamble; each then appends its
    own ORM-specific import lines.

    Args:
        enum_names: All known enum names — used for type-hint resolution.
        emit_enum_names: If provided, only add ``from enum import Enum`` when
            this set is non-empty.  Defaults to *enum_names* when ``None``.
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
    return lines


# ---------------------------------------------------------------------------
# ParseResult + BaseGenerator
# ---------------------------------------------------------------------------


class BaseGenerator(ABC):
    """Abstract base class for ORM code generators.

    Subclasses implement ORM-specific code generation. Both backends share
    the same public interface so callers never need to know which ORM is active.
    """

    @abstractmethod
    def generate_models(
        self,
        schema: AlterSchema,
        local_enum_names: set[str] | None = None,
    ) -> str:
        """Generate a complete models file from *schema*.

        Returns a single Python source string suitable for writing to disk.
        Includes all imports, enum classes, and ORM model classes.

        Args:
            local_enum_names: If provided, only emit enum class definitions for
                names in this set.  Useful when sibling files define other enums
                that are imported rather than defined here.
        """
        ...

    @abstractmethod
    def update_models(
        self,
        schema: AlterSchema,
        existing_code: str,
        local_enum_names: set[str] | None = None,
    ) -> str:
        """Surgical update: modify only changed model classes in *existing_code*.

        Uses AST to locate class definitions by line number, replaces only the
        classes that differ from *schema*, and leaves everything else (comments,
        blank lines, helper functions, custom methods, imports) untouched.

        New classes are appended at the end of the file.

        Args:
            schema: The target schema to reflect in the file.
            existing_code: Current Python source code of the models file.
            local_enum_names: If provided, only emit/update enum class definitions
                for names in this set.  Enums imported from other files are not
                re-defined here even if they appear in *schema*.

        Returns:
            Updated Python source code.
        """
        ...

    @abstractmethod
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

        Returns:
            Ordered list of import statement strings (one ``import …`` or
            ``from … import …`` per entry).
        """
        ...

    # ------------------------------------------------------------------
    # Concrete helpers shared by all backends
    # ------------------------------------------------------------------

    def _collect_missing_imports(
        self,
        schema: AlterSchema,
        enum_names: set[str],
        tree: ast.Module,
        emit_enum_names: set[str] | None = None,
    ) -> list[str]:
        """Return import lines that are needed but not yet present in *tree*."""
        present = _imported_names(tree)
        needed = self._build_imports(schema, enum_names, emit_enum_names=emit_enum_names)
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
                        line_names.add(
                            alias.asname if alias.asname else alias.name.split(".")[0]
                        )
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        line_names.add(alias.asname if alias.asname else alias.name)
            if not line_names.issubset(present):
                missing.append(line)
        return missing

    def _insert_missing_imports(
        self,
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

        missing = self._collect_missing_imports(
            schema, enum_names, tree, emit_enum_names=emit_enum_names
        )
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

    def preview_apply(self, schema: AlterSchema, project_root: Path) -> str:
        """Dry run: return a unified diff of all files that WOULD change.

        Reads each file on disk, calls ``update_models()``, and diffs the
        result against the original. Returns all diffs concatenated.
        Does NOT write any files.
        """
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


def _default_model_path(schema: AlterSchema, project_root: Path | None = None) -> str:
    """Infer the default model file path for new tables with no ``file_path`` set.

    Priority:
    1. If existing tables have ``file_path`` set, pick the directory that
       appears most often and use ``<that_dir>/models.py``.
    2. If no existing table has a ``file_path`` and an ``app/`` directory
       exists inside *project_root*, fall back to ``"app/models.py"``.
    3. Final fallback: ``"models.py"`` at the project root — never creates a
       phantom directory that doesn't exist in the project.
    """
    dir_counts: dict[str, int] = {}
    for t in schema.tables:
        if t.file_path:
            d = str(Path(t.file_path).parent)
            dir_counts[d] = dir_counts.get(d, 0) + 1

    if dir_counts:
        best_dir = max(dir_counts, key=lambda k: dir_counts[k])
        return str(Path(best_dir) / "models.py")

    if project_root and (project_root / "app").is_dir():
        return "app/models.py"

    return "models.py"


def get_generator(orm: str) -> BaseGenerator:
    """Return the correct generator backend for the given ORM string.

    Args:
        orm: One of ``"sqlmodel"`` or ``"sqlalchemy"``.

    Raises:
        ValueError: if *orm* is not recognised.
    """
    if orm == "sqlmodel":
        from alter.generators.sqlmodel import SQLModelGenerator
        return SQLModelGenerator()
    if orm == "sqlalchemy":
        from alter.generators.sqlalchemy import SQLAlchemyGenerator
        return SQLAlchemyGenerator()
    raise ValueError(
        f"Unknown ORM '{orm}'. Expected 'sqlmodel' or 'sqlalchemy'."
    )
