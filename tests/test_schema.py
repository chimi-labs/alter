"""Tests for src/alter/schema.py — AlterSchema and related Pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from alter.errors import SchemaFileError
from alter.schema import (
    AlterSchema,
    Column,
    EnumDef,
    Index,
    Position,
    Relation,
    SchemaMetadata,
    Table,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_VALID_JSON = {
    "version": 1,
    "orm": "sqlmodel",
    "dialect": "postgresql",
    "tables": [],
    "relations": [],
    "enums": [],
    "metadata": {},
}

FULL_VALID_JSON = {
    "version": 1,
    "orm": "sqlmodel",
    "dialect": "postgresql",
    "tables": [
        {
            "name": "users",
            "file_path": "app/models.py",
            "position": {"x": 100, "y": 50},
            "columns": [
                {
                    "name": "id",
                    "type": "uuid",
                    "primary_key": True,
                    "nullable": False,
                    "default": "uuid4",
                },
                {
                    "name": "email",
                    "type": "string",
                    "nullable": False,
                    "unique": True,
                    "max_length": 255,
                },
                {
                    "name": "role",
                    "type": "Role",
                    "nullable": False,
                    "default": "member",
                },
            ],
            "indexes": [{"columns": ["email"], "unique": True}],
        },
        {
            "name": "posts",
            "file_path": "app/models.py",
            "position": {"x": 400, "y": 50},
            "columns": [
                {
                    "name": "id",
                    "type": "uuid",
                    "primary_key": True,
                    "nullable": False,
                    "default": "uuid4",
                },
                {
                    "name": "author_id",
                    "type": "uuid",
                    "nullable": False,
                    "foreign_key": "users.id",
                },
            ],
            "indexes": [],
        },
    ],
    "relations": [
        {
            "name": "user_posts",
            "from_table": "posts",
            "from_column": "author_id",
            "to_table": "users",
            "to_column": "id",
            "type": "many-to-one",
            "on_delete": "CASCADE",
        }
    ],
    "enums": [
        {"name": "Role", "values": ["admin", "member", "viewer"]}
    ],
    "metadata": {
        "sqlmodel_module": "app/models.py",
        "alembic_dir": "alembic",
        "database_url_env": "DATABASE_URL",
    },
}


# ---------------------------------------------------------------------------
# AlterSchema construction
# ---------------------------------------------------------------------------


def test_load_minimal_valid_json() -> None:
    schema = AlterSchema.model_validate(MINIMAL_VALID_JSON)
    assert schema.version == 1
    assert schema.orm == "sqlmodel"
    assert schema.dialect == "postgresql"
    assert schema.tables == []
    assert schema.relations == []
    assert schema.enums == []


def test_load_full_valid_json() -> None:
    schema = AlterSchema.model_validate(FULL_VALID_JSON)
    assert len(schema.tables) == 2
    assert schema.tables[0].name == "users"
    assert schema.tables[0].position.x == 100
    assert schema.tables[0].position.y == 50
    assert len(schema.tables[0].columns) == 3
    assert schema.tables[0].columns[0].type == "uuid"
    assert schema.tables[0].columns[0].primary_key is True
    assert len(schema.enums) == 1
    assert schema.enums[0].name == "Role"


def test_orm_defaults_to_sqlmodel() -> None:
    data = {k: v for k, v in MINIMAL_VALID_JSON.items() if k != "orm"}
    schema = AlterSchema.model_validate(data)
    assert schema.orm == "sqlmodel"


def test_invalid_orm_rejected() -> None:
    data = {**MINIMAL_VALID_JSON, "orm": "django"}
    with pytest.raises(ValidationError):
        AlterSchema.model_validate(data)


def test_unknown_version_rejected() -> None:
    data = {**MINIMAL_VALID_JSON, "version": 99}
    with pytest.raises(ValidationError):
        AlterSchema.model_validate(data)


def test_file_path_defaults_to_app_models() -> None:
    data = {
        **MINIMAL_VALID_JSON,
        "tables": [
            {
                "name": "items",
                "columns": [{"name": "id", "type": "uuid", "primary_key": True}],
            }
        ],
    }
    schema = AlterSchema.model_validate(data)
    assert schema.tables[0].file_path == "app/models.py"


def test_position_preserved_in_table() -> None:
    schema = AlterSchema.model_validate(FULL_VALID_JSON)
    pos = schema.tables[0].position
    assert pos.x == 100
    assert pos.y == 50


def test_enum_column_type_valid_with_defined_enum() -> None:
    # "Role" column type is valid because Role is in enums list
    schema = AlterSchema.model_validate(FULL_VALID_JSON)
    role_col = schema.tables[0].columns[2]
    assert role_col.type == "Role"


def test_enum_column_type_invalid_without_enum_def() -> None:
    data = {
        **MINIMAL_VALID_JSON,
        "tables": [
            {
                "name": "items",
                "columns": [
                    {"name": "id", "type": "uuid", "primary_key": True},
                    {"name": "status", "type": "UndefinedEnum"},
                ],
            }
        ],
        "enums": [],  # no enums defined
    }
    with pytest.raises((ValidationError, Exception)):
        AlterSchema.model_validate(data)


def test_sqlalchemy_orm_accepted() -> None:
    data = {**MINIMAL_VALID_JSON, "orm": "sqlalchemy"}
    schema = AlterSchema.model_validate(data)
    assert schema.orm == "sqlalchemy"


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    schema = AlterSchema.model_validate(FULL_VALID_JSON)
    alter_file = tmp_path / "project.alter"
    schema.save(alter_file)

    loaded = AlterSchema.load(alter_file)
    assert loaded.version == schema.version
    assert loaded.orm == schema.orm
    assert len(loaded.tables) == len(schema.tables)
    assert loaded.tables[0].name == schema.tables[0].name
    assert loaded.tables[0].position.x == schema.tables[0].position.x
    assert loaded.tables[0].position.y == schema.tables[0].position.y
    assert len(loaded.enums) == 1
    assert loaded.enums[0].name == "Role"


def test_save_produces_valid_json(tmp_path: Path) -> None:
    schema = AlterSchema.model_validate(FULL_VALID_JSON)
    alter_file = tmp_path / "project.alter"
    schema.save(alter_file)

    raw = alter_file.read_text()
    parsed = json.loads(raw)
    assert parsed["version"] == 1
    assert parsed["orm"] == "sqlmodel"


def test_save_is_pretty_printed(tmp_path: Path) -> None:
    schema = AlterSchema.model_validate(MINIMAL_VALID_JSON)
    alter_file = tmp_path / "project.alter"
    schema.save(alter_file)

    raw = alter_file.read_text()
    # Pretty-printed JSON has newlines
    assert "\n" in raw


def test_load_nonexistent_file_raises_schema_file_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.alter"
    with pytest.raises(SchemaFileError):
        AlterSchema.load(missing)


def test_load_invalid_json_raises_schema_file_error(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.alter"
    bad_file.write_text("not valid json {{")
    with pytest.raises(SchemaFileError):
        AlterSchema.load(bad_file)


def test_load_invalid_schema_raises_schema_file_error(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.alter"
    bad_file.write_text('{"version": 99}')
    with pytest.raises(SchemaFileError):
        AlterSchema.load(bad_file)


def test_minimal_schema_round_trip(tmp_path: Path) -> None:
    schema = AlterSchema()  # all defaults
    alter_file = tmp_path / "empty.alter"
    schema.save(alter_file)
    loaded = AlterSchema.load(alter_file)
    assert loaded.orm == "sqlmodel"
    assert loaded.tables == []


# ---------------------------------------------------------------------------
# Sub-model edge cases
# ---------------------------------------------------------------------------


def test_column_nullable_default_is_true() -> None:
    col = Column(name="x", type="string")
    assert col.nullable is True


def test_column_primary_key_default_is_false() -> None:
    col = Column(name="id", type="uuid")
    assert col.primary_key is False


def test_position_default_is_origin() -> None:
    pos = Position()
    assert pos.x == 0
    assert pos.y == 0


def test_index_unique_default_is_false() -> None:
    idx = Index(columns=["email"])
    assert idx.unique is False


def test_relation_type_valid_values() -> None:
    for rel_type in ("one-to-one", "one-to-many", "many-to-one", "many-to-many"):
        r = Relation(
            name="r",
            from_table="a",
            from_column="id",
            to_table="b",
            to_column="a_id",
            type=rel_type,  # type: ignore[arg-type]
        )
        assert r.type == rel_type


def test_enum_def_stores_values() -> None:
    from alter.schema import EnumMember
    e = EnumDef(name="Status", values=["active", "inactive"])
    assert len(e.values) == 2
    # Legacy plain strings are normalised to EnumMember
    assert isinstance(e.values[0], EnumMember)
    assert e.values[0].member_name == "active"
    assert e.values[0].value == "active"
