"""Tests for schema validation (alter.validate)."""

from __future__ import annotations

import pytest

from alter.schema import AlterSchema, Column, Relation, Table
from alter.validate import ValidationIssue, _parse_fk_reference, validate_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_table(name: str = "users") -> Table:
    return Table(
        name=name,
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="email", type="string"),
        ],
    )


def _col(**kw) -> Column:
    defaults = dict(name="col", type="string")
    defaults.update(kw)
    return Column(**defaults)


def _issues(schema: AlterSchema, severity: str | None = None) -> list[ValidationIssue]:
    issues = validate_schema(schema)
    if severity:
        return [i for i in issues if i.severity == severity]
    return issues


# ---------------------------------------------------------------------------
# Valid schema — no errors
# ---------------------------------------------------------------------------


def test_valid_schema_no_errors():
    schema = AlterSchema(tables=[_valid_table()])
    errors = _issues(schema, "error")
    assert errors == []


def test_valid_schema_with_fk_no_errors():
    users = _valid_table("users")
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="users.id", index=True),
        ],
    )
    schema = AlterSchema(tables=[users, posts])
    errors = _issues(schema, "error")
    assert errors == []


# ---------------------------------------------------------------------------
# Primary key checks
# ---------------------------------------------------------------------------


def test_missing_pk_produces_warning():
    table = Table(name="logs", columns=[Column(name="message", type="text")])
    schema = AlterSchema(tables=[table])
    warnings = _issues(schema, "warning")
    assert any("primary key" in i.message.lower() for i in warnings)


def test_table_with_pk_no_pk_warning():
    schema = AlterSchema(tables=[_valid_table()])
    warnings = _issues(schema, "warning")
    pk_warnings = [w for w in warnings if "primary key" in w.message.lower()]
    assert pk_warnings == []


# ---------------------------------------------------------------------------
# Duplicate column names
# ---------------------------------------------------------------------------


def test_duplicate_column_name_is_error():
    # Duplicate column names are now caught at Table construction time by the
    # _check_unique_columns model validator, so a ValidationError is raised
    # before validate_schema() is ever called.
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Duplicate column names"):
        Table(
            name="users",
            columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
                Column(name="id", type="string"),  # duplicate!
            ],
        )


# ---------------------------------------------------------------------------
# Dangling foreign key references
# ---------------------------------------------------------------------------


def test_dangling_fk_unknown_table_is_error():
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="nonexistent_table.id"),
        ],
    )
    schema = AlterSchema(tables=[posts])
    errors = _issues(schema, "error")
    assert any("unknown table" in i.message.lower() for i in errors)


def test_dangling_fk_unknown_column_is_error():
    users = _valid_table("users")
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="users.no_such_col"),
        ],
    )
    schema = AlterSchema(tables=[users, posts])
    errors = _issues(schema, "error")
    assert any("unknown column" in i.message.lower() for i in errors)


def test_valid_fk_no_dangling_error():
    users = _valid_table("users")
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="users.id", index=True),
        ],
    )
    schema = AlterSchema(tables=[users, posts])
    errors = _issues(schema, "error")
    assert not any("unknown" in i.message.lower() for i in errors)


def test_invalid_fk_format_is_error():
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="bad_format_no_dot"),
        ],
    )
    schema = AlterSchema(tables=[posts])
    errors = _issues(schema, "error")
    assert any("format" in i.message.lower() for i in errors)


# ---------------------------------------------------------------------------
# FK index suggestions
# ---------------------------------------------------------------------------


def test_fk_without_index_suggests_info():
    users = _valid_table("users")
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="users.id", index=False),
        ],
    )
    schema = AlterSchema(tables=[users, posts])
    infos = _issues(schema, "info")
    assert any("index" in i.message.lower() for i in infos)


def test_fk_with_index_no_info_suggestion():
    users = _valid_table("users")
    posts = Table(
        name="posts",
        columns=[
            Column(name="id", type="uuid", primary_key=True, nullable=False),
            Column(name="user_id", type="uuid", foreign_key="users.id", index=True),
        ],
    )
    schema = AlterSchema(tables=[users, posts])
    infos = _issues(schema, "info")
    fk_index_infos = [i for i in infos if "FK" in i.message or "index" in i.message.lower()]
    # Should have no index suggestion for this column
    col_infos = [i for i in fk_index_infos if i.column == "user_id"]
    assert col_infos == []


# ---------------------------------------------------------------------------
# Empty names
# ---------------------------------------------------------------------------


def test_empty_table_name_is_error():
    table = Table(
        name="",
        columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
    )
    schema = AlterSchema(tables=[table])
    errors = _issues(schema, "error")
    assert any("empty" in i.message.lower() or "name" in i.message.lower() for i in errors)


