import os
import re
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep

import grpc
import pytest
from kubling.v1 import value_pb2

from kubling_sqlalchemy import dbapi
from kubling_sqlalchemy.transport import KublingLobReference, TlsConfig


pytestmark = pytest.mark.integration

_LARGE_STREAM_SQL = (
    "SELECT a.Name, b.Name FROM SYS.Columns AS a "
    "CROSS JOIN SYS.Columns AS b LIMIT 10000"
)


def _required_env(name):
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"--integration requires {name}.", pytrace=False)
    return value


def _optional_bytes(name):
    path = os.environ.get(name)
    return Path(path).read_bytes() if path else None


def _open_dbapi_connection(*, rpc_timeout_seconds=None, autocommit=False):
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
    return dbapi.connect(
        _required_env("KUBLING_GRPC_TARGET"),
        tls=tls,
        vdb_name=_required_env("KUBLING_GRPC_VDB"),
        vdb_version=_required_env("KUBLING_GRPC_VDB_VERSION"),
        username=_required_env("KUBLING_GRPC_USERNAME"),
        password=_required_env("KUBLING_GRPC_PASSWORD"),
        connect_timeout_seconds=timeout,
        rpc_timeout_seconds=(
            timeout if rpc_timeout_seconds is None else rpc_timeout_seconds
        ),
        application_name="kubling-sqlalchemy-b3-acceptance",
        autocommit=autocommit,
    )


@pytest.fixture
def dbapi_connection(request):
    if not request.config.getoption("--integration"):
        pytest.skip("Use --integration to enable database access.")
    connection = _open_dbapi_connection()
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="module")
def dbapi_test_table():
    table = _required_env("KUBLING_GRPC_TEST_TABLE")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", table):
        pytest.fail("KUBLING_GRPC_TEST_TABLE must be a dotted SQL identifier.", pytrace=False)
    return table


def _fixture_query(table):
    return f"SELECT marker, seq FROM {table} WHERE marker = ? ORDER BY seq"


def _fetchall(connection, sql, parameters=()):
    cursor = connection.cursor().execute(sql, parameters)
    rows = cursor.fetchall()
    cursor.close()
    return rows


def _wait_until_absent(connection, sql, value, timeout_seconds=5):
    started = monotonic()
    while True:
        if not _fetchall(connection, sql, (value,)):
            return
        if monotonic() - started >= timeout_seconds:
            pytest.fail("server resource remained visible after cleanup", pytrace=False)
        sleep(0.05)


def test_live_dbapi_query_is_incremental_and_preserves_empty_schema(
    dbapi_connection,
    dbapi_test_table,
):
    marker = os.environ.get("KUBLING_GRPC_TEST_MARKER", "grpc-b2")
    cursor = dbapi_connection.cursor()
    cursor.arraysize = 1

    cursor.execute(_fixture_query(dbapi_test_table), (marker,))
    assert [item.name.lower() for item in cursor.description] == ["marker", "seq"]
    assert cursor.rowcount == -1
    assert cursor.fetchone() == (marker, 1)
    assert cursor.fetchmany() == [(marker, 2)]
    assert cursor.fetchone() is None
    assert cursor.rowcount == 2

    cursor.execute(_fixture_query(dbapi_test_table), (f"{marker}-absent",))
    assert [item.name.lower() for item in cursor.description] == ["marker", "seq"]
    assert cursor.fetchall() == []
    assert cursor.rowcount == 0
    dbapi_connection.commit()


def test_live_dbapi_rejects_concurrent_cursors_and_cancel_releases_slot(
    dbapi_connection,
    dbapi_test_table,
):
    marker = os.environ.get("KUBLING_GRPC_TEST_MARKER", "grpc-b2")
    first = dbapi_connection.cursor()
    second = dbapi_connection.cursor()
    first.arraysize = 1
    first.execute(_fixture_query(dbapi_test_table), (marker,))
    assert first.fetchone() == (marker, 1)

    with pytest.raises(dbapi.OperationalError, match="unfinished execution"):
        second.execute(_fixture_query(dbapi_test_table), (marker,))

    first.close()
    second.execute(_fixture_query(dbapi_test_table), (marker,))
    assert second.fetchall() == [(marker, 1), (marker, 2)]
    dbapi_connection.rollback()


def test_live_dbapi_cancel_large_stream_releases_server_session(dbapi_connection):
    cursor = dbapi_connection.cursor()
    cursor.arraysize = 1
    cursor.execute(_LARGE_STREAM_SQL)
    assert cursor.fetchone() is not None

    cursor.close()

    followup = dbapi_connection.cursor().execute("SELECT 1")
    assert followup.fetchone() == (1,)
    followup.close()


