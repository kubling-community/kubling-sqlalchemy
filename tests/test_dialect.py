import pytest
from sqlalchemy import create_engine, inspect
from kubling.dialect import KublingDialect

@pytest.fixture
def engine():
    return create_engine("kubling://sa:sa@127.0.0.1:35432/EmptyVDB")

def test_dbapi():
    print(KublingDialect.dbapi())

def test_dialect_registration():
    assert KublingDialect.name == "kubling"


def test_table_names(engine):
    inspector = inspect(engine)
    tables = inspector.get_table_names(schema="kube1")
    assert isinstance(tables, list)  # Ensure the result is a list
    print("Tables:", tables)


def test_column_metadata(engine):
    inspector = inspect(engine)
    columns = inspector.get_columns(table_name="EVENT", schema="kube1")
    print("Columns:", columns)
    assert isinstance(columns, list)
    for column in columns:
        assert "name" in column
        assert "type" in column
        print("Column:", column)

def test_foreign_keys(engine):
    inspector = inspect(engine)
    inspector.get_temp_view_names()
    foreign_keys = inspector.get_foreign_keys(table_name="Procedures", schema="SYS")
    assert isinstance(foreign_keys, list)
    for fk in foreign_keys:
        assert "name" in fk
        assert "constrained_columns" in fk
        assert "referred_schema" in fk
        assert "referred_table" in fk
        assert "referred_columns" in fk
        print("Column:", fk)