# ---------------------------------------------------------------------------
# Relation validation
# ---------------------------------------------------------------------------


def test_relation_unknown_from_table_is_error():
    users = _valid_table("users")
    rel = Relation(
        name="bad_rel",
        from_table="nonexistent",
        from_column="user_id",
        to_table="users",
        to_column="id",
    )
    schema = AlterSchema(tables=[users], relations=[rel])
    errors = _issues(schema, "error")
    assert any("from_table" in i.message.lower() or "nonexistent" in i.message for i in errors)


def test_relation_unknown_to_table_is_error():
    users = _valid_table("users")
    rel = Relation(
        name="bad_rel",
        from_table="users",
        from_column="id",
        to_table="nonexistent",
        to_column="id",
    )
    schema = AlterSchema(tables=[users], relations=[rel])
    errors = _issues(schema, "error")
    assert any("to_table" in i.message.lower() or "nonexistent" in i.message for i in errors)


def test_relation_unknown_from_column_is_error():
    """Relation.from_column not in from_table.columns → error."""
    users = _valid_table("users")
    orders = _valid_table("orders")
    rel = Relation(
        name="r",
        from_table="orders",
        from_column="ghost_col",     # does not exist in orders
        to_table="users",
        to_column="id",
    )
    schema = AlterSchema(tables=[users, orders], relations=[rel])
    errors = _issues(schema, "error")
    assert any("ghost_col" in i.message for i in errors), (
        "Expected an error for dangling from_column 'ghost_col'"
    )


def test_relation_unknown_to_column_is_error():
    """Relation.to_column not in to_table.columns → error."""
    users = _valid_table("users")
    orders = _valid_table("orders")
    rel = Relation(
        name="r",
        from_table="orders",
        from_column="id",
        to_table="users",
        to_column="ghost_col",       # does not exist in users
    )
    schema = AlterSchema(tables=[users, orders], relations=[rel])
    errors = _issues(schema, "error")
    assert any("ghost_col" in i.message for i in errors), (
        "Expected an error for dangling to_column 'ghost_col'"
    )


def test_relation_valid_columns_no_error():
    """A Relation whose columns all exist must produce no column-level errors."""
    users = _valid_table("users")
    orders = _valid_table("orders")
    rel = Relation(
        name="r",
        from_table="orders",
        from_column="id",
        to_table="users",
        to_column="id",
    )
    schema = AlterSchema(tables=[users, orders], relations=[rel])
    col_errors = [
        i for i in _issues(schema, "error")
        if "column" in i.message.lower() and "relation" in i.message.lower()
    ]
    assert col_errors == [], f"Unexpected column errors for valid relation: {col_errors}"


def test_relation_unknown_from_table_does_not_also_produce_column_error():
    """When from_table is unknown, no spurious from_column error should appear."""
    users = _valid_table("users")
    rel = Relation(
        name="r",
        from_table="nonexistent",
        from_column="some_col",
        to_table="users",
        to_column="id",
    )
    schema = AlterSchema(tables=[users], relations=[rel])
    errors = _issues(schema, "error")
    # Only one error: the missing from_table
    from_table_errors = [e for e in errors if "from_table" in e.message.lower() or "nonexistent" in e.message]
    col_errors = [e for e in errors if "some_col" in e.message]
    assert from_table_errors, "Expected from_table error"
    assert not col_errors, "Must not emit a column error when from_table is already unknown"


# ---------------------------------------------------------------------------
# Schema-qualified foreign keys  (fix: validator must accept 'schema.table.col')
# ---------------------------------------------------------------------------


class TestParseFkReference:
    """Unit tests for the _parse_fk_reference helper."""

    def test_two_part_unqualified(self):
        schema, table, col = _parse_fk_reference("users.id")
        assert schema is None
        assert table == "users"
        assert col == "id"

    def test_three_part_schema_qualified(self):
        schema, table, col = _parse_fk_reference("myschema.orders.id")
        assert schema == "myschema"
        assert table == "orders"
        assert col == "id"

    def test_one_part_returns_empty_strings(self):
        schema, table, col = _parse_fk_reference("no_dot")
        assert table == ""
        assert col == ""

    def test_four_part_returns_empty_strings(self):
        schema, table, col = _parse_fk_reference("a.b.c.d")
        assert table == ""
        assert col == ""


