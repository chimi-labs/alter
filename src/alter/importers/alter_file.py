"""Alter file importer — loads .alter JSON into AlterSchema.

This is the simplest importer.  It loads a ``.alter`` JSON file from another
project and returns it as an ``AlterSchema`` object, preserving all table
positions from the source file.  Useful for using an existing schema as a
template or merging schemas across projects.
"""

from __future__ import annotations

from pathlib import Path

from alter.schema import AlterSchema


def import_alter_file(path: Path) -> AlterSchema:
    """Load a ``.alter`` JSON file and return an ``AlterSchema``.

    Preserves all table positions from the source file.  Validation is
    performed by ``AlterSchema.load`` — a ``SchemaFileError`` is raised
    if the file is missing, malformed, or contains schema errors.

    Args:
        path: Absolute or relative path to the ``.alter`` JSON file.

    Returns:
        An ``AlterSchema`` with tables, columns, relations, and enums from
        the file, including their canvas positions.
    """
    return AlterSchema.load(path)
