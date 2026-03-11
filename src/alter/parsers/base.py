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

from alter.schema import AlterSchema, Table


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