class TestSchemaQualifiedForeignKeys:
    """Validator must not reject schema-qualified FK strings."""

    def _schema_qualified_fk_schema(self) -> AlterSchema:
        orders = Table(
            name="orders",
            schema_name="myschema",
            columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
            ],
        )
        order_items = Table(
            name="order_items",
            schema_name="myschema",
            columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
                Column(
                    name="order_id",
                    type="uuid",
                    foreign_key="myschema.orders.id",
                    index=True,
                ),
            ],
        )
        return AlterSchema(tables=[orders, order_items])

    def test_schema_qualified_fk_no_format_error(self):
        """'schema.table.column' must not produce a format error."""
        errors = _issues(self._schema_qualified_fk_schema(), "error")
        format_errors = [e for e in errors if "format" in e.message.lower()]
        assert format_errors == []

    def test_schema_qualified_fk_resolved_table_found(self):
        """Referenced table is found by bare name despite schema prefix in FK string."""
        errors = _issues(self._schema_qualified_fk_schema(), "error")
        unknown_errors = [e for e in errors if "unknown table" in e.message.lower()]
        assert unknown_errors == []

    def test_schema_qualified_fk_resolved_column_found(self):
        """Referenced column is found correctly when FK is schema-qualified."""
        errors = _issues(self._schema_qualified_fk_schema(), "error")
        col_errors = [e for e in errors if "unknown column" in e.message.lower()]
        assert col_errors == []

    def test_schema_qualified_fk_zero_errors(self):
        """A valid schema-qualified FK produces zero validation errors."""
        errors = _issues(self._schema_qualified_fk_schema(), "error")
        assert errors == []

    def test_schema_qualified_fk_dangling_table_still_errors(self):
        """A schema-qualified FK to a nonexistent table still raises an error."""
        table = Table(
            name="items",
            columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
                Column(
                    name="order_id",
                    type="uuid",
                    foreign_key="myschema.nonexistent_table.id",
                ),
            ],
        )
        errors = _issues(AlterSchema(tables=[table]), "error")
        assert any("unknown table" in e.message.lower() for e in errors)

    def test_unqualified_fk_still_valid(self):
        """Existing plain 'table.column' FKs must still pass without errors."""
        users = _valid_table("users")
        posts = Table(
            name="posts",
            columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
                Column(name="user_id", type="uuid", foreign_key="users.id", index=True),
            ],
        )
        errors = _issues(AlterSchema(tables=[users, posts]), "error")
        assert errors == []

    def test_error_message_mentions_both_formats(self):
        """Format-error message must describe both valid forms."""
        table = Table(
            name="items",
            columns=[
                Column(name="id", type="uuid", primary_key=True, nullable=False),
                Column(name="x", type="string", foreign_key="no_dot_at_all"),
            ],
        )
        errors = _issues(AlterSchema(tables=[table]), "error")
        format_errors = [e for e in errors if "format" in e.message.lower()]
        assert format_errors
        assert "schema.table.column" in format_errors[0].message

# ---------------------------------------------------------------------------
# Identifier validation — table names
# ---------------------------------------------------------------------------


class TestTableNameIdentifiers:
    """validate_schema should flag invalid table names."""

    def _schema_with_table(self, name: str) -> AlterSchema:
        return AlterSchema(tables=[
            Table(
                name=name,
                columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
            )
        ])

    # --- valid names (no identifier errors) ---

    def test_simple_lowercase_name_is_valid(self) -> None:
        errors = _issues(self._schema_with_table("users"), "error")
        ident_errors = [e for e in errors if "valid sql identifier" in e.message.lower()]
        assert ident_errors == []

    def test_name_with_underscore_prefix_is_valid(self) -> None:
        errors = _issues(self._schema_with_table("_internal"), "error")
        ident_errors = [e for e in errors if "valid sql identifier" in e.message.lower()]
        assert ident_errors == []

    def test_mixed_case_name_is_valid(self) -> None:
        errors = _issues(self._schema_with_table("UserRole"), "error")
        ident_errors = [e for e in errors if "valid sql identifier" in e.message.lower()]
        assert ident_errors == []

    def test_name_with_digits_in_middle_is_valid(self) -> None:
        errors = _issues(self._schema_with_table("user2role"), "error")
        ident_errors = [e for e in errors if "valid sql identifier" in e.message.lower()]
        assert ident_errors == []

    # --- invalid names (must produce an error) ---

    def test_name_starting_with_digit_is_error(self) -> None:
        errors = _issues(self._schema_with_table("123users"), "error")
        assert any("valid sql identifier" in e.message.lower() for e in errors)

    def test_name_with_hyphen_is_error(self) -> None:
        errors = _issues(self._schema_with_table("user-profile"), "error")
        assert any("valid sql identifier" in e.message.lower() for e in errors)

    def test_name_with_space_is_error(self) -> None:
        errors = _issues(self._schema_with_table("my table"), "error")
        assert any("valid sql identifier" in e.message.lower() for e in errors)

    def test_name_with_dot_is_error(self) -> None:
        errors = _issues(self._schema_with_table("my.table"), "error")
        assert any("valid sql identifier" in e.message.lower() for e in errors)

    # --- SQL reserved words (must produce a warning, not an error) ---

    def test_reserved_word_select_is_warning(self) -> None:
        all_issues = _issues(self._schema_with_table("select"))
        warnings = [i for i in all_issues if i.severity == "warning" and "reserved" in i.message.lower()]
        assert warnings, "Expected a reserved-word warning for table 'select'"

    def test_reserved_word_select_no_identifier_error(self) -> None:
        errors = _issues(self._schema_with_table("select"), "error")
        ident_errors = [e for e in errors if "valid sql identifier" in e.message.lower()]
        assert ident_errors == []

    def test_reserved_word_table_is_warning(self) -> None:
        all_issues = _issues(self._schema_with_table("table"))
        warnings = [i for i in all_issues if i.severity == "warning" and "reserved" in i.message.lower()]
        assert warnings

    def test_reserved_word_case_insensitive(self) -> None:
        """Both 'SELECT' and 'Select' should trigger the reserved-word warning."""
        for name in ("SELECT", "Select"):
            all_issues = _issues(self._schema_with_table(name))
            warnings = [i for i in all_issues if i.severity == "warning" and "reserved" in i.message.lower()]
            assert warnings, f"Expected reserved-word warning for table '{name}'"


