import pytest
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CHAR,
    Date,
    DateTime,
    Float,
    Integer,
    JSON,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    Time,
    inspect,
)

from kubling_sqlalchemy.dialect import Geography, Geometry, XML


pytestmark = pytest.mark.integration

REFLECTION_SCHEMA = "reflection"


def test_inspector_discovers_acceptance_schema_and_tables(engine):
    inspector = inspect(engine)

    assert "acceptance" in inspector.get_schema_names()
    assert {"GRPC_B2_TEST", "LATE_ERROR_PAYLOAD"} <= set(
        inspector.get_table_names(schema="acceptance")
    )
    assert inspector.get_view_names(schema="acceptance") == []
    assert inspector.has_table("GRPC_B2_TEST", schema="acceptance") is True
    assert inspector.has_table("MISSING_B5_TABLE", schema="acceptance") is False
    assert inspector.default_schema_name is None
    assert inspector.get_table_names() == []


def test_inspector_reflects_columns_primary_key_and_comment(engine):
    inspector = inspect(engine)

    columns = inspector.get_columns("GRPC_B2_TEST", schema="acceptance")
    assert [column["name"] for column in columns] == ["MARKER", "SEQ"]
    assert isinstance(columns[0]["type"], String)
    assert columns[0]["type"].length == 64
    assert columns[0]["nullable"] is False
    assert isinstance(columns[1]["type"], Integer)
    assert columns[1]["nullable"] is False
    assert inspector.get_pk_constraint(
        "GRPC_B2_TEST", schema="acceptance"
    ) == {
        "name": "CONSTRAINT_8",
        "constrained_columns": ["SEQ"],
    }
    assert inspector.get_table_comment(
        "GRPC_B2_TEST", schema="acceptance"
    ) == {"text": None}


def test_table_autoload_uses_native_grpc_reflection(engine):
    metadata = MetaData()

    reflected = Table(
        "GRPC_B2_TEST",
        metadata,
        schema="acceptance",
        autoload_with=engine,
    )

    assert list(reflected.c.keys()) == ["MARKER", "SEQ"]
    assert [column.name for column in reflected.primary_key.columns] == ["SEQ"]


def test_multi_reflection_and_composite_system_foreign_key(engine):
    inspector = inspect(engine)

    columns = inspector.get_multi_columns(
        schema="acceptance",
        filter_names=["GRPC_B2_TEST"],
    )
    assert list(columns) == [("acceptance", "GRPC_B2_TEST")]
    assert [column["name"] for column in columns[("acceptance", "GRPC_B2_TEST")]] == [
        "MARKER",
        "SEQ",
    ]

    primary_keys = inspector.get_multi_pk_constraint(
        schema="acceptance",
        filter_names=["GRPC_B2_TEST"],
    )
    assert primary_keys[("acceptance", "GRPC_B2_TEST")][
        "constrained_columns"
    ] == ["SEQ"]

    unique_constraints = inspector.get_multi_unique_constraints(
        schema="acceptance",
        filter_names=["GRPC_B2_TEST"],
    )
    assert unique_constraints[("acceptance", "GRPC_B2_TEST")] == [
        {"name": "PRIMARY_KEY_8", "column_names": ["SEQ"]}
    ]

    foreign_keys = inspector.get_foreign_keys("Columns", schema="SYS")
    catalog_fk = next(item for item in foreign_keys if item["name"] == "FK0")
    assert catalog_fk["constrained_columns"] == [
        "VDBName",
        "SchemaName",
        "TableName",
    ]
    assert catalog_fk["referred_schema"] == "SYS"
    assert catalog_fk["referred_table"] == "Tables"
    assert catalog_fk["referred_columns"] == ["VDBName", "SchemaName", "Name"]

    multi_foreign_keys = inspector.get_multi_foreign_keys(
        schema="SYS",
        filter_names=["Columns"],
    )
    assert multi_foreign_keys[("SYS", "Columns")] == foreign_keys


def test_system_view_definition_materializes_clob_reference(engine):
    inspector = inspect(engine)
    view_name = "information_schema.columns"

    assert view_name in inspector.get_view_names(schema="pg_catalog")
    definition = inspector.get_view_definition(view_name, schema="pg_catalog")

    assert "FROM sys.columns" in definition


def test_metadata_reflect_discovers_selected_table(engine):
    metadata = MetaData()

    metadata.reflect(
        bind=engine,
        schema="acceptance",
        only=["GRPC_B2_TEST"],
    )

    assert list(metadata.tables) == ["acceptance.GRPC_B2_TEST"]
    reflected = metadata.tables["acceptance.GRPC_B2_TEST"]
    assert list(reflected.c.keys()) == ["MARKER", "SEQ"]


def test_b5_fixture_separates_tables_and_views(engine):
    inspector = inspect(engine)

    assert REFLECTION_SCHEMA in inspector.get_schema_names()
    table_names = set(inspector.get_table_names(schema=REFLECTION_SCHEMA))
    assert {"B5_CHILD", "B5_PARENT", "B5_TYPES"} <= table_names
    assert "B5_PARENT_VIEW" not in table_names
    assert "B5_PARENT_VIEW" in inspector.get_view_names(schema=REFLECTION_SCHEMA)
    assert inspector.get_materialized_view_names(schema=REFLECTION_SCHEMA) == []
    assert inspector.has_table("B5_PARENT", schema=REFLECTION_SCHEMA) is True
    assert inspector.has_table("B5_PARENT_VIEW", schema=REFLECTION_SCHEMA) is True


