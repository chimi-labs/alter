"""Parser package — ORM auto-detection and parser factory.

Public API::

    from alter.parsers import detect_project_orm, get_parser
    from alter.parsers.base import ParseResult
"""

from __future__ import annotations

from pathlib import Path

from alter.errors import ParseError
from alter.parsers.base import BaseParser, ParseResult, get_parser, iter_py_files


def _has_sqlmodel_table_definitions(directory: Path) -> bool:
    """Return True if any .py file in *directory* contains a SQLModel table class.

    SQLModel marks ORM table classes with the ``table=True`` keyword argument:
    ``class MyModel(SQLModel, table=True): …``

    This marker is unique to SQLModel and is not used by plain SQLAlchemy, so
    it reliably distinguishes "SQLModel table" from "SQLAlchemy import only".
    """
    for py_file in iter_py_files(directory):
        try:
            source = py_file.read_text(encoding="utf-8")
            if "table=True" in source or "table = True" in source:
                return True
        except OSError:
            continue
    return False


def _has_sqlalchemy_table_definitions(directory: Path) -> bool:
    """Return True if any .py file in *directory* contains a SQLAlchemy-native table.

    A SQLAlchemy-native table class sets ``__tablename__`` directly (without
    using SQLModel's ``table=True`` marker).  Files that also import from
    sqlmodel are assumed to be SQLModel files, not pure SQLAlchemy.
    """
    for py_file in iter_py_files(directory):
        try:
            source = py_file.read_text(encoding="utf-8")
            if (
                "__tablename__" in source
                and "table=True" not in source
                and "table = True" not in source
                and "from sqlmodel import" not in source
                and "import sqlmodel" not in source.lower()
            ):
                return True
        except OSError:
            continue
    return False


def detect_project_orm(directory: Path) -> str:
    """Scan Python files in *directory* and return the detected ORM.

    Rules:
    1. If only SQLModel imports are found → ``"sqlmodel"``
    2. If only SQLAlchemy imports are found → ``"sqlalchemy"``
    3. If **both** are found (SQLModel projects often import SQLAlchemy directly
       for event listeners, custom column types, etc.):

       a. Check for actual table class definitions — ``table=True`` (SQLModel)
          vs ``__tablename__`` without SQLModel imports (SQLAlchemy).
       b. If only SQLModel table definitions exist → ``"sqlmodel"``
       c. If only SQLAlchemy table definitions exist → ``"sqlalchemy"``
       d. If **both** have table definitions → raises ``ParseError`` (genuine
          conflict: two ORM frameworks each defining real models).
       e. If neither has table definitions → ``"sqlmodel"`` (default).

    4. If **neither** ORM is imported → defaults to ``"sqlmodel"``

    Args:
        directory: Project directory to scan (searched recursively).

    Returns:
        ``"sqlmodel"`` or ``"sqlalchemy"``.

    Raises:
        ParseError: if both ORM frameworks have actual model table definitions
            in the same project.
    """
    from alter.parsers.sqlalchemy import SQLAlchemyParser
    from alter.parsers.sqlmodel import SQLModelParser

    sqlmodel_parser = SQLModelParser()
    sqlalchemy_parser = SQLAlchemyParser()

    has_sqlmodel = False
    has_sqlalchemy = False

    for py_file in iter_py_files(directory):
        if not has_sqlmodel and sqlmodel_parser.detect_orm(py_file):
            has_sqlmodel = True
        if not has_sqlalchemy and sqlalchemy_parser.detect_orm(py_file):
            has_sqlalchemy = True
        if has_sqlmodel and has_sqlalchemy:
            break

    if has_sqlmodel and has_sqlalchemy:
        # SQLModel projects often import directly from sqlalchemy (for event
        # listeners, advanced column types, etc.).  Only treat this as a
        # genuine conflict if BOTH frameworks define actual ORM table classes.
        has_sqlmodel_tables = _has_sqlmodel_table_definitions(directory)
        has_sqlalchemy_tables = _has_sqlalchemy_table_definitions(directory)

        if has_sqlmodel_tables and has_sqlalchemy_tables:
            raise ParseError(
                "Both SQLModel and SQLAlchemy table definitions detected in the "
                "same project. Alter supports one ORM per project. "
                "Set the 'orm' field explicitly in your .alter file."
            )

        if has_sqlalchemy_tables:
            return "sqlalchemy"

        # SQLModel tables found (or neither, which still defaults to sqlmodel).
        return "sqlmodel"

    if has_sqlalchemy:
        return "sqlalchemy"

    # Default to sqlmodel (even if neither detected)
    return "sqlmodel"


__all__ = [
    "detect_project_orm",
    "get_parser",
    "BaseParser",
    "ParseResult",
]
