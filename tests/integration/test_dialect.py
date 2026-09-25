import os

import pytest
from kubling.v1 import command_pb2
from sqlalchemy import (
    Integer,
    String,
    bindparam,
    cast,
    column,
    exc,
    func,
    select,
    table,
    text,
    update,
)

from kubling_sqlalchemy import dbapi


pytestmark = pytest.mark.integration


@pytest.fixture
def fixture_table(schema_name, table_name):
    return table(
        table_name,
        column("marker", String),
        column("seq", Integer),
        schema=schema_name,
    )


def test_native_dialect_connects_and_exposes_server_version(engine):
    assert engine.dialect.name == "kubling"
    assert engine.dialect.driver == "grpc"
    assert engine.dialect.dbapi is dbapi

    with engine.connect() as connection:
        assert connection.dialect.server_version_info == (26, 2)
        assert connection.default_isolation_level == "KUBLING DEFAULT"
        assert connection.connection.dbapi_connection.ping() is True


def test_core_select_bind_limit_offset_and_statement_cache(
    engine,
    fixture_table,
):
    marker = os.environ.get("KUBLING_GRPC_TEST_MARKER", "grpc-b2")
    statement = (
        select(fixture_table.c.marker, fixture_table.c.seq)
        .where(fixture_table.c.marker == bindparam("marker"))
        .order_by(fixture_table.c.seq)
        .limit(bindparam("limit"))
        .offset(bindparam("offset"))
    )

    with engine.connect() as connection:
        first = connection.execute(
            statement,
            {"marker": marker, "limit": 1, "offset": 0},
        ).all()
        second = connection.execute(
            statement,
            {"marker": marker, "limit": 1, "offset": 1},
        ).all()

    assert first == [(marker, 1)]
    assert second == [(marker, 2)]
    assert len(engine._compiled_cache) == 1


def test_core_join_aggregate_grouping_and_cast(engine, fixture_table):
    marker = os.environ.get("KUBLING_GRPC_TEST_MARKER", "grpc-b2")
    left = fixture_table.alias("left_rows")
    right = fixture_table.alias("right_rows")
    statement = (
        select(
            cast(left.c.seq, String).label("seq_text"),
            func.count(right.c.seq).label("matches"),
        )
        .select_from(left.join(right, left.c.seq == right.c.seq))
        .where(left.c.marker == bindparam("marker"))
        .group_by(left.c.seq)
        .order_by(left.c.seq)
    )

    with engine.connect() as connection:
        rows = connection.execute(statement, {"marker": marker}).all()

    assert rows == [("1", 1), ("2", 1)]


def test_pool_reuses_session_and_rolls_back_uncommitted_work(
    engine,
    fixture_table,
):
    marker = os.environ.get("KUBLING_GRPC_TEST_MARKER", "grpc-b2")
    temporary = "grpc-b4-pool-reset"
    change = (
        update(fixture_table)
        .where(fixture_table.c.seq == bindparam("target"))
        .values(marker=bindparam("new_marker"))
    )
    read = select(fixture_table.c.marker).where(fixture_table.c.seq == bindparam("target"))

    with engine.connect() as connection:
        first_session = connection.connection.dbapi_connection.session_id
        changed = connection.execute(change, {"target": 1, "new_marker": temporary})
        assert changed.rowcount == 1

    with engine.connect() as connection:
        second_session = connection.connection.dbapi_connection.session_id
        observed = connection.execute(read, {"target": 1}).scalar_one()

    assert first_session == second_session
    assert observed == marker


def test_core_executemany_commit_and_rollback(engine, fixture_table):
    statement = (
        update(fixture_table)
        .where(fixture_table.c.seq == bindparam("target"))
        .values(marker=fixture_table.c.marker)
    )

    with engine.begin() as connection:
        result = connection.execute(statement, [{"target": 1}, {"target": 1}])
        assert result.rowcount == 2

    with engine.connect() as connection:
        result = connection.execute(statement, {"target": 1})
        assert result.rowcount == 1
        connection.rollback()


def test_dbapi_errors_are_wrapped_by_sqlalchemy(engine, fixture_table):
    statement = text(
        f"SELECT missing_b4_column FROM {fixture_table.schema}.{fixture_table.name}"
    )

    with engine.connect() as connection:
        with pytest.raises(exc.ProgrammingError) as captured:
            connection.execute(statement)

    assert isinstance(captured.value.orig, dbapi.ProgrammingError)
    assert captured.value.orig.stable_code
    assert captured.value.orig.sql_executed is not None


def test_pool_pre_ping_replaces_a_session_invalidated_by_server(engine):
    with engine.connect() as connection:
        dbapi_connection = connection.connection.dbapi_connection
        first_session_id = dbapi_connection.session_id
        session = dbapi_connection._session
        response = session._session_stub.Logout(
            command_pb2.LogoutRequest(
                expiring_token=session._login.expiring_token,
            ),
            timeout=10,
            metadata=session._metadata,
        )
        assert response.success is True

    with engine.connect() as connection:
        second_session_id = connection.connection.dbapi_connection.session_id
        assert connection.execute(text("SELECT 1")).scalar_one() == 1

    assert second_session_id != first_session_id
