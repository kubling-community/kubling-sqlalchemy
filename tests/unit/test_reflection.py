from __future__ import annotations

from types import SimpleNamespace

import pytest
from kubling.v1 import value_pb2
from sqlalchemy import exc
from sqlalchemy.sql.sqltypes import ARRAY, Integer, NullType, Numeric, String

from kubling_sqlalchemy.dialect import KublingDialect
from kubling_sqlalchemy.transport import KublingClob, KublingLobReference


class FakeResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def __iter__(self):
        return iter(self.rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None


class FakeDbapiConnection:
    def __init__(self):
        self.materialized = []

    def materialize_lob(self, reference):
        self.materialized.append(reference)
        return "SELECT 2"


class FakeConnection:
    def __init__(self):
        self.dbapi_connection = FakeDbapiConnection()
        self.connection = SimpleNamespace(dbapi_connection=self.dbapi_connection)
        self.calls = []
        self.view_body = KublingClob("SELECT 1")

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        parameters = parameters or {}
        self.calls.append((sql, parameters))
        if "FROM SYS.Schemas" in sql:
            return FakeResult([("SYS",), ("acceptance",)])
        if "FROM SYS.Tables" in sql and "IsMaterialized = false" in sql:
            return FakeResult([("GRPC_B2_TEST",), ("orders",)])
        if "FROM SYS.Tables" in sql and "IsMaterialized = true" in sql:
            return FakeResult([("daily_sales",)])
        if "FROM SYSADMIN.Views" in sql and "SELECT Name" in sql:
            return FakeResult([("active_orders",)])
        if "SELECT Name FROM SYS.Tables" in sql:
            rows = [(parameters["table_name"],)] if parameters["table_name"] == "orders" else []
            return FakeResult(rows)
        if "FROM SYS.Columns" in sql:
            return FakeResult(
                [
                    {
                        "Name": "code",
                        "DataType": "string",
                        "Length": 40,
                        "Precision": 40,
                        "Scale": 0,
                        "NullType": "No Nulls",
                        "DefaultValue": None,
                        "IsAutoIncremented": False,
                        "Description": "business code",
                    },
                    {
                        "Name": "amount",
                        "DataType": "bigdecimal",
                        "Length": 0,
                        "Precision": 12,
                        "Scale": 3,
                        "NullType": "Nullable",
                        "DefaultValue": "0",
                        "IsAutoIncremented": False,
                        "Description": None,
                    },
                    {
                        "Name": "sequence",
                        "DataType": "integer",
                        "Length": 32,
                        "Precision": 32,
                        "Scale": 0,
                        "NullType": "No Nulls",
                        "DefaultValue": None,
                        "IsAutoIncremented": True,
                        "Description": None,
                    },
                    {
                        "Name": "labels",
                        "DataType": "string[][]",
                        "Length": 20,
                        "Precision": 20,
                        "Scale": 0,
                        "NullType": "Nullable",
                        "DefaultValue": None,
                        "IsAutoIncremented": False,
                        "Description": None,
                    },
                    {
                        "Name": "future",
                        "DataType": "future_type",
                        "Length": 0,
                        "Precision": 0,
                        "Scale": 0,
                        "NullType": "Unknown",
                        "DefaultValue": None,
                        "IsAutoIncremented": False,
                        "Description": None,
                    },
                ]
            )
        if "FROM SYS.KeyColumns" in sql:
            if parameters["table_name"] != "orders":
                return FakeResult([])
            if parameters["key_type"] == "Primary":
                return FakeResult(
                    [
                        {"KeyName": "pk_orders", "Name": "tenant", "Position": 1},
                        {"KeyName": "pk_orders", "Name": "id", "Position": 2},
                    ]
                )
            if parameters["key_type"] == "Index":
                return FakeResult(
                    [
                        {"KeyName": "ix_status", "Name": "tenant", "Position": 1},
                        {"KeyName": "ix_status", "Name": "status", "Position": 2},
                    ]
                )
            return FakeResult(
                [
                    {"KeyName": "uq_code", "Name": "tenant", "Position": 1},
                    {"KeyName": "uq_code", "Name": "code", "Position": 2},
                ]
            )
        if "FROM SYS.ReferenceKeyColumns" in sql:
            if parameters["table_name"] != "orders":
                return FakeResult([])
            return FakeResult(
                [
                    {
                        "FK_NAME": "fk_customer",
                        "FKCOLUMN_NAME": "tenant",
                        "PKTABLE_SCHEM": "crm",
                        "PKTABLE_NAME": "customers",
                        "PKCOLUMN_NAME": "tenant",
                        "KEY_SEQ": 1,
                        "UPDATE_RULE": 3,
                        "DELETE_RULE": 0,
                    },
                    {
                        "FK_NAME": "fk_customer",
                        "FKCOLUMN_NAME": "customer_id",
                        "PKTABLE_SCHEM": "crm",
                        "PKTABLE_NAME": "customers",
                        "PKCOLUMN_NAME": "id",
                        "KEY_SEQ": 2,
                        "UPDATE_RULE": 3,
                        "DELETE_RULE": 0,
                    },
                ]
            )
        if "SELECT Description FROM SYS.Tables" in sql:
            return FakeResult([("orders catalog",)])
        if "SELECT Body FROM SYSADMIN.Views" in sql:
            return FakeResult([(self.view_body,)])
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.fixture
def dialect():
    current = KublingDialect()
    current.default_schema_name = None
    return current


def test_schema_table_and_view_discovery_requires_no_invented_default(dialect):
    connection = FakeConnection()

    assert dialect.get_schema_names(connection) == ["SYS", "acceptance"]
    assert dialect.get_table_names(connection, schema="acceptance") == [
        "GRPC_B2_TEST",
        "orders",
    ]
    assert dialect.get_view_names(connection, schema="acceptance") == [
        "active_orders"
    ]
    assert dialect.get_materialized_view_names(
        connection, schema="acceptance"
    ) == ["daily_sales"]
    assert dialect.has_table(connection, "orders", schema="acceptance") is True
    assert dialect.has_table(connection, "missing", schema="acceptance") is False
    assert dialect.get_table_names(connection) == []
    assert dialect.has_table(connection, "orders") is False


def test_reflection_cache_can_be_invalidated_by_clearing_info_cache(dialect):
    connection = FakeConnection()
    info_cache = {}

    assert dialect.get_schema_names(connection, info_cache=info_cache) == [
        "SYS",
        "acceptance",
    ]
    assert dialect.get_schema_names(connection, info_cache=info_cache) == [
        "SYS",
        "acceptance",
    ]
    assert len(connection.calls) == 1

    info_cache.clear()
    dialect.get_schema_names(connection, info_cache=info_cache)
    assert len(connection.calls) == 2


def test_column_reflection_preserves_shape_and_uses_nulltype_for_unknown(dialect):
    connection = FakeConnection()

    with pytest.warns(exc.SAWarning, match="future_type"):
        columns = dialect.get_columns(connection, "orders", schema="sales")

    assert [column["name"] for column in columns] == [
        "code",
        "amount",
        "sequence",
        "labels",
        "future",
    ]
    assert isinstance(columns[0]["type"], String)
    assert columns[0]["type"].length == 40
    assert columns[0]["nullable"] is False
    assert columns[0]["comment"] == "business code"
    assert isinstance(columns[1]["type"], Numeric)
    assert columns[1]["type"].precision == 12
    assert columns[1]["type"].scale == 3
    assert columns[1]["default"] == "0"
    assert isinstance(columns[2]["type"], Integer)
    assert columns[2]["autoincrement"] is True
    assert isinstance(columns[3]["type"], ARRAY)
    assert columns[3]["type"].dimensions == 2
    assert isinstance(columns[3]["type"].item_type, String)
    assert columns[3]["type"].item_type.length == 20
    assert isinstance(columns[4]["type"], NullType)
    assert columns[4]["nullable"] is True

    with pytest.raises(exc.NoSuchTableError, match="explicit schema"):
        dialect.get_columns(connection, "orders")


def test_composite_primary_unique_and_foreign_keys_keep_column_order(dialect):
    connection = FakeConnection()

    assert dialect.get_pk_constraint(connection, "orders", schema="sales") == {
        "name": "pk_orders",
        "constrained_columns": ["tenant", "id"],
    }
    assert dialect.get_unique_constraints(connection, "orders", schema="sales") == [
        {"name": "uq_code", "column_names": ["tenant", "code"]}
    ]
    assert dialect.get_indexes(connection, "orders", schema="sales") == [
        {
            "name": "ix_status",
            "column_names": ["tenant", "status"],
            "unique": False,
        }
    ]
    assert dialect.get_check_constraints(connection, "orders", schema="sales") == []
    assert dialect.get_table_options(connection, "orders", schema="sales") == {}
    assert dialect.get_foreign_keys(connection, "orders", schema="sales") == [
        {
            "name": "fk_customer",
            "constrained_columns": ["tenant", "customer_id"],
            "referred_schema": "crm",
            "referred_table": "customers",
            "referred_columns": ["tenant", "id"],
            "options": {"onupdate": "NO ACTION", "ondelete": "CASCADE"},
        }
    ]
    with pytest.raises(exc.NoSuchTableError, match="sales.missing"):
        dialect.get_pk_constraint(connection, "missing", schema="sales")


def test_comments_and_view_definitions_support_inline_and_referenced_clobs(dialect):
    connection = FakeConnection()

    assert dialect.get_table_comment(connection, "orders", schema="sales") == {
        "text": "orders catalog"
    }
    assert (
        dialect.get_view_definition(connection, "active_orders", schema="sales")
        == "SELECT 1"
    )

    reference = KublingLobReference(
        lob_id="view-body",
        type=value_pb2.VALUE_TYPE_CLOB,
        session_id="session-1",
        size_bytes=8,
        expires_at_unix_ms=2_000_000_000_000,
    )
    connection.view_body = reference
    assert (
        dialect.get_view_definition(connection, "active_orders", schema="sales")
        == "SELECT 2"
    )
    assert connection.dbapi_connection.materialized == [reference]
