"""Pydantic schema definitions for the .alter file format.

The .alter file is a JSON file that stores the database schema AND the canvas
layout (table positions). It is the single source of truth that both the
diagram and the generated ORM code sync against.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations / Literals
# ---------------------------------------------------------------------------

OrmType = Literal["sqlmodel", "sqlalchemy"]
DialectType = Literal["postgresql"]
RelationType = Literal["one-to-one", "one-to-many", "many-to-one", "many-to-many"]
OnDeleteType = Literal["CASCADE", "SET NULL", "RESTRICT", "NO ACTION", "SET DEFAULT"]


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class Position(BaseModel):
    """Absolute pixel position of a table card on the canvas.

    (0, 0) is the top-left corner of the canvas. Values are in pixels and used
    directly for DOM element placement (no transformation needed for zoom/pan).
    """

    x: int = 0
    y: int = 0


class Index(BaseModel):
    """A database index on one or more columns."""

    columns: list[str]
    unique: bool = False


class Column(BaseModel):
    """A single column definition within a table."""

    name: str
    type: str  # built-in alter type (e.g. "uuid", "string") or enum name (PascalCase)
    primary_key: bool = False
    nullable: bool = True
    default: Optional[str] = None  # e.g. "uuid4", "now", "false", or a literal value
    unique: bool = False
    max_length: Optional[int] = None
    foreign_key: Optional[str] = None  # "table.column" format
    index: bool = False
    inherited: bool = False  # True if this column comes from a Python base class
    extra_kwargs: Optional[dict[str, str]] = None  # Passthrough Field() kwargs (e.g. sa_column, regex)


class Table(BaseModel):
    """A database table definition including canvas layout position."""

    name: str
    file_path: str = "app/models.py"
    position: Position = Field(default_factory=Position)
    columns: list[Column] = Field(default_factory=list)
    indexes: list[Index] = Field(default_factory=list)
    bases: list[str] = Field(default_factory=list)  # Python base class names (e.g. ["UUIDBase"])


class Relation(BaseModel):
    """A foreign-key relation between two tables."""

    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    type: RelationType = "many-to-one"
    on_delete: OnDeleteType = "CASCADE"


class EnumMember(BaseModel):
    """A single enum member with its Python name and string value."""

    member_name: str  # Python identifier (e.g. "ENDUSER")
    value: str  # String value (e.g. "enduser")


class EnumDef(BaseModel):
    """A named enum definition shared across tables."""

    name: str
    values: list[str | EnumMember]
    file_path: Optional[str] = None  # relative path to the file defining this enum

    @field_validator("values", mode="before")
    @classmethod
    def normalise_values(cls, v: list) -> list:
        """Accept both plain strings (legacy) and EnumMember dicts."""
        result = []
        for item in v:
            if isinstance(item, str):
                # Legacy format: plain string → treat as (value, value)
                result.append(EnumMember(member_name=item, value=item))
            elif isinstance(item, dict):
                result.append(EnumMember(**item))
            elif isinstance(item, EnumMember):
                result.append(item)
            else:
                result.append(item)
        return result


class SchemaMetadata(BaseModel):
    """Metadata stored in the .alter file for tooling context."""

    sqlmodel_module: str = "app/models.py"
    alembic_dir: str = "alembic"
    database_url_env: str = "DATABASE_URL"
    created_at: Optional[datetime] = None
    last_synced: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Root model
# ---------------------------------------------------------------------------


class AlterSchema(BaseModel):
    """Root model for the .alter file format.

    This is the complete in-memory representation of a project's database schema
    plus canvas layout. Serialize to/from JSON with ``load`` and ``save``.
    """

    version: int = 1
    orm: OrmType = "sqlmodel"
    dialect: DialectType = "postgresql"
    tables: list[Table] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    enums: list[EnumDef] = Field(default_factory=list)
    metadata: SchemaMetadata = Field(default_factory=SchemaMetadata)

    @field_validator("version")
    @classmethod
    def check_version(cls, v: int) -> int:
        """Reject .alter files from unknown future versions."""
        if v != 1:
            raise ValueError(
                f"Unsupported .alter file version: {v}. "
                "Expected version 1. Please update Alter."
            )
        return v

    @model_validator(mode="after")
    def validate_enum_references(self) -> "AlterSchema":
        """Check that enum column types reference a defined enum."""
        from alter.types import TYPE_MAP  # avoid circular import at module level

        known_enums = {e.name for e in self.enums}
        for table in self.tables:
            for col in table.columns:
                if col.type not in TYPE_MAP and col.type not in known_enums:
                    raise ValueError(
                        f"Column '{table.name}.{col.name}' has unknown type '{col.type}'. "
                        f"It is not a built-in type and does not match any defined enum."
                    )
        return self

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "AlterSchema":
        """Load an AlterSchema from a .alter JSON file on disk.

        Raises:
            SchemaFileError: if the file cannot be read or parsed.
        """
        from alter.errors import SchemaFileError

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SchemaFileError(f"Cannot read .alter file at {path}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SchemaFileError(
                f"Invalid JSON in .alter file at {path}: {exc}"
            ) from exc

        try:
            return cls.model_validate(data)
        except Exception as exc:
            raise SchemaFileError(
                f"Schema validation failed for {path}: {exc}"
            ) from exc

    def save(self, path: Path) -> None:
        """Write the schema to a .alter JSON file (pretty-printed, git-diffable).

        Raises:
            SchemaFileError: if the file cannot be written.
        """
        from alter.errors import SchemaFileError

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                self.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise SchemaFileError(
                f"Cannot write .alter file to {path}: {exc}"
            ) from exc
