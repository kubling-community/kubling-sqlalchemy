import os
import re
from pathlib import Path

import grpc
import pytest
from kubling import features
from kubling.v1 import transaction_pb2, value_pb2

from kubling_sqlalchemy.transport import (
    BoundParameter,
    ExecutionEnd,
    GrpcConfig,
    KublingGrpcClient,
    KublingRpcError,
    ProtocolError,
    ResultRows,
    ResultSetEnd,
    ResultSetStart,
    TlsConfig,
    UpdateResult,
    type_descriptor,
)


pytestmark = pytest.mark.integration


def _required_env(name):
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"--integration requires {name}.", pytrace=False)
    return value


def _optional_bytes(name):
    path = os.environ.get(name)
    return Path(path).read_bytes() if path else None


def _live_grpc_config():
    insecure = os.environ.get("KUBLING_GRPC_INSECURE") == "1"
    tls = None
    if not insecure:
        tls = TlsConfig(
            root_certificates=_optional_bytes("KUBLING_GRPC_CA_FILE"),
            private_key=_optional_bytes("KUBLING_GRPC_CLIENT_KEY_FILE"),
            certificate_chain=_optional_bytes("KUBLING_GRPC_CLIENT_CERT_FILE"),
            server_name_override=os.environ.get("KUBLING_GRPC_SERVER_NAME"),
        )
    timeout = float(os.environ.get("KUBLING_GRPC_TIMEOUT_SECONDS", "30"))
    return GrpcConfig(
        _required_env("KUBLING_GRPC_TARGET"),
        tls=tls,
        connect_timeout_seconds=timeout,
        rpc_timeout_seconds=timeout,
    )


@pytest.fixture(scope="module")
def grpc_session(request):
    if not request.config.getoption("--integration"):
        pytest.skip("Use --integration to enable database access.")
    client = KublingGrpcClient(_live_grpc_config())
    try:
        session = client.open_session(
            vdb_name=_required_env("KUBLING_GRPC_VDB"),
            vdb_version=_required_env("KUBLING_GRPC_VDB_VERSION"),
            username=_required_env("KUBLING_GRPC_USERNAME"),
            password=_required_env("KUBLING_GRPC_PASSWORD"),
            application_name="kubling-sqlalchemy-b2-acceptance",
        )
    except Exception:
        client.close()
        raise
    try:
        yield session
    finally:
        try:
            session.close()
        finally:
            client.close()


@pytest.fixture(scope="module")
def grpc_test_table():
    table = _required_env("KUBLING_GRPC_TEST_TABLE")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", table):
        pytest.fail("KUBLING_GRPC_TEST_TABLE must be a dotted SQL identifier.", pytrace=False)
    return table


def test_live_session_has_required_capabilities(grpc_session):
    assert grpc_session.ping() is True
    assert grpc_session.vdb_name == os.environ["KUBLING_GRPC_VDB"]
    assert grpc_session.vdb_version == os.environ["KUBLING_GRPC_VDB_VERSION"]
    required = {
        features.GENERIC_EXECUTE_V1,
        features.TYPED_PARAMETERS_V1,
        features.STRUCTURED_ERRORS_V1,
        features.TRANSACTION_IDS_V1,
        features.TRANSACTION_STATUS_V1,
        features.LOB_READ_V1,
        features.LOB_WRITE_V1,
    }
    assert required <= grpc_session.advertised_features
    assert grpc_session.server_info.capabilities.capability_id


def test_live_parameter_binding_empty_result_and_multiple_batches(
    grpc_session,
    grpc_test_table,
):
    string = type_descriptor(value_pb2.VALUE_TYPE_STRING)
    marker = os.environ.get("KUBLING_GRPC_TEST_MARKER", "grpc-b2")
    sql = f"SELECT marker, seq FROM {grpc_test_table} WHERE marker = ? ORDER BY seq"

    events = list(
        grpc_session.execute(
            sql,
            [BoundParameter(marker, string)],
            batch_size=1,
        )
    )
    starts = [event for event in events if isinstance(event, ResultSetStart)]
    batches = [event for event in events if isinstance(event, ResultRows)]
    ends = [event for event in events if isinstance(event, ResultSetEnd)]

    assert len(starts) == len(ends) == 1
    assert [column.name.lower() for column in starts[0].columns] == ["marker", "seq"]
    assert len(batches) >= 2
    rows = [row for batch in batches for row in batch.rows]
    assert len(rows) >= 2
    assert all(row[0] == marker for row in rows)
    assert ends[0].row_count == len(rows)
    assert isinstance(events[-1], ExecutionEnd)

    empty_events = list(
        grpc_session.execute(
            sql,
            [BoundParameter(f"{marker}-absent", string)],
            batch_size=1,
        )
    )
    empty_start = next(event for event in empty_events if isinstance(event, ResultSetStart))
    empty_end = next(event for event in empty_events if isinstance(event, ResultSetEnd))
    assert [column.name.lower() for column in empty_start.columns] == ["marker", "seq"]
    assert not any(isinstance(event, ResultRows) and event.rows for event in empty_events)
    assert empty_end.row_count == 0


