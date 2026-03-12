"""Tests for src/alter/mcp_server.py — all MCP tools via direct function calls.

Each test calls init_mcp() to reset the module-level singleton, then invokes
the tool functions directly (they are regular Python callables decorated with
@mcp.tool()).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import alter.mcp_server as ms
from alter.mcp_server import (
    add_column,
    add_file,
    add_relation,
    add_table,
    commit_changes,
    diff_markdown,
    discard_changes,
    export_schema,
    get_diff,
    modify_column,
    read_proposed,
    read_schema,
    redo,
    remove_entity,
    rename_entity,
    undo,
    validate,
)
from alter.schema import AlterSchema, Column, Table


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_staging(tmp_path: Path) -> Path:
    """Reset the MCP singleton to a fresh empty .alter file before each test."""
    alter_path = tmp_path / "test.alter"
    ms.init_mcp(alter_path)
    return alter_path


def _seed_table(name: str = "users") -> str:
    """Helper: add a table via add_table() and return its success message."""
    return add_table(name)


# ---------------------------------------------------------------------------
# read_schema
# ---------------------------------------------------------------------------


def test_read_schema_returns_dict_structure() -> None:
    result = read_schema()
    assert isinstance(result, dict)
    assert "orm" in result
    assert "tables" in result
    assert "relations" in result


def test_read_schema_empty_initially() -> None:
    result = read_schema()
    assert result["tables"] == []
    assert result["relations"] == []


def test_read_schema_orm_field() -> None:
    result = read_schema()
    assert result["orm"] in ("sqlmodel", "sqlalchemy")


# ---------------------------------------------------------------------------
# read_proposed
# ---------------------------------------------------------------------------


def test_read_proposed_same_as_current_when_no_pending() -> None:
    schema = read_schema()
    proposed = read_proposed()
    assert schema == proposed


def test_read_proposed_shows_pending_changes() -> None:
    add_table("users")
    proposed = read_proposed()
    assert any(t["name"] == "users" for t in proposed["tables"])


# ---------------------------------------------------------------------------
# add_table
# ---------------------------------------------------------------------------


def test_add_table_success() -> None:
    msg = add_table("orders")
    assert "orders" in msg
    assert "Error" not in msg


def test_add_table_seeds_id_column() -> None:
    add_table("products")
    proposed = read_proposed()
    tbl = next(t for t in proposed["tables"] if t["name"] == "products")
    assert any(c["name"] == "id" and c["primary_key"] for c in tbl["columns"])


def test_add_table_duplicate_returns_error() -> None:
    add_table("users")
    msg = add_table("users")
    assert msg.startswith("Error:")


def test_add_table_custom_file_path() -> None:
    add_table("invoices", file_path="app/billing/models.py")
    staging = ms._get_staging()
    proposed = staging.proposed_schema
    assert proposed is not None
    tbl = next(t for t in proposed.tables if t.name == "invoices")
    assert tbl.file_path == "app/billing/models.py"


# ---------------------------------------------------------------------------
# add_column
# ---------------------------------------------------------------------------


def test_add_column_success() -> None:
    add_table("users")
    msg = add_column("users", "email", "string")
    assert "email" in msg
    assert "Error" not in msg


def test_add_column_visible_in_proposed() -> None:
    add_table("users")
    add_column("users", "email", "string", nullable=False, unique=True)
    proposed = read_proposed()
    tbl = next(t for t in proposed["tables"] if t["name"] == "users")
    col = next(c for c in tbl["columns"] if c["name"] == "email")
    assert col["unique"] is True
    assert col["nullable"] is False


def test_add_column_table_not_found_returns_error() -> None:
    msg = add_column("nonexistent", "x", "string")
    assert msg.startswith("Error:")


def test_add_column_duplicate_returns_error() -> None:
    add_table("users")
    add_column("users", "email", "string")
    msg = add_column("users", "email", "string")
    assert msg.startswith("Error:")


def test_add_column_fk_nonexistent_table_returns_error() -> None:
    add_table("users")
    msg = add_column("users", "org_id", "uuid", foreign_key="nonexistent_table.id")
    assert msg.startswith("Error:")
    assert "nonexistent_table" in msg


def test_add_column_fk_nonexistent_column_returns_error() -> None:
    add_table("users")
    add_table("orgs")
    # orgs has only the seeded 'id' column — 'name' does not exist
    msg = add_column("users", "org_id", "uuid", foreign_key="orgs.name")
    assert msg.startswith("Error:")
    assert "orgs.name" in msg


def test_add_column_fk_nonexistent_target_leaves_no_partial_column() -> None:
    """A failed FK validation must not leave the column in the schema."""
    add_table("users")
    add_column("users", "org_id", "uuid", foreign_key="ghost.id")
    proposed = read_proposed()
    tbl = next(t for t in proposed["tables"] if t["name"] == "users")
    col_names = [c["name"] for c in tbl["columns"]]
    assert "org_id" not in col_names


def test_add_column_fk_valid_creates_column_and_relation() -> None:
    add_table("users")
    add_table("orgs")
    # orgs has a seeded uuid 'id' column
    msg = add_column("users", "org_id", "uuid", foreign_key="orgs.id")
    assert "Error" not in msg
    proposed = read_proposed()
    # Column exists
    tbl = next(t for t in proposed["tables"] if t["name"] == "users")
    col_names = [c["name"] for c in tbl["columns"]]
    assert "org_id" in col_names
    # Relation exists (serialised as "from": "table.col", "to": "table.col")
    assert any(
        r.get("from") == "users.org_id" and r.get("to") == "orgs.id"
        for r in proposed["relations"]
    )


def test_add_column_fk_invalid_format_returns_error() -> None:
    add_table("users")
    msg = add_column("users", "x", "uuid", foreign_key="no_dot_here")
    assert msg.startswith("Error:")


# ---------------------------------------------------------------------------
# modify_column
# ---------------------------------------------------------------------------


def test_modify_column_changes_type() -> None:
    add_table("products")
    add_column("products", "price", "int")
    modify_column("products", "price", new_type="decimal")
    proposed = read_proposed()
    tbl = next(t for t in proposed["tables"] if t["name"] == "products")
    col = next(c for c in tbl["columns"] if c["name"] == "price")
    assert col["type"] == "decimal"


def test_modify_column_changes_nullable() -> None:
    add_table("users")
    add_column("users", "bio", "text", nullable=True)
    modify_column("users", "bio", nullable=False)
    proposed = read_proposed()
    tbl = next(t for t in proposed["tables"] if t["name"] == "users")
    col = next(c for c in tbl["columns"] if c["name"] == "bio")
    assert col["nullable"] is False


def test_modify_column_table_not_found_returns_error() -> None:
    msg = modify_column("ghost", "col", new_type="string")
    assert msg.startswith("Error:")


# ---------------------------------------------------------------------------
# add_relation
# ---------------------------------------------------------------------------


def test_add_relation_success() -> None:
    add_table("users")
    add_table("posts")
    msg = add_relation("posts", "author_id", "users", "id")
    assert "Error" not in msg


def test_add_relation_visible_in_proposed() -> None:
    add_table("users")
    add_table("posts")
    add_relation("posts", "author_id", "users", "id", on_delete="CASCADE")
    proposed = read_proposed()
    assert len(proposed["relations"]) == 1
    rel = proposed["relations"][0]
    assert rel["from"] == "posts.author_id"
    assert rel["to"] == "users.id"


def test_add_relation_from_table_not_found_returns_error() -> None:
    add_table("users")
    msg = add_relation("missing_table", "col", "users", "id")
    assert msg.startswith("Error:")


def test_add_relation_to_table_not_found_returns_error() -> None:
    add_table("posts")
    msg = add_relation("posts", "author_id", "missing_users", "id")
    assert msg.startswith("Error:")


# ---------------------------------------------------------------------------
# remove_entity
# ---------------------------------------------------------------------------


def test_remove_entity_drops_table() -> None:
    add_table("users")
    remove_entity("users")
    proposed = read_proposed()
    assert not any(t["name"] == "users" for t in proposed["tables"])


def test_remove_entity_drops_table_and_its_relations() -> None:
    add_table("users")
    add_table("posts")
    add_relation("posts", "author_id", "users", "id")
    remove_entity("users")
    proposed = read_proposed()
    assert len(proposed["relations"]) == 0


def test_remove_entity_drops_column() -> None:
    add_table("users")
    add_column("users", "bio", "text")
    remove_entity("users", column="bio")
    proposed = read_proposed()
    tbl = next(t for t in proposed["tables"] if t["name"] == "users")
    assert not any(c["name"] == "bio" for c in tbl["columns"])


def test_remove_entity_table_not_found_returns_error() -> None:
    msg = remove_entity("ghost_table")
    assert msg.startswith("Error:")


# ---------------------------------------------------------------------------
# rename_entity
# ---------------------------------------------------------------------------


def test_rename_entity_renames_table() -> None:
    add_table("users")
    rename_entity("users", "members")
    proposed = read_proposed()
    names = {t["name"] for t in proposed["tables"]}
    assert "members" in names
    assert "users" not in names


def test_rename_entity_updates_relation_references() -> None:
    add_table("users")
    add_table("posts")
    add_relation("posts", "author_id", "users", "id")
    rename_entity("users", "members")
    proposed = read_proposed()
    rel = proposed["relations"][0]
    assert rel["to"].startswith("members.")


def test_rename_entity_renames_column() -> None:
    add_table("users")
    add_column("users", "email_address", "string")
    rename_entity("users", "email", column="email_address")
    proposed = read_proposed()
    tbl = next(t for t in proposed["tables"] if t["name"] == "users")
    col_names = {c["name"] for c in tbl["columns"]}
    assert "email" in col_names
    assert "email_address" not in col_names


def test_rename_entity_table_not_found_returns_error() -> None:
    msg = rename_entity("ghost", "specter")
    assert msg.startswith("Error:")


# ---------------------------------------------------------------------------
# get_diff
# ---------------------------------------------------------------------------


def test_get_diff_empty_when_no_pending() -> None:
    result = get_diff()
    assert result == []


def test_get_diff_detects_add_table() -> None:
    add_table("users")
    changes = get_diff()
    assert any(c["type"] == "add_table" and c["table"] == "users" for c in changes)


def test_get_diff_detects_add_column() -> None:
    # Commit the table first so it exists in current_schema
    add_table("products")
    commit_changes()
    # Now propose adding a column to the existing committed table
    add_column("products", "price", "decimal")
    changes = get_diff()
    assert any(c["type"] == "add_column" and c["column"] == "price" for c in changes)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_returns_list() -> None:
    result = validate()
    assert isinstance(result, list)


def test_validate_no_issues_for_empty_schema() -> None:
    result = validate()
    errors = [i for i in result if i["severity"] == "error"]
    assert errors == []


# ---------------------------------------------------------------------------
# commit_changes
# ---------------------------------------------------------------------------


def test_commit_changes_writes_to_disk(fresh_staging: Path) -> None:
    add_table("users")
    commit_changes()
    loaded = AlterSchema.load(fresh_staging)
    assert any(t.name == "users" for t in loaded.tables)


def test_commit_changes_returns_count_message() -> None:
    add_table("users")
    msg = commit_changes()
    assert "1 change" in msg or "changes" in msg


def test_commit_changes_nothing_when_no_pending() -> None:
    msg = commit_changes()
    assert "Nothing" in msg


# ---------------------------------------------------------------------------
# discard_changes
# ---------------------------------------------------------------------------


def test_discard_changes_clears_pending() -> None:
    add_table("users")
    discard_changes()
    proposed = read_proposed()
    assert not any(t["name"] == "users" for t in proposed["tables"])


def test_discard_changes_nothing_when_no_pending() -> None:
    msg = discard_changes()
    assert "Nothing" in msg


# ---------------------------------------------------------------------------
# undo / redo
# ---------------------------------------------------------------------------


def test_undo_reverts_last_change() -> None:
    add_table("users")
    add_table("posts")
    undo()
    proposed = read_proposed()
    names = {t["name"] for t in proposed["tables"]}
    assert "users" in names
    assert "posts" not in names


def test_undo_on_empty_stack_returns_nothing() -> None:
    msg = undo()
    assert "Nothing" in msg


def test_redo_reapplies_undone_change() -> None:
    add_table("users")
    undo()
    redo()
    proposed = read_proposed()
    assert any(t["name"] == "users" for t in proposed["tables"])


# ---------------------------------------------------------------------------
# export_schema
# ---------------------------------------------------------------------------


def test_export_schema_sql_contains_create_table(fresh_staging: Path) -> None:
    # Commit a table first (export reads current schema)
    add_table("users")
    commit_changes()
    result = export_schema(format="sql")
    assert "CREATE TABLE" in result.upper()


def test_export_schema_mermaid_contains_erdiagram(fresh_staging: Path) -> None:
    add_table("products")
    commit_changes()
    result = export_schema(format="mermaid")
    assert "erDiagram" in result


def test_export_schema_alter_is_valid_json(fresh_staging: Path) -> None:
    add_table("items")
    commit_changes()
    result = export_schema(format="alter")
    parsed = json.loads(result)
    assert "tables" in parsed


# ---------------------------------------------------------------------------
# diff_markdown
# ---------------------------------------------------------------------------


def test_diff_markdown_no_pending_returns_no_changes_message() -> None:
    result = diff_markdown()
    assert "No pending changes" in result


def test_diff_markdown_with_pending_returns_markdown() -> None:
    add_table("users")
    result = diff_markdown()
    assert "## Schema Changes" in result
    assert "users" in result


# ---------------------------------------------------------------------------
# Full staging flow — the main integration test
# ---------------------------------------------------------------------------


def test_full_staging_flow(fresh_staging: Path) -> None:
    """Propose → undo → redo → commit → verify disk write.

    The table is committed first so that add_column shows as a discrete diff
    entry (not subsumed into the parent add_table entry).
    """
    # Step 1: add table and commit it to current_schema
    add_table("users")
    commit_changes()
    assert get_diff() == []  # clean slate

    # Step 2: propose adding a column
    add_column("users", "email", "string")
    diff = get_diff()
    assert len(diff) == 1
    assert diff[0]["type"] == "add_column"
    assert diff[0]["column"] == "email"

    # Step 3: undo the column
    undo()
    assert get_diff() == []  # back to committed state
    assert not ms._get_staging().has_pending()

    # Step 4: redo the column
    redo()
    diff = get_diff()
    assert len(diff) == 1
    assert diff[0]["column"] == "email"

    # Step 5: commit
    msg = commit_changes()
    assert "change" in msg

    # Step 6: verify disk
    loaded = AlterSchema.load(fresh_staging)
    assert any(t.name == "users" for t in loaded.tables)
    users = next(t for t in loaded.tables if t.name == "users")
    assert any(c.name == "email" for c in users.columns)

    # Step 7: no pending after commit
    assert get_diff() == []
    assert undo() == "Nothing to undo."


# ---------------------------------------------------------------------------
# add_file
# ---------------------------------------------------------------------------

_SQLMODEL_ORDERS = """\
from sqlmodel import SQLModel, Field

class Orders(SQLModel, table=True):
    __tablename__ = "orders"
    id: int = Field(primary_key=True)
    user_id: int
"""


def test_add_file_success(fresh_staging: Path) -> None:
    """add_file parses a model file and proposes its tables."""
    project_root = fresh_staging.parent
    model_file = project_root / "legacy.py"
    model_file.write_text(_SQLMODEL_ORDERS)

    result = add_file("legacy.py")

    assert "Added" in result
    assert "orders" in result
    # Proposed schema should contain the new table
    proposed = read_proposed()
    names = {t["name"] for t in proposed["tables"]}
    assert "orders" in names


def test_add_file_duplicate_returns_error(fresh_staging: Path) -> None:
    """add_file returns an error message when all tables already exist."""
    project_root = fresh_staging.parent
    model_file = project_root / "legacy.py"
    model_file.write_text(_SQLMODEL_ORDERS)

    # Add the file once (proposes 'orders')
    add_file("legacy.py")
    commit_changes()

    # Try to add the same file again — should fail gracefully
    result = add_file("legacy.py")
    assert "Error" in result
    assert "already exist" in result
