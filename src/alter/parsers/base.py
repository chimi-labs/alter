"""Abstract base parser interface.

Both ORM backends (SQLModel, SQLAlchemy) implement this interface.
Use ``get_parser(orm)`` to get the right backend.
"""

from __future__ import annotations

import ast
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alter.schema import AlterSchema, Column, EnumDef, Relation, Table


# ---------------------------------------------------------------------------
# Directory scanning utilities
# ---------------------------------------------------------------------------

# Directories that should never be scanned for ORM models or enums.
_EXCLUDED_DIRS: frozenset[str] = frozenset({
    ".venv", "venv", ".env",
    "site-packages", "__pycache__",
    ".git", ".hg", ".svn",
    "node_modules",
    "dist", "build", "egg-info",
    ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
})


def iter_py_files(directory: Path) -> list[Path]:
    """Return all ``.py`` files under *directory*, skipping non-project dirs.

    Uses :func:`os.walk` with in-place directory filtering so that excluded
    subtrees (virtual envs, caches, build artefacts) are never descended into.

    **Must return a list** (not a generator): callers such as
    ``parse_directory`` iterate the result twice — once to collect enum/base
    definitions and again to parse ORM files.  If this function ever changes
    to use ``yield``, the second iteration would silently produce nothing.
    """
    result: list[Path] = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED_DIRS)
        for f in sorted(files):
            if f.endswith(".py"):
                result.append(Path(root) / f)
    return result


# ---------------------------------------------------------------------------
# Import resolution utilities (shared by all parser backends)
# ---------------------------------------------------------------------------


@dataclass
class ImportInfo:
    """A single ``from X import Y`` statement."""

    module: str       # dotted module path (empty string for bare relative imports)
    names: list[str]  # imported names; ["*"] for wildcard
    level: int        # number of leading dots (0 = absolute, 1 = same package, …)


def extract_imports(tree: ast.Module) -> list[ImportInfo]:
    """Return all ``from … import …`` statements found in *tree*."""
    result: list[ImportInfo] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            result.append(ImportInfo(
                module=node.module or "",
                names=[alias.name for alias in node.names],
                level=node.level,
            ))
    return result


def resolve_module_to_path(
    module: str,
    search_roots: list[Path],
    current_file: Path,
    level: int = 0,
) -> Path | None:
    """Resolve a Python module name to a ``.py`` file path.

    Args:
        module: Dotted module path (e.g. ``"app.enums"``).  May be an empty
            string for bare relative imports (``from . import foo``).
        search_roots: Directories to treat as Python package roots for
            absolute imports.
        current_file: The file that contains the import statement.  Used for
            relative import resolution.
        level: Number of leading dots in the import (0 = absolute).

    Returns:
        The resolved :class:`~pathlib.Path`, or ``None`` if the module cannot
        be located (e.g. a third-party package).
    """
    if level > 0:
        # Relative import — start from the package directory
        base = current_file.parent
        for _ in range(level - 1):
            base = base.parent

        if module:
            parts = module.split(".")
            candidate = base
            for part in parts:
                candidate = candidate / part
        else:
            candidate = base

        py = candidate.with_suffix(".py")
        if py.exists():
            return py
        init_py = candidate / "__init__.py"
        if init_py.exists():
            return init_py
        return None

    # Absolute import
    parts = module.split(".")
    for root in search_roots:
        candidate = root
        for part in parts:
            candidate = candidate / part
        py = candidate.with_suffix(".py")
        if py.exists():
            return py
        init_py = candidate / "__init__.py"
        if init_py.exists():
            return init_py
    return None


# ---------------------------------------------------------------------------
# Shared internal file-result dataclass
# ---------------------------------------------------------------------------


@dataclass
class _FileResult:
    """One-file parse result: tables, enums, relations, warnings."""

    tables: list[Table] = field(default_factory=list)
    enums: list[EnumDef] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared AST helpers — used by both SQLModel and SQLAlchemy parsers
# ---------------------------------------------------------------------------


def _node_to_name(node: ast.expr) -> str:
    """Return the simple name of a Name or Attribute node, or '' otherwise."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _node_to_type_str(node: ast.expr) -> str:
    """Convert an AST annotation node to a dotted type string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_node_to_type_str(node.value)}.{node.attr}"
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Subscript):
        return f"{_node_to_type_str(node.value)}[{_node_to_type_str(node.slice)}]"
    return "unknown"


