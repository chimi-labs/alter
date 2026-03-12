"""Alter file exporter — serializes AlterSchema to .alter JSON."""

from pathlib import Path

from alter.schema import AlterSchema


def export_alter_file(schema: AlterSchema, path: Path) -> Path:
    """Write *schema* as a .alter JSON file to *path* and return the path."""
    schema.save(path)
    return path
