from concurrent import futures

import grpc
import pytest
from kubling.v1 import command_pb2_grpc, lob_pb2_grpc
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from .test_client import ContractService


@pytest.fixture
def dialect_server():
    service = ContractService()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    command_pb2_grpc.add_SessionServiceServicer_to_server(service, server)
    command_pb2_grpc.add_QueryServiceServicer_to_server(service, server)
    lob_pb2_grpc.add_LobServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield service, port
    finally:
        server.stop(grace=0).wait()


@pytest.fixture
def engine(dialect_server):
    _, port = dialect_server
    url = URL.create(
        "kubling",
        username="alice",
        password="not-logged",
        host="127.0.0.1",
        port=port,
        database="test-vdb",
        query={"vdb_version": "1", "insecure": "true"},
    )
    current = create_engine(
        url,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
    )
    try:
        yield current
    finally:
        current.dispose()


def test_create_engine_executes_core_sql_and_reuses_a_clean_pool_connection(
    dialect_server,
    engine,
):
    service, _ = dialect_server
    statement = text("select :value")

    with engine.connect() as connection:
        first_session = connection.connection.dbapi_connection.session_id
        first = connection.execute(statement, {"value": 7})
        assert first.keys() == ["id", "name"]
        assert first.fetchall() == [(1, "one"), (2, "two")]

    with engine.connect() as connection:
        second_session = connection.connection.dbapi_connection.session_id
        second = connection.execute(statement, {"value": 8})
        assert second.fetchall() == [(1, "one"), (2, "two")]

    assert first_session == second_session
    assert service.login_count == 1
    assert service.ping_count == 1
    assert [request.params[0].value.integer_value for request in service.execute_requests] == [
        7,
        8,
    ]
    assert [request.sql for request in service.execute_requests] == [
        "select ?",
        "select ?",
    ]


def test_sqlalchemy_transactions_delegate_commit_and_rollback(dialect_server, engine):
    service, _ = dialect_server

    with engine.begin() as connection:
        result = connection.execute(text("update"))
        assert result.rowcount == 1

    with engine.connect() as connection:
        result = connection.execute(text("update"))
        assert result.rowcount == 1
        connection.rollback()

    assert service.commit_count == 1
    assert service.rollback_count >= 1


def test_pool_pre_ping_replaces_an_invalid_session(dialect_server, engine):
    service, _ = dialect_server

    with engine.connect():
        pass
    assert service.login_count == 1

    service.ping_valid = False
    with engine.connect() as connection:
        assert connection.connection.dbapi_connection.session_id == "session-1"

    assert service.ping_count == 1
    assert service.login_count == 2
    assert service.logout_count >= 1


def test_autocommit_execution_option_uses_no_explicit_transaction(
    dialect_server,
    engine,
):
    service, _ = dialect_server

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        result = connection.execute(text("update"))
        assert result.rowcount == 1

    request = service.execute_requests[-1]
    assert request.transaction_id == ""