def _const_bool(node: ast.expr) -> bool | None:
    """Return the value of a boolean Constant node, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _is_enum_class(node: ast.ClassDef) -> bool:
    """Return True if the class inherits from any Enum variant.

    Handles:
    * ``class X(Enum)`` / ``class X(str, Enum)`` — ``ast.Name`` base ``"Enum"``
    * ``class X(IntEnum)`` / ``class X(StrEnum)``
    * ``class X(enum.Enum)`` / ``class X(enum.IntEnum)`` — ``ast.Attribute`` base
    """
    _ENUM_NAMES = frozenset({"Enum", "IntEnum", "StrEnum"})
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in _ENUM_NAMES:
            return True
        if isinstance(base, ast.Attribute) and base.attr in _ENUM_NAMES:
            return True
    return False


def _parse_enum_class(node: ast.ClassDef, file_path: str | None = None) -> EnumDef:
    """Extract enum name, member names, and string values from an AST ClassDef.

    Members whose values are non-string constants (e.g. ``int``) fall back to
    using the member name as the value.
    """
    from alter.schema import EnumMember  # avoid circular at module level
    values: list[EnumMember] = []
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    member_name = target.id
                    if isinstance(stmt.value, ast.Constant) and isinstance(
                        stmt.value.value, str
                    ):
                        values.append(
                            EnumMember(member_name=member_name, value=stmt.value.value)
                        )
                    else:
                        # Use the member name as both name and value
                        values.append(
                            EnumMember(member_name=member_name, value=member_name)
                        )
    return EnumDef(name=node.name, values=values, file_path=file_path)


def _get_table_schema(node: ast.ClassDef) -> str | None:
    """Return the schema name from ``__table_args__`` or ``None``.

    Handles both forms of ``__table_args__``:

    * **Plain dict** — ``__table_args__ = {"schema": "myschema"}``
    * **Tuple** — ``__table_args__ = (Index(...), {"schema": "myschema"})``

    SQLAlchemy/SQLModel convention: when the tuple form is used, the *last*
    element of the tuple must be a plain dict of table-level kwargs (including
    ``"schema"``).  We scan from the end and use the first dict found.
    """
    for stmt in node.body:
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if not (isinstance(target, ast.Name) and target.id == "__table_args__"):
                continue
            value = stmt.value

            # Resolve the options dict: either the value itself (plain dict)
            # or the last ast.Dict element inside a tuple.
            if isinstance(value, ast.Dict):
                options_dict: ast.Dict | None = value
            elif isinstance(value, ast.Tuple):
                options_dict = None
                for elt in reversed(value.elts):
                    if isinstance(elt, ast.Dict):
                        options_dict = elt
                        break
            else:
                continue

            if options_dict is None:
                continue

            for key, val in zip(options_dict.keys, options_dict.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "schema"
                    and isinstance(val, ast.Constant)
                    and isinstance(val.value, str)
                ):
                    return val.value
    return None


def _make_relation(table_name: str, col: Column) -> Relation | None:
    """Build a Relation from a foreign_key column.

    ``col.foreign_key`` is stored verbatim (e.g. ``"table.col"`` or
    ``"schema.table.col"``).  We split on the *last* dot to get the column
    name, then strip any schema prefix from the table part so that the
    canvas-facing ``Relation.to_table`` holds the unqualified table name.
    """
    if not col.foreign_key:
        return None
    parts = col.foreign_key.rsplit(".", 1)
    if len(parts) != 2:
        return None
    to_table_raw, to_column = parts
    # Strip leading schema qualifier ("myschema.users" → "users")
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


# ---------------------------------------------------------------------------
# ParseResult + BaseParser
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    """Full result from parsing one or more ORM model files.

    Returned by ``BaseParser.parse_directory()``. Callers can inspect
    ``warnings`` to surface issues to the user without crashing.
    """

    schema: AlterSchema
    warnings: list[str] = field(default_factory=list)
    skipped_files: list[Path] = field(default_factory=list)


class BaseParser(ABC):
    """Abstract base class for ORM model file parsers.

    Subclasses implement ORM-specific AST analysis. Both backends share the
    same public interface so callers never need to know which ORM is active.

    Args:
        project_root: If provided, ``Table.file_path`` values are recorded
            relative to this directory. Otherwise the absolute path is used.
    """

    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = project_root

    @abstractmethod
    def detect_orm(self, path: Path) -> bool:
        """Return True if the given file uses this parser's ORM."""
        ...

    @abstractmethod
    def parse_file(self, path: Path) -> list[Table]:
        """Parse a single Python file and return its Table definitions.

        Raises:
            ParseError: on unrecoverable parse failure.
        """
        ...

    @abstractmethod
    def parse_file_result(self, path: Path) -> ParseResult:
        """Parse a single Python file and return the full result (tables + enums).

        Use this instead of ``parse_file`` when enum definitions must be
        preserved — e.g. when syncing a known file or registering a new file.

        Raises:
            ParseError: on unrecoverable parse failure.
        """
        ...

    @abstractmethod
    def parse_directory(self, directory: Path) -> ParseResult:
        """Recursively parse all Python files in *directory*.

        Files with syntax errors are skipped and recorded in
        ``ParseResult.skipped_files``. Non-fatal issues appear in
        ``ParseResult.warnings``.
        """
        ...

    # ------------------------------------------------------------------
    # Helpers shared by all backends
    # ------------------------------------------------------------------

    def _relative_path(self, path: Path) -> str:
        """Return a string file path, relative to project_root if set."""
        if self.project_root is not None:
            try:
                return str(path.relative_to(self.project_root))
            except ValueError:
                pass
        return str(path)

    def _search_roots(self, path: Path) -> list[Path]:
        """Return candidate package root directories for resolving imports.

        When *project_root* is set, it is the sole search root. Otherwise a
        heuristic walks up from the file until it leaves a Python package
        (no ``__init__.py``), giving both that root and the file's directory.
        """
        if self.project_root is not None:
            return [self.project_root]
        curr = path.parent
        while (curr / "__init__.py").exists() and curr != curr.parent:
            curr = curr.parent
        return [curr, path.parent]

    def _collect_import_deps(
        self,
        path: Path,
        search_roots: list[Path],
    ) -> list[tuple[Path, str, ast.Module]]:
        """BFS traversal of the import graph starting from *path*.

        Returns a list of ``(dep_path, dep_fp, dep_tree)`` tuples for every
        dependency reachable from *path* (excluding *path* itself).  Circular
        imports are safe — each file is visited at most once.

        Used by both ``_resolve_imports`` implementations to avoid duplicating
        the traversal loop.
        """
        visited: set[Path] = {path.resolve()}
        deps: list[tuple[Path, str, ast.Module]] = []
        queue: list[Path] = []

        # Seed queue with direct imports of *path*
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            for imp in extract_imports(tree):
                dep = resolve_module_to_path(imp.module, search_roots, path, imp.level)
                if dep is not None and dep.resolve() not in visited:
                    queue.append(dep)
        except Exception:  # noqa: BLE001
            return []

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
                # Follow transitive imports
                for imp in extract_imports(dep_tree):
                    tdep = resolve_module_to_path(
                        imp.module, search_roots, dep_path, imp.level
                    )
                    if tdep is not None and tdep.resolve() not in visited:
                        queue.append(tdep)
            except Exception:  # noqa: BLE001
                pass

        return deps

    def _phase1_collect_enums(
        self, py_files: list[Path]
    ) -> tuple[dict[str, EnumDef], dict[Path, ast.Module]]:
        """Phase 1: scan *all* ``.py`` files to collect enum definitions.

        Returns ``(global_enums, parsed_trees)`` where *global_enums* maps
        class name → EnumDef and *parsed_trees* maps file path → AST (for
        backends that need a second sub-pass, e.g. SQLModel base-class
        collection).

        Files that cannot be parsed are silently skipped — syntax errors in
        enum-only files do not abort the directory parse.
        """
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
                pass  # syntax errors handled later in Phase 2

        return global_enums, parsed_trees