def test_live_dbapi_cancel_releases_observable_request_and_logout_session():
    target = _open_dbapi_connection(autocommit=True)
    observer = _open_dbapi_connection(autocommit=True)
    target_session = target.session_id
    try:
        cursor = target.cursor()
        cursor.arraysize = 1
        cursor.execute(
            "SELECT ? FROM SYS.Columns AS a CROSS JOIN SYS.Columns AS b LIMIT 100000",
            ("x" * 65536,),
        )
        assert cursor.fetchone() is not None
        assert _fetchall(
            observer,
            "SELECT SessionId, ExecutionId FROM SYSADMIN.REQUESTS "
            "WHERE SessionId = ?",
            (target_session,),
        )

        cursor.close()
        _wait_until_absent(
            observer,
            "SELECT SessionId, ExecutionId FROM SYSADMIN.REQUESTS "
            "WHERE SessionId = ?",
            target_session,
        )

        followup = target.cursor().execute("SELECT 1")
        assert followup.fetchone() == (1,)
        followup.close()
        target.close()

        _wait_until_absent(
            observer,
            "SELECT SessionId FROM SYSADMIN.SESSIONS WHERE SessionId = ?",
            target_session,
        )
    finally:
        if not target.closed:
            target.close()
        observer.close()


def test_live_dbapi_large_rows_make_progress_and_preserve_payload(dbapi_connection):
    payload = "x" * (16 * 1024)
    cursor = dbapi_connection.cursor()
    cursor.arraysize = 1
    cursor.execute("SELECT ? FROM SYS.Columns LIMIT 40", (payload,))

    rows = cursor.fetchall()

    assert len(rows) == 40
    assert all(row == (payload,) for row in rows)
    followup = dbapi_connection.cursor().execute("SELECT 1")
    assert followup.fetchone() == (1,)
    followup.close()


@pytest.mark.parametrize("size", [262143, 262144, 262145, 524288, 1048576])
def test_live_dbapi_materializes_and_releases_multichunk_clob(
    dbapi_connection,
    size,
):
    payload = "x" * size
    cursor = dbapi_connection.cursor().execute("SELECT CAST(? AS CLOB)", (payload,))
    reference = cursor.fetchone()[0]
    cursor.close()

    assert isinstance(reference, KublingLobReference)
    assert reference.size_bytes == len(payload)
    assert dbapi_connection.materialize_lob(reference) == payload

    followup = dbapi_connection.cursor().execute("SELECT 1")
    assert followup.fetchone() == (1,)
    followup.close()


def test_live_dbapi_stream_deadline_releases_server_session():
    connection = _open_dbapi_connection(rpc_timeout_seconds=0.05, autocommit=True)
    try:
        cursor = connection.cursor()
        cursor.arraysize = 1
        cursor.execute(_LARGE_STREAM_SQL)
        assert cursor.fetchone() is not None
        sleep(0.1)

        with pytest.raises(dbapi.OperationalError) as captured:
            while cursor.fetchone() is not None:
                pass

        assert captured.value.grpc_code == grpc.StatusCode.DEADLINE_EXCEEDED
        cursor.close()

        followup = connection.cursor().execute("SELECT 1")
        assert followup.fetchone() == (1,)
        followup.close()
    finally:
        connection.close()


def test_live_dbapi_executemany_commit_and_successive_rollback(
    dbapi_connection,
    dbapi_test_table,
):
    sql = f"UPDATE {dbapi_test_table} SET marker = marker WHERE seq = ?"
    cursor = dbapi_connection.cursor()

    cursor.executemany(sql, [(1,), (1,)])
    assert cursor.rowcount == 2
    first = dbapi_connection.transaction_id
    dbapi_connection.rollback()

    cursor.execute(sql, (1,))
    assert cursor.rowcount == 1
    second = dbapi_connection.transaction_id
    dbapi_connection.commit()

    assert first
    assert second
    assert first != second


def test_live_dbapi_late_error_preserves_rows_and_structured_error(dbapi_connection):
    cursor = dbapi_connection.cursor()
    cursor.arraysize = 1
    cursor.execute(_required_env("KUBLING_GRPC_LATE_ERROR_SQL"))
    rows = []

    with pytest.raises(dbapi.OperationalError) as captured:
        while True:
            row = cursor.fetchone()
            if row is None:
                break
            rows.append(row)

    assert len(rows) == 2
    assert captured.value.grpc_code == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert captured.value.stable_code == "KBL61007"
    assert captured.value.sql_executed is True
    assert cursor.rowcount == -1