# ---------------------------------------------------------------------------
# Identifier validation — column names
# ---------------------------------------------------------------------------


class TestColumnNameIdentifiers:
    """validate_schema should flag invalid column names."""

    def _schema_with_col(self, col_name: str) -> AlterSchema:
        return AlterSchema(tables=[
            Table(
                name="users",
                columns=[
                    Column(name="id", type="uuid", primary_key=True, nullable=False),
                    Column(name=col_name, type="string"),
                ],
            )
        ])

    # --- valid names ---

    def test_simple_column_name_is_valid(self) -> None:
        errors = _issues(self._schema_with_col("email"), "error")
        ident_errors = [e for e in errors if "valid sql identifier" in e.message.lower()]
        assert ident_errors == []

    def test_underscore_prefix_column_is_valid(self) -> None:
        errors = _issues(self._schema_with_col("_meta"), "error")
        ident_errors = [e for e in errors if "valid sql identifier" in e.message.lower()]
        assert ident_errors == []

    def test_column_with_trailing_digit_is_valid(self) -> None:
        errors = _issues(self._schema_with_col("address2"), "error")
        ident_errors = [e for e in errors if "valid sql identifier" in e.message.lower()]
        assert ident_errors == []

    # --- invalid names ---

    def test_column_starting_with_digit_is_error(self) -> None:
        errors = _issues(self._schema_with_col("1name"), "error")
        assert any("valid sql identifier" in e.message.lower() for e in errors)

    def test_column_with_hyphen_is_error(self) -> None:
        errors = _issues(self._schema_with_col("first-name"), "error")
        assert any("valid sql identifier" in e.message.lower() for e in errors)

    def test_column_with_space_is_error(self) -> None:
        errors = _issues(self._schema_with_col("my column"), "error")
        assert any("valid sql identifier" in e.message.lower() for e in errors)

    def test_column_with_at_sign_is_error(self) -> None:
        errors = _issues(self._schema_with_col("email@domain"), "error")
        assert any("valid sql identifier" in e.message.lower() for e in errors)

    # --- SQL reserved words ---

    def test_column_named_from_is_warning(self) -> None:
        all_issues = _issues(self._schema_with_col("from"))
        warnings = [i for i in all_issues if i.severity == "warning" and "reserved" in i.message.lower()]
        assert warnings

    def test_column_named_select_is_warning_not_error(self) -> None:
        all_issues = _issues(self._schema_with_col("select"))
        warnings = [i for i in all_issues if i.severity == "warning" and "reserved" in i.message.lower()]
        errors = [i for i in all_issues if i.severity == "error" and "valid sql identifier" in i.message.lower()]
        assert warnings
        assert not errors

    def test_column_reserved_word_case_insensitive(self) -> None:
        for name in ("FROM", "From", "from"):
            all_issues = _issues(self._schema_with_col(name))
            warnings = [i for i in all_issues if i.severity == "warning" and "reserved" in i.message.lower()]
            assert warnings, f"Expected reserved-word warning for column '{name}'"