def deduplicate_tables(
    tables: list[Table],
    warnings: list[str],
) -> list[Table]:
    """Return *tables* with duplicates removed (first definition wins).

    When two classes in the same file share the same ``__tablename__``, the
    parser correctly picks up both. This helper keeps the first occurrence and
    records a warning for each subsequent duplicate, so ``alter init`` and
    ``alter sync`` always produce a clean schema even when a file has been
    partially corrupted (e.g. by a previous ``alter apply`` run on a project
    with non-conventional class names).
    """
    seen: dict[str, int] = {}  # tablename → first index
    result: list[Table] = []
    for table in tables:
        if table.name not in seen:
            seen[table.name] = len(result)
            result.append(table)
        else:
            warnings.append(
                f"Duplicate table '{table.name}' (class '{table.name}' appears more than once "
                f"with the same __tablename__). Keeping the first definition."
            )
    return result


def get_parser(orm: str, project_root: Path | None = None) -> BaseParser:
    """Return the correct parser backend for the given ORM string.

    Args:
        orm: One of ``"sqlmodel"`` or ``"sqlalchemy"``.
        project_root: Passed to the parser constructor.

    Raises:
        ValueError: if *orm* is not recognised.
    """
    if orm == "sqlmodel":
        from alter.parsers.sqlmodel import SQLModelParser
        return SQLModelParser(project_root=project_root)
    if orm == "sqlalchemy":
        from alter.parsers.sqlalchemy import SQLAlchemyParser
        return SQLAlchemyParser(project_root=project_root)
    raise ValueError(
        f"Unknown ORM '{orm}'. Expected 'sqlmodel' or 'sqlalchemy'."
    )