@pytest.mark.parametrize(
    ("sql", "values", "declared_types", "expected"),
    [
        (
            "SELECT ? || ?",
            ("left", "right"),
            (value_pb2.VALUE_TYPE_STRING, value_pb2.VALUE_TYPE_STRING),
            [("leftright",)],
        ),
        (
            "SELECT ? / ?",
            (8, 2),
            (value_pb2.VALUE_TYPE_INTEGER, value_pb2.VALUE_TYPE_INTEGER),
            [(4,)],
        ),
        (
            "SELECT 1 FROM (SELECT 1 AS x) AS d WHERE ? = ?",
            (7, 7),
            (value_pb2.VALUE_TYPE_LONG, value_pb2.VALUE_TYPE_LONG),
            [(1,)],
        ),
    ],
)
def test_live_typed_parameters_resolve_ambiguous_operators(
    grpc_session,
    sql,
    values,
    declared_types,
    expected,
):
    parameters = [
        BoundParameter(value, type_descriptor(declared_type))
        for value, declared_type in zip(values, declared_types)
    ]

    events = list(grpc_session.execute(sql, parameters))
    rows = [row for event in events if isinstance(event, ResultRows) for row in event.rows]

    assert rows == expected
    assert isinstance(events[-1], ExecutionEnd)


def test_live_transactions_and_update_counts(grpc_session, grpc_test_table):
    integer = type_descriptor(value_pb2.VALUE_TYPE_INTEGER)
    sql = f"UPDATE {grpc_test_table} SET marker = marker WHERE seq = ?"

    rollback_id = grpc_session.begin_transaction()
    rollback_events = list(
        grpc_session.execute(
            sql,
            [BoundParameter(1, integer)],
            transaction_id=rollback_id,
        )
    )
    rollback_update = next(
        event for event in rollback_events if isinstance(event, UpdateResult)
    )
    assert rollback_update.counts == (1,)
    rolled_back = grpc_session.rollback_transaction(rollback_id)
    assert rolled_back.transaction_id == rollback_id
    assert rolled_back.state == transaction_pb2.TRANSACTION_STATE_ROLLED_BACK

    commit_id = grpc_session.begin_transaction()
    commit_events = list(
        grpc_session.execute(
            sql,
            [BoundParameter(1, integer)],
            transaction_id=commit_id,
        )
    )
    commit_update = next(event for event in commit_events if isinstance(event, UpdateResult))
    assert commit_update.counts == (1,)
    committed = grpc_session.commit_transaction(commit_id)
    assert committed.transaction_id == commit_id
    assert committed.state == transaction_pb2.TRANSACTION_STATE_COMMITTED


def test_live_structured_sql_error(grpc_session, grpc_test_table):
    with pytest.raises(KublingRpcError) as captured:
        list(grpc_session.execute(f"SELECT missing_b2_column FROM {grpc_test_table}"))

    details = captured.value.details
    assert details.stable_code
    assert details.category is not None
    assert details.retryability is not None
    assert details.sql_executed is not None


def test_live_error_after_rows_never_completes(grpc_session):
    stream = grpc_session.execute(
        _required_env("KUBLING_GRPC_LATE_ERROR_SQL"),
        batch_size=1,
    )
    rows_before_error = 0

    with pytest.raises(KublingRpcError) as captured:
        for event in stream:
            if isinstance(event, ResultRows) and event.rows:
                rows_before_error += len(event.rows)

    assert rows_before_error == 2
    assert stream.complete is False
    assert captured.value.details.grpc_code == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert captured.value.details.stable_code == "KBL61007"
    assert captured.value.details.sql_executed is True


def test_live_lob_round_trip(grpc_session):
    payload = b"kubling-grpc-b2-lob"

    reference = grpc_session.write_lob(payload)
    try:
        assert b"".join(grpc_session.read_lob(reference)) == payload
    finally:
        grpc_session.release_lob(reference)


def test_live_invalid_credentials_keep_authentication_details():
    client = KublingGrpcClient(_live_grpc_config())
    try:
        with pytest.raises(KublingRpcError) as captured:
            client.open_session(
                vdb_name=_required_env("KUBLING_GRPC_VDB"),
                vdb_version=_required_env("KUBLING_GRPC_VDB_VERSION"),
                username=_required_env("KUBLING_GRPC_USERNAME"),
                password=_required_env("KUBLING_GRPC_PASSWORD")
                + "-definitely-invalid",
                application_name="kubling-sqlalchemy-b6-invalid-auth",
            )
    finally:
        client.close()

    assert captured.value.details.grpc_code == grpc.StatusCode.UNAUTHENTICATED
    assert captured.value.details.stable_code == "KBL61001"
    assert captured.value.details.sql_executed is None


def test_closed_session_rejects_further_work_locally():
    client = KublingGrpcClient(_live_grpc_config())
    session = client.open_session(
        vdb_name=_required_env("KUBLING_GRPC_VDB"),
        vdb_version=_required_env("KUBLING_GRPC_VDB_VERSION"),
        username=_required_env("KUBLING_GRPC_USERNAME"),
        password=_required_env("KUBLING_GRPC_PASSWORD"),
        application_name="kubling-sqlalchemy-b6-close",
    )
    try:
        session.close()
        with pytest.raises(ProtocolError, match="closed"):
            session.ping()
    finally:
        client.close()


def test_unreachable_endpoint_honors_connect_deadline():
    client = KublingGrpcClient(
        GrpcConfig(
            "127.0.0.1:1",
            tls=None,
            connect_timeout_seconds=0.05,
            rpc_timeout_seconds=0.05,
        )
    )
    try:
        with pytest.raises(KublingRpcError) as captured:
            client.wait_until_ready()
    finally:
        client.close()

    assert captured.value.details.grpc_code == grpc.StatusCode.DEADLINE_EXCEEDED
    assert captured.value.details.sql_executed is None
