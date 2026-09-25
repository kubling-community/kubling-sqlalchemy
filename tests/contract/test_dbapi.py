from concurrent import futures

import grpc
import pytest
from kubling import features
from kubling.v1 import command_pb2_grpc, lob_pb2_grpc, transaction_pb2, value_pb2

from kubling_sqlalchemy import dbapi

from .test_client import ContractService


@pytest.fixture
def dbapi_server():
    service = ContractService()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    command_pb2_grpc.add_SessionServiceServicer_to_server(service, server)
    command_pb2_grpc.add_QueryServiceServicer_to_server(service, server)
    lob_pb2_grpc.add_LobServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield service, f"127.0.0.1:{port}"
    finally:
        server.stop(grace=0).wait()


@pytest.fixture
def connection(dbapi_server):
    _, endpoint = dbapi_server
    current = dbapi.connect(
        endpoint,
        tls=None,
        vdb_name="test-vdb",
        vdb_version="1",
        username="alice",
        password="not-logged",
    )
    try:
        yield current
    finally:
        current.close()


def test_dbapi_connect_execute_fetch_and_commit_use_official_services(
    dbapi_server,
    connection,
):
    service, _ = dbapi_server
    cursor = connection.cursor()
    cursor.arraysize = 1

    returned = cursor.execute("select ?", (7,))

    assert returned is cursor
    assert [item.name for item in cursor.description] == ["id", "name"]
    assert cursor.fetchmany() == [(1, "one")]
    assert cursor.fetchall() == [(2, "two")]
    assert cursor.rowcount == 2
    assert service.execute_request.params[0].value.integer_value == 7
    assert (
        service.execute_request.params[0].declared_type.type
        == value_pb2.VALUE_TYPE_INTEGER
    )
    assert service.execute_request.transaction_id == "tx-1"
    assert service.execute_request.batch_size == 1
    assert set(service.execute_request.accepted_features) == {
        features.ARRAY_VALUES_V1,
        features.SPATIAL_VALUES_V1,
        features.LOB_READ_V1,
    }

    connection.commit()
    assert connection.transaction_id is None


def test_dbapi_updates_and_successive_transactions_use_explicit_ids(connection):
    cursor = connection.cursor()

    cursor.execute("update")
    assert cursor.description is None
    assert cursor.rowcount == 1
    first = connection.transaction_id
    connection.rollback()

    cursor.execute("update")
    second = connection.transaction_id
    connection.commit()

    assert first == "tx-1"
    assert second == "tx-2"
    assert connection.transaction_state == transaction_pb2.TRANSACTION_STATE_NONE


def test_dbapi_late_error_is_not_reported_as_success(connection):
    cursor = connection.cursor().execute("late failure")

    assert cursor.fetchone() == (1, "one")
    with pytest.raises(dbapi.ProgrammingError) as captured:
        cursor.fetchone()

    assert captured.value.stable_code == "KBL-TEST"
    assert captured.value.sqlstate == "42000"
    assert captured.value.sql_executed is True
    assert cursor.rowcount == -1
    assert connection.transaction_state == transaction_pb2.TRANSACTION_STATE_ROLLBACK_ONLY


def test_dbapi_autocommit_reports_no_transaction(dbapi_server):
    _, endpoint = dbapi_server
    connection = dbapi.connect(
        endpoint,
        tls=None,
        vdb_name="test-vdb",
        username="alice",
        password="not-logged",
        autocommit=True,
    )
    try:
        cursor = connection.cursor().execute("update")
        assert cursor.rowcount == 1
        assert connection.transaction_id is None
    finally:
        connection.close()
