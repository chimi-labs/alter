"""Tests for src/alter/types.py — the canonical type mapping module."""

from __future__ import annotations

import pytest

from alter.types import (
    TYPE_MAP,
    alter_to_python,
    alter_to_sql,
    is_enum_type,
    python_to_alter,
    sql_to_alter,
)


# ---------------------------------------------------------------------------
# alter_to_python
# ---------------------------------------------------------------------------


def test_alter_to_python_uuid() -> None:
    assert alter_to_python("uuid") == "uuid.UUID"


def test_alter_to_python_string() -> None:
    assert alter_to_python("string") == "str"


def test_alter_to_python_int() -> None:
    assert alter_to_python("int") == "int"


def test_alter_to_python_bigint() -> None:
    assert alter_to_python("bigint") == "int"


def test_alter_to_python_bool() -> None:
    assert alter_to_python("bool") == "bool"


def test_alter_to_python_datetime() -> None:
    assert alter_to_python("datetime") == "datetime"


def test_alter_to_python_float() -> None:
    assert alter_to_python("float") == "float"


def test_alter_to_python_decimal() -> None:
    assert alter_to_python("decimal") == "Decimal"


def test_alter_to_python_json() -> None:
    assert alter_to_python("json") == "dict"


def test_alter_to_python_text() -> None:
    assert alter_to_python("text") == "str"


def test_alter_to_python_date() -> None:
    assert alter_to_python("date") == "date"


def test_alter_to_python_time() -> None:
    assert alter_to_python("time") == "time"


def test_alter_to_python_bytes() -> None:
    assert alter_to_python("bytes") == "bytes"


def test_alter_to_python_enum_passthrough() -> None:
    # PascalCase enum names pass through unchanged
    assert alter_to_python("Role") == "Role"
    assert alter_to_python("SubscriptionStatus") == "SubscriptionStatus"


def test_alter_to_python_unknown_lowercase_raises() -> None:
    with pytest.raises(KeyError):
        alter_to_python("unknowntype")


# ---------------------------------------------------------------------------
# alter_to_sql
# ---------------------------------------------------------------------------


def test_alter_to_sql_uuid() -> None:
    assert alter_to_sql("uuid") == "UUID"


def test_alter_to_sql_string_no_max_length() -> None:
    assert alter_to_sql("string") == "TEXT"


def test_alter_to_sql_string_with_max_length() -> None:
    assert alter_to_sql("string", 255) == "VARCHAR(255)"


def test_alter_to_sql_string_with_max_length_100() -> None:
    assert alter_to_sql("string", 100) == "VARCHAR(100)"


def test_alter_to_sql_int() -> None:
    assert alter_to_sql("int") == "INTEGER"


def test_alter_to_sql_bigint() -> None:
    assert alter_to_sql("bigint") == "BIGINT"


def test_alter_to_sql_bool() -> None:
    assert alter_to_sql("bool") == "BOOLEAN"


def test_alter_to_sql_datetime() -> None:
    assert alter_to_sql("datetime") == "TIMESTAMPTZ"


def test_alter_to_sql_float() -> None:
    assert alter_to_sql("float") == "DOUBLE PRECISION"


def test_alter_to_sql_decimal() -> None:
    assert alter_to_sql("decimal") == "NUMERIC"


def test_alter_to_sql_json() -> None:
    assert alter_to_sql("json") == "JSONB"


def test_alter_to_sql_text() -> None:
    assert alter_to_sql("text") == "TEXT"


def test_alter_to_sql_date() -> None:
    assert alter_to_sql("date") == "DATE"


def test_alter_to_sql_time() -> None:
    assert alter_to_sql("time") == "TIME"


def test_alter_to_sql_bytes() -> None:
    assert alter_to_sql("bytes") == "BYTEA"


def test_alter_to_sql_enum_passthrough() -> None:
    assert alter_to_sql("Role") == "Role"


def test_alter_to_sql_unknown_raises() -> None:
    with pytest.raises(KeyError):
        alter_to_sql("unknowntype")


# ---------------------------------------------------------------------------
# python_to_alter
# ---------------------------------------------------------------------------


def test_python_to_alter_uuid_qualified() -> None:
    assert python_to_alter("uuid.UUID") == "uuid"


def test_python_to_alter_str() -> None:
    assert python_to_alter("str") == "string"


def test_python_to_alter_int() -> None:
    assert python_to_alter("int") == "int"


def test_python_to_alter_bool() -> None:
    assert python_to_alter("bool") == "bool"


def test_python_to_alter_float() -> None:
    assert python_to_alter("float") == "float"


def test_python_to_alter_datetime_qualified() -> None:
    assert python_to_alter("datetime.datetime") == "datetime"


