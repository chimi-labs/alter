"""Regression tests — unreferenced enums from non-table files collected into schema.

ISSUE: alter init collected every Enum subclass found in the scanned directory,
including enums from DTO files, Pydantic-only models, and utility scripts. These
cluttered schema.alter and appeared in SQL/Mermaid exports.

Fix: parse_directory now post-filters schema.enums so only enums actually
referenced by at least one column type in a parsed SQLModel table are kept.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from alter.parsers.sqlmodel import SQLModelParser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write *files* (name → source) into *tmp_path* and return it."""
    for name, src in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# File fixtures
# ---------------------------------------------------------------------------

MODELS_PY = """\
from enum import Enum
from typing import Optional
from sqlmodel import SQLModel, Field

class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"

class UserSQL(SQLModel, table=True):
    __tablename__ = "user"
    id: int = Field(primary_key=True)
    role: UserRole
    name: str
"""

DTO_PY = """\
from enum import Enum
from pydantic import BaseModel

class SearchType(str, Enum):
    EXACT = "exact"
    FUZZY = "fuzzy"

class UserSearchDTO(BaseModel):
    query: str
    search_type: SearchType
"""

PYDANTIC_CONFIG_PY = """\
from enum import Enum
from pydantic import BaseModel

class ConfigType(str, Enum):
    FEATURE = "feature"
    EXPERIMENT = "experiment"

class FeatureFlagGroups(str, Enum):
    BETA = "beta"
    INTERNAL = "internal"

class FeatureConfig(BaseModel):
    config_type: ConfigType
    groups: FeatureFlagGroups
"""

UTILS_PY = """\
from enum import Enum

class Operation(str, Enum):
    CREATE = "create"
    DELETE = "delete"

def run_operation(op: Operation) -> None:
    pass
"""

SECOND_MODELS_PY = """\
from enum import Enum
from typing import Optional
from sqlmodel import SQLModel, Field

class ItemStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"

class ItemSQL(SQLModel, table=True):
    __tablename__ = "item"
    id: int = Field(primary_key=True)
    status: ItemStatus
"""

ORPHAN_ENUM_IN_MODEL_FILE_PY = """\
from enum import Enum
from sqlmodel import SQLModel, Field

class OrphanEnum(str, Enum):
    A = "a"
    B = "b"

class ProductSQL(SQLModel, table=True):
    __tablename__ = "product"
    id: int = Field(primary_key=True)
    name: str
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUnreferencedEnumsFiltered:
    def test_referenced_enum_kept(self, tmp_path: Path):
        """UserRole is used by UserSQL.role → must appear in schema.enums."""
        _make_dir(tmp_path, {"models.py": MODELS_PY})
        result = SQLModelParser().parse_directory(tmp_path)
        enum_names = {e.name for e in result.schema.enums}
        assert "UserRole" in enum_names

    def test_dto_enum_removed(self, tmp_path: Path):
        """SearchType lives only in a DTO file → must NOT appear in schema.enums."""
        _make_dir(tmp_path, {
            "models.py": MODELS_PY,
            "dtos.py": DTO_PY,
        })
        result = SQLModelParser().parse_directory(tmp_path)
        enum_names = {e.name for e in result.schema.enums}
        assert "SearchType" not in enum_names

    def test_pydantic_config_enums_removed(self, tmp_path: Path):
        """ConfigType and FeatureFlagGroups are Pydantic-only → not in schema."""
        _make_dir(tmp_path, {
            "models.py": MODELS_PY,
            "config.py": PYDANTIC_CONFIG_PY,
        })
        result = SQLModelParser().parse_directory(tmp_path)
        enum_names = {e.name for e in result.schema.enums}
        assert "ConfigType" not in enum_names
        assert "FeatureFlagGroups" not in enum_names

    def test_utility_enum_removed(self, tmp_path: Path):
        """Operation is a utility script enum → not in schema."""
        _make_dir(tmp_path, {
            "models.py": MODELS_PY,
            "utils.py": UTILS_PY,
        })
        result = SQLModelParser().parse_directory(tmp_path)
        enum_names = {e.name for e in result.schema.enums}
        assert "Operation" not in enum_names

    def test_multiple_files_only_referenced_kept(self, tmp_path: Path):
        """Multi-file project: only UserRole and ItemStatus survive the filter."""
        _make_dir(tmp_path, {
            "models/users.py": MODELS_PY,
            "models/items.py": SECOND_MODELS_PY,
            "dtos/search.py": DTO_PY,
            "config/flags.py": PYDANTIC_CONFIG_PY,
            "scripts/utils.py": UTILS_PY,
        })
        result = SQLModelParser().parse_directory(tmp_path)
        enum_names = {e.name for e in result.schema.enums}

        assert "UserRole" in enum_names
        assert "ItemStatus" in enum_names
        assert "SearchType" not in enum_names
        assert "ConfigType" not in enum_names
        assert "FeatureFlagGroups" not in enum_names
        assert "Operation" not in enum_names

    def test_tables_still_parsed(self, tmp_path: Path):
        """Filtering enums must not affect table parsing."""
        _make_dir(tmp_path, {
            "models.py": MODELS_PY,
            "dtos.py": DTO_PY,
        })
        result = SQLModelParser().parse_directory(tmp_path)
        table_names = {t.name for t in result.schema.tables}
        assert "user" in table_names

    def test_orphan_enum_in_table_file_also_filtered(self, tmp_path: Path):
        """An enum defined in the same file as tables but not used → filtered."""
        _make_dir(tmp_path, {"models.py": ORPHAN_ENUM_IN_MODEL_FILE_PY})
        result = SQLModelParser().parse_directory(tmp_path)
        enum_names = {e.name for e in result.schema.enums}
        assert "OrphanEnum" not in enum_names

    def test_no_tables_means_no_enums(self, tmp_path: Path):
        """A directory with only DTO/utility files → zero enums in schema."""
        _make_dir(tmp_path, {
            "dtos.py": DTO_PY,
            "utils.py": UTILS_PY,
        })
        result = SQLModelParser().parse_directory(tmp_path)
        assert result.schema.enums == []

    def test_enum_count_exact(self, tmp_path: Path):
        """Exactly one enum (UserRole) should be kept for the single-table project."""
        _make_dir(tmp_path, {
            "models.py": MODELS_PY,
            "dtos.py": DTO_PY,
            "config.py": PYDANTIC_CONFIG_PY,
        })
        result = SQLModelParser().parse_directory(tmp_path)
        assert len(result.schema.enums) == 1
        assert result.schema.enums[0].name == "UserRole"
