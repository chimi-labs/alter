"""Parser package — ORM auto-detection and parser factory.

Public API::

    from alter.parsers import detect_project_orm, get_parser
    from alter.parsers.base import ParseResult
"""

from __future__ import annotations

from pathlib import Path

from alter.errors import ParseError
from alter.parsers.base import BaseParser, ParseResult, get_parser, iter_py_files


def detect_project_orm(directory: Path) -> str:
    """Scan Python files in *directory* and return the detected ORM.

    Rules:
    - If SQLModel imports are found (and no SQLAlchemy) → ``"sqlmodel"``
    - If SQLAlchemy imports are found (and no SQLModel) → ``"sqlalchemy"``
    - If **both** are found → raises ``ParseError`` (one project, one ORM)
    - If **neither** is found → defaults to ``"sqlmodel"``

    Args:
        directory: Project directory to scan (searched recursively).

    Returns:
        ``"sqlmodel"`` or ``"sqlalchemy"``.

    Raises:
        ParseError: if both ORM frameworks are detected in the same project.
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
        raise ParseError(
            "Both SQLModel and SQLAlchemy imports detected in the same project. "
            "Alter supports one ORM per project. "
            "Set the 'orm' field explicitly in your .alter file."
        )

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