def test_python_to_alter_datetime_unqualified() -> None:
    assert python_to_alter("datetime") == "datetime"


def test_python_to_alter_optional_str() -> None:
    assert python_to_alter("Optional[str]") == "string"


def test_python_to_alter_optional_uuid() -> None:
    assert python_to_alter("Optional[uuid.UUID]") == "uuid"


def test_python_to_alter_optional_int() -> None:
    assert python_to_alter("Optional[int]") == "int"


def test_python_to_alter_decimal_qualified() -> None:
    assert python_to_alter("decimal.Decimal") == "decimal"


def test_python_to_alter_decimal_unqualified() -> None:
    assert python_to_alter("Decimal") == "decimal"


def test_python_to_alter_dict() -> None:
    assert python_to_alter("dict") == "json"


def test_python_to_alter_bytes() -> None:
    assert python_to_alter("bytes") == "bytes"


def test_python_to_alter_enum_passthrough() -> None:
    assert python_to_alter("Role") == "Role"
    assert python_to_alter("SubscriptionStatus") == "SubscriptionStatus"


def test_python_to_alter_unknown_lowercase_raises() -> None:
    with pytest.raises(KeyError):
        python_to_alter("unknowntype")


# ---------------------------------------------------------------------------
# sql_to_alter
# ---------------------------------------------------------------------------


def test_sql_to_alter_uuid() -> None:
    assert sql_to_alter("UUID") == "uuid"


def test_sql_to_alter_varchar_with_length() -> None:
    assert sql_to_alter("VARCHAR(255)") == "string"


def test_sql_to_alter_varchar_no_length() -> None:
    assert sql_to_alter("VARCHAR") == "string"


def test_sql_to_alter_text() -> None:
    assert sql_to_alter("TEXT") == "text"


def test_sql_to_alter_integer() -> None:
    assert sql_to_alter("INTEGER") == "int"


def test_sql_to_alter_int() -> None:
    assert sql_to_alter("INT") == "int"


def test_sql_to_alter_bigint() -> None:
    assert sql_to_alter("BIGINT") == "bigint"


def test_sql_to_alter_boolean() -> None:
    assert sql_to_alter("BOOLEAN") == "bool"


def test_sql_to_alter_timestamptz() -> None:
    assert sql_to_alter("TIMESTAMPTZ") == "datetime"


def test_sql_to_alter_timestamp() -> None:
    assert sql_to_alter("TIMESTAMP") == "datetime"


def test_sql_to_alter_jsonb() -> None:
    assert sql_to_alter("JSONB") == "json"


def test_sql_to_alter_json() -> None:
    assert sql_to_alter("JSON") == "json"


def test_sql_to_alter_numeric() -> None:
    assert sql_to_alter("NUMERIC") == "decimal"


def test_sql_to_alter_double_precision() -> None:
    assert sql_to_alter("DOUBLE PRECISION") == "float"


def test_sql_to_alter_bytea() -> None:
    assert sql_to_alter("BYTEA") == "bytes"


def test_sql_to_alter_lowercase_input() -> None:
    # Should normalise to uppercase
    assert sql_to_alter("text") == "text"
    assert sql_to_alter("integer") == "int"
    assert sql_to_alter("varchar(100)") == "string"


def test_sql_to_alter_unknown_raises() -> None:
    with pytest.raises(KeyError):
        sql_to_alter("UNKNOWN_TYPE_XYZ")


# ---------------------------------------------------------------------------
# is_enum_type
# ---------------------------------------------------------------------------


def test_is_enum_type_true_for_pascal_case() -> None:
    assert is_enum_type("Role") is True
    assert is_enum_type("SubscriptionStatus") is True
    assert is_enum_type("InvoiceStatus") is True


def test_is_enum_type_false_for_built_in() -> None:
    for t in TYPE_MAP:
        assert is_enum_type(t) is False, f"{t!r} should not be an enum type"


def test_is_enum_type_false_for_empty_string() -> None:
    assert is_enum_type("") is False


# ---------------------------------------------------------------------------
# TYPE_MAP completeness
# ---------------------------------------------------------------------------


def test_type_map_covers_all_expected_types() -> None:
    expected = {
        "uuid", "string", "text", "int", "bigint", "float",
        "decimal", "bool", "datetime", "date", "time", "json", "bytes",
    }
    assert set(TYPE_MAP.keys()) == expected


def test_all_type_map_entries_have_python_and_sql() -> None:
    for alter_type, entry in TYPE_MAP.items():
        assert entry.python_type, f"Missing python_type for {alter_type!r}"
        assert entry.sql_type, f"Missing sql_type for {alter_type!r}"