def test_live_dbapi_structured_sql_error_maps_to_programming_error(
    dbapi_connection,
    dbapi_test_table,
):
    with pytest.raises(dbapi.ProgrammingError) as captured:
        dbapi_connection.cursor().execute(
            f"SELECT missing_b3_column FROM {dbapi_test_table}"
        )

    assert captured.value.stable_code
    assert captured.value.sqlstate
    assert captured.value.sql_executed is not None


@pytest.mark.parametrize(
    ("sql", "value", "expected", "type_code"),
    [
        ("SELECT CAST(? AS INTEGER)", None, None, value_pb2.VALUE_TYPE_INTEGER),
        (
            "SELECT CAST(? AS STRING)",
            "Kubling ñ 'quote'",
            "Kubling ñ 'quote'",
            value_pb2.VALUE_TYPE_STRING,
        ),
        (
            "SELECT CAST(? AS VARBINARY)",
            b"\x00\xff",
            b"\x00\xff",
            value_pb2.VALUE_TYPE_VARBINARY,
        ),
        (
            "SELECT CAST(? AS BIGDECIMAL(18,4))",
            Decimal("123456.7890"),
            Decimal("123456.7890"),
            value_pb2.VALUE_TYPE_BIGDECIMAL,
        ),
        ("SELECT CAST(? AS BOOLEAN)", True, True, value_pb2.VALUE_TYPE_BOOLEAN),
        (
            "SELECT CAST(? AS DATE)",
            date(2026, 9, 20),
            date(2026, 9, 20),
            value_pb2.VALUE_TYPE_DATE,
        ),
        (
            "SELECT CAST(? AS TIME)",
            time(12, 34, 56),
            time(12, 34, 56),
            value_pb2.VALUE_TYPE_TIME,
        ),
        (
            "SELECT CAST(? AS TIMESTAMP)",
            datetime(2026, 9, 20, 12, 34, 56, 123456),
            datetime(2026, 9, 20, 12, 34, 56, 123456),
            value_pb2.VALUE_TYPE_TIMESTAMP,
        ),
    ],
)
def test_live_dbapi_parameter_round_trip_is_exact(
    dbapi_connection,
    sql,
    value,
    expected,
    type_code,
):
    cursor = dbapi_connection.cursor().execute(sql, (value,))

    assert cursor.fetchone() == (expected,)
    assert cursor.fetchone() is None
    assert cursor.description[0].type_code == type_code


@pytest.mark.parametrize(
    ("sql", "parameters", "expected"),
    [
        ("SELECT ? || ?", ("left", "right"), ("leftright",)),
        ("SELECT ? / ?", (8, 2), (4,)),
        (
            "SELECT 1 FROM (SELECT 1 AS x) AS d WHERE ? = ?",
            (2**40, 2**40),
            (1,),
        ),
    ],
)
def test_live_dbapi_infers_types_for_ambiguous_operators(
    dbapi_connection,
    sql,
    parameters,
    expected,
):
    cursor = dbapi_connection.cursor().execute(sql, parameters)

    assert cursor.fetchone() == expected
    assert cursor.fetchone() is None


@pytest.mark.parametrize(
    ("sql", "parameters", "expected_count"),
    [
        ("SELECT ?", (), 1),
        ("SELECT 1", (1,), 0),
    ],
)
def test_live_dbapi_rejects_incorrect_parameter_count_before_execution(
    dbapi_connection,
    sql,
    parameters,
    expected_count,
):
    with pytest.raises(dbapi.DataError) as captured:
        dbapi_connection.cursor().execute(sql, parameters)

    assert captured.value.grpc_code == grpc.StatusCode.INVALID_ARGUMENT
    assert captured.value.stable_code == "KBL61005"
    assert captured.value.sql_executed is False
    assert f"expects {expected_count} parameters" in str(captured.value)


def test_live_dbapi_preserves_duplicate_column_names(
    dbapi_connection,
    dbapi_test_table,
):
    cursor = dbapi_connection.cursor().execute(
        f"SELECT seq, seq FROM {dbapi_test_table} WHERE seq = ?",
        (1,),
    )

    assert [column.name for column in cursor.description] == ["SEQ", "SEQ"]
    assert cursor.fetchall() == [(1, 1)]