def test_b5_fixture_preserves_column_metadata_and_comments(engine):
    inspector = inspect(engine)

    columns = inspector.get_columns("B5_PARENT", schema=REFLECTION_SCHEMA)
    assert [column["name"] for column in columns] == [
        "tenant_id",
        "parent_id",
        "code",
        "amount",
        "note",
    ]
    by_name = {column["name"]: column for column in columns}

    assert isinstance(by_name["tenant_id"]["type"], String)
    assert by_name["tenant_id"]["nullable"] is False
    assert isinstance(by_name["parent_id"]["type"], Integer)
    assert by_name["parent_id"]["nullable"] is False
    assert isinstance(by_name["code"]["type"], String)
    assert by_name["code"]["type"].length == 32
    assert by_name["code"]["nullable"] is False
    assert by_name["code"]["comment"] == "Stable code within a tenant"
    assert isinstance(by_name["amount"]["type"], Numeric)
    assert by_name["amount"]["type"].precision == 12
    assert by_name["amount"]["type"].scale == 3
    assert by_name["amount"]["default"] == "0.000"
    assert by_name["amount"]["nullable"] is True
    assert isinstance(by_name["note"]["type"], String)
    assert by_name["note"]["type"].length == 80
    assert by_name["note"]["nullable"] is True
    assert inspector.get_table_comment("B5_PARENT", schema=REFLECTION_SCHEMA) == {
        "text": "Parent fixture for generic SQL catalog reflection"
    }


def test_b5_fixture_preserves_composite_constraints(engine):
    inspector = inspect(engine)

    assert inspector.get_pk_constraint(
        "B5_PARENT", schema=REFLECTION_SCHEMA
    ) == {
        "name": "PK_B5_PARENT",
        "constrained_columns": ["tenant_id", "parent_id"],
    }
    assert inspector.get_unique_constraints(
        "B5_PARENT", schema=REFLECTION_SCHEMA
    ) == [
        {
            "name": "UK_B5_PARENT_TENANT_CODE",
            "column_names": ["tenant_id", "code"],
        }
    ]
    assert inspector.get_pk_constraint(
        "B5_CHILD", schema=REFLECTION_SCHEMA
    ) == {
        "name": "PK_B5_CHILD",
        "constrained_columns": ["child_id"],
    }
    assert inspector.get_foreign_keys(
        "B5_CHILD", schema=REFLECTION_SCHEMA
    ) == [
        {
            "name": "FK_B5_CHILD_PARENT",
            "constrained_columns": ["tenant_ref", "parent_ref"],
            "referred_schema": REFLECTION_SCHEMA,
            "referred_table": "B5_PARENT",
            "referred_columns": ["tenant_id", "parent_id"],
            "options": {"onupdate": "NO ACTION", "ondelete": "NO ACTION"},
        }
    ]


def test_b5_fixture_reflects_scalar_and_array_types(engine):
    columns = inspect(engine).get_columns("B5_TYPES", schema=REFLECTION_SCHEMA)
    by_name = {column["name"]: column["type"] for column in columns}

    expected_types = {
        "string_value": String,
        "char_value": CHAR,
        "boolean_value": Boolean,
        "byte_value": SmallInteger,
        "short_value": SmallInteger,
        "integer_value": Integer,
        "long_value": BigInteger,
        "biginteger_value": Numeric,
        "bigdecimal_value": Numeric,
        "float_value": Float,
        "double_value": Float,
        "date_value": Date,
        "time_value": Time,
        "timestamp_value": DateTime,
        "varbinary_value": LargeBinary,
        "blob_value": LargeBinary,
        "clob_value": Text,
        "json_value": JSON,
        "xml_value": XML,
        "geometry_value": Geometry,
        "geography_value": Geography,
        "string_array_value": ARRAY,
    }
    assert set(by_name) == set(expected_types)
    for name, expected_type in expected_types.items():
        assert isinstance(by_name[name], expected_type), name

    assert by_name["string_value"].length == 48
    assert by_name["char_value"].length == 1
    assert by_name["bigdecimal_value"].precision == 18
    assert by_name["bigdecimal_value"].scale == 4
    assert by_name["varbinary_value"].length == 64
    assert by_name["string_array_value"].dimensions == 1
    assert isinstance(by_name["string_array_value"].item_type, String)


def test_b5_fixture_supports_empty_table_autoload_multi_and_view_lob(engine):
    inspector = inspect(engine)
    metadata = MetaData()

    parent = Table(
        "B5_PARENT",
        metadata,
        schema=REFLECTION_SCHEMA,
        autoload_with=engine,
    )
    assert list(parent.c.keys()) == [
        "tenant_id",
        "parent_id",
        "code",
        "amount",
        "note",
    ]
    assert [column.name for column in parent.primary_key.columns] == [
        "tenant_id",
        "parent_id",
    ]

    multi_columns = inspector.get_multi_columns(
        schema=REFLECTION_SCHEMA,
        filter_names=["B5_PARENT", "B5_TYPES"],
    )
    assert set(multi_columns) == {
        (REFLECTION_SCHEMA, "B5_PARENT"),
        (REFLECTION_SCHEMA, "B5_TYPES"),
    }

    reflected = MetaData()
    reflected.reflect(
        bind=engine,
        schema=REFLECTION_SCHEMA,
        only=["B5_PARENT", "B5_TYPES"],
    )
    assert set(reflected.tables) == {
        "reflection.B5_PARENT",
        "reflection.B5_TYPES",
    }

    assert inspector.get_view_definition(
        "B5_PARENT_VIEW", schema=REFLECTION_SCHEMA
    ) == "SELECT tenant_id, parent_id, code, amount, note FROM B5_PARENT"
