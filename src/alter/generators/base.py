"""Abstract base generator interface.

Both ORM backends (SQLModel, SQLAlchemy) implement this interface.
Use ``get_generator(orm)`` to get the right backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from alter.schema import AlterSchema


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
    def preview_apply(self, schema: AlterSchema, project_root: Path) -> str:
        """Dry run: return a unified diff of all files that WOULD change.

        Reads each file on disk, calls ``update_models()``, and diffs the
        result against the original. Returns all diffs concatenated.
        Does NOT write any files.
        """
        ...


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
