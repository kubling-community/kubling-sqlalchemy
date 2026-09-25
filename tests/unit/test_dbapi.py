from datetime import date, datetime, time

import grpc
import pytest
from kubling import features
from kubling.v1 import error_pb2, transaction_pb2, value_pb2

from kubling_sqlalchemy import dbapi
from kubling_sqlalchemy.dbapi.connection import Connection
from kubling_sqlalchemy.transport import (
    Column,
    ExecutionEnd,
    KublingLocalTime,
    KublingLocalTimestamp,
    KublingRpcError,
    KublingLobReference,
    ResultRows,
    ResultSetEnd,
    ResultSetStart,
    RpcErrorDetails,
    TransactionStatus,
    UpdateResult,
    type_descriptor,
)


class FakeStream:
    def __init__(self, events):
        self._events = iter(events)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        item = next(self._events)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class FakeClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.begin_calls = 0
        self.commit_calls = []
        self.rollback_calls = []
        self.execute_calls = []
        self.streams = []
        self.closed = False
        self.commit_error = None
        self.ping_result = True
        self.released_lobs = []
        self.session_id = "session-1"
        self.server_info = type("ServerInfo", (), {"server_version": "26.2-RC4"})()
        self.advertised_features = frozenset(
            {
                features.ARRAY_VALUES_V1,
                features.SPATIAL_VALUES_V1,
                features.LOB_READ_V1,
            }
        )

    def begin_transaction(self):
        self.begin_calls += 1
        return f"tx-{self.begin_calls}"

    def commit_transaction(self, transaction_id):
        self.commit_calls.append(transaction_id)
        if self.commit_error is not None:
            raise self.commit_error
        return TransactionStatus(
            transaction_id,
            transaction_pb2.TRANSACTION_STATE_COMMITTED,
        )

    def rollback_transaction(self, transaction_id):
        self.rollback_calls.append(transaction_id)
        return TransactionStatus(
            transaction_id,
            transaction_pb2.TRANSACTION_STATE_ROLLED_BACK,
        )

    def execute(
        self,
        sql,
        parameters,
        *,
        transaction_id,
        batch_size,
        accepted_features,
    ):
        values = tuple(parameters)
        self.execute_calls.append(
            (sql, values, transaction_id, batch_size, frozenset(accepted_features))
        )
        state = (
            TransactionStatus(transaction_id, transaction_pb2.TRANSACTION_STATE_ACTIVE)
            if transaction_id
            else TransactionStatus("", transaction_pb2.TRANSACTION_STATE_NONE)
        )
        if sql == "update":
            events = [UpdateResult(1, (1,)), ExecutionEnd(1, state)]
        elif sql == "unknown update":
            events = [UpdateResult(1, (None,)), ExecutionEnd(1, state)]
        elif sql == "temporal query":
            columns = (
                Column(
                    "local_time",
                    "time",
                    False,
                    15,
                    6,
                    type_descriptor(value_pb2.VALUE_TYPE_TIME),
                ),
                Column(
                    "local_timestamp",
                    "timestamp",
                    False,
                    35,
                    6,
                    type_descriptor(value_pb2.VALUE_TYPE_TIMESTAMP),
                ),
                Column(
                    "nanosecond_timestamp",
                    "timestamp",
                    False,
                    38,
                    9,
                    type_descriptor(value_pb2.VALUE_TYPE_TIMESTAMP),
                ),
            )
            events = [
                ResultSetStart(1, columns, 1, None),
                ResultRows(
                    1,
                    (
                        (
                            KublingLocalTime("12:34:56.123456"),
                            KublingLocalTimestamp(
                                "2026-09-20T12:34:56.123456"
                            ),
                            KublingLocalTimestamp(
                                "2026-09-20T12:34:56.123456789"
                            ),
                        ),
                    ),
                ),
                ResultSetEnd(1, 1),
                ExecutionEnd(1, state),
            ]
        else:
            columns = (
                Column(
                    "id",
                    "integer",
                    False,
                    10,
                    0,
                    type_descriptor(value_pb2.VALUE_TYPE_INTEGER),
                ),
                Column(
                    "name",
                    "string",
                    True,
                    0,
                    0,
                    type_descriptor(value_pb2.VALUE_TYPE_STRING),
                ),
            )
            events = [
                ResultSetStart(1, columns, 1, None),
                ResultRows(1, ((1, "one"),)),
            ]
            if sql == "late failure":
                events.append(
                    KublingRpcError(
                        RpcErrorDetails(
                            grpc_code=grpc.StatusCode.INVALID_ARGUMENT,
                            message="statement failed",
                            stable_code="KBL-TEST",
                            sql_state="42000",
                            category=error_pb2.ERROR_CATEGORY_SQL,
                            retryability=error_pb2.RETRYABILITY_NEVER,
                            transaction_id=transaction_id or None,
                            transaction_state=(
                                transaction_pb2.TRANSACTION_STATE_ROLLBACK_ONLY
                                if transaction_id
                                else None
                            ),
                            sql_executed=True,
                        )
                    )
                )
            else:
                events.extend(
                    [
                        ResultRows(1, ((2, "two"),)),
                        ResultSetEnd(1, 2),
                        ExecutionEnd(1, state),
                    ]
                )
        stream = FakeStream(events)
        self.streams.append(stream)
        return stream

    def close(self):
        self.closed = True

    def ping(self):
        return self.ping_result

    def read_lob(self, reference):
        return FakeStream((b"catalog ", b"definition"))

    def release_lob(self, reference):
        self.released_lobs.append(reference)


@pytest.fixture
def resources():
    client = FakeClient()
    session = FakeSession()
    connection = Connection(client, session)
    return client, session, connection


def test_module_declares_pep249_surface_and_type_categories():
    assert dbapi.apilevel == "2.0"
    assert dbapi.threadsafety == 1
    assert dbapi.paramstyle == "qmark"
    assert issubclass(dbapi.ProgrammingError, dbapi.DatabaseError)
    assert issubclass(dbapi.DatabaseError, dbapi.Error)
    assert dbapi.STRING == value_pb2.VALUE_TYPE_STRING
    assert dbapi.NUMBER == value_pb2.VALUE_TYPE_BIGDECIMAL
    assert dbapi.BINARY == value_pb2.VALUE_TYPE_BLOB
    assert dbapi.DATETIME == value_pb2.VALUE_TYPE_TIMESTAMP
    assert dbapi.ROWID != value_pb2.VALUE_TYPE_LONG
    assert dbapi.Date(2026, 9, 18) == date(2026, 9, 18)
    assert dbapi.Time(12, 30, 5) == time(12, 30, 5)
    assert dbapi.Timestamp(2026, 9, 18, 12, 30, 5) == datetime(
        2026, 9, 18, 12, 30, 5
    )
    assert dbapi.Binary(bytearray(b"abc")) == b"abc"


def test_connection_materializes_and_releases_clob(resources):
    _, session, connection = resources
    reference = KublingLobReference(
        lob_id="lob-1",
        type=value_pb2.VALUE_TYPE_CLOB,
        session_id="session-1",
        size_bytes=18,
        expires_at_unix_ms=2_000_000_000_000,
    )

    assert connection.materialize_lob(reference) == "catalog definition"
    assert session.released_lobs == [reference]


def test_query_fetches_incrementally_and_exposes_lossless_metadata(resources):
    _, session, connection = resources
    cursor = connection.cursor()
    cursor.arraysize = 1

    assert cursor.execute("query", [7]) is cursor
    assert session.execute_calls == [
        (
            "query",
            (7,),
            "tx-1",
            1,
            frozenset(
                {
                    features.ARRAY_VALUES_V1,
                    features.SPATIAL_VALUES_V1,
                    features.LOB_READ_V1,
                }
            ),
        )
    ]
    assert cursor.description == (
        ("id", value_pb2.VALUE_TYPE_INTEGER, None, None, 10, None, False),
        ("name", value_pb2.VALUE_TYPE_STRING, None, None, None, None, True),
    )
    assert cursor.columns[0].declared_type.type == value_pb2.VALUE_TYPE_INTEGER
    assert cursor.rowcount == -1
    assert cursor.fetchone() == (1, "one")
    assert cursor.rowcount == -1
    assert cursor.fetchmany() == [(2, "two")]
    assert cursor.fetchone() is None
    assert cursor.rowcount == 2


def test_query_returns_standard_temporal_values_when_conversion_is_lossless(resources):
    _, _, connection = resources
    cursor = connection.cursor().execute("temporal query")

    assert cursor.fetchone() == (
        time(12, 34, 56, 123456),
        datetime(2026, 9, 20, 12, 34, 56, 123456),
        KublingLocalTimestamp("2026-09-20T12:34:56.123456789"),
    )
    assert cursor.fetchone() is None


def test_only_one_unfinished_execution_is_allowed_per_connection(resources):
    _, session, connection = resources
    first = connection.cursor()
    second = connection.cursor()

    first.execute("query")
    with pytest.raises(dbapi.OperationalError, match="another cursor"):
        second.execute("query")

    first.close()
    assert session.streams[0].closed is True
    assert second.execute("query").fetchall() == [(1, "one"), (2, "two")]


def test_update_executemany_and_successive_transactions(resources):
    _, session, connection = resources
    cursor = connection.cursor()

    cursor.execute("update", [1])
    assert cursor.description is None
    assert cursor.rowcount == 1
    cursor.executemany("update", [(2,), (3,)])
    assert cursor.rowcount == 2
    assert session.begin_calls == 1
    assert all(call[2] == "tx-1" for call in session.execute_calls)

    connection.commit()
    assert session.commit_calls == ["tx-1"]
    assert connection.transaction_id is None

    cursor.execute("unknown update")
    assert cursor.rowcount == -1
    assert connection.transaction_id == "tx-2"
    connection.rollback()
    assert session.rollback_calls == ["tx-2"]


def test_autocommit_uses_no_explicit_transaction(resources):
    _, session, connection = resources
    connection.autocommit = True

    connection.cursor().execute("update")

    assert session.begin_calls == 0
    assert session.execute_calls[0][2] == ""
    assert connection.transaction_id is None


def test_connection_exposes_pool_health_and_session_identity(resources):
    _, session, connection = resources

    assert connection.ping() is True
    assert connection.session_id == "session-1"
    assert connection.server_version == "26.2-RC4"

    session.ping_result = False
    assert connection.ping() is False


def test_late_error_is_raised_during_fetch_with_structured_details(resources):
    _, _, connection = resources
    cursor = connection.cursor().execute("late failure")

    assert cursor.fetchone() == (1, "one")
    with pytest.raises(dbapi.ProgrammingError) as captured:
        cursor.fetchone()

    error = captured.value
    assert error.stable_code == "KBL-TEST"
    assert error.sqlstate == "42000"
    assert error.sql_executed is True
    assert connection.transaction_state == transaction_pb2.TRANSACTION_STATE_ROLLBACK_ONLY
    with pytest.raises(dbapi.OperationalError, match="rollback-only"):
        connection.cursor().execute("update")
    with pytest.raises(dbapi.OperationalError, match="rollback-only"):
        connection.commit()
    connection.rollback()


def test_commit_with_unknown_outcome_is_never_retried(resources):
    _, session, connection = resources
    connection.cursor().execute("update")
    session.commit_error = KublingRpcError(
        RpcErrorDetails(
            grpc_code=grpc.StatusCode.UNAVAILABLE,
            message="connection lost",
        )
    )

    with pytest.raises(dbapi.OperationalError):
        connection.commit()
    with pytest.raises(dbapi.OperationalError, match="will not be retried"):
        connection.commit()

    assert session.commit_calls == ["tx-1"]
    assert connection.transaction_state == transaction_pb2.TRANSACTION_STATE_UNKNOWN
    connection.close()
    assert session.rollback_calls == []


def test_commit_discards_an_unfinished_result_visibly(resources):
    _, session, connection = resources
    cursor = connection.cursor().execute("query")
    stream = session.streams[0]

    connection.commit()

    assert stream.closed is True
    assert cursor.description is None
    with pytest.raises(dbapi.ProgrammingError, match="did not produce"):
        cursor.fetchone()


def test_close_cancels_pending_result_rolls_back_and_closes_owned_resources(resources):
    client, session, connection = resources
    cursor = connection.cursor().execute("query")

    connection.close()

    assert session.streams[0].closed is True
    assert session.rollback_calls == ["tx-1"]
    assert session.closed is True
    assert client.closed is True
    assert cursor.closed is True
    assert connection.closed is True
    connection.close()
    with pytest.raises(dbapi.InterfaceError):
        connection.cursor()


def test_parameter_and_fetch_misuse_raise_dbapi_errors(resources):
    _, _, connection = resources
    cursor = connection.cursor()

    with pytest.raises(dbapi.ProgrammingError, match="positional"):
        cursor.execute("query", {"value": 1})
    with pytest.raises(dbapi.ProgrammingError, match="did not produce"):
        cursor.execute("update").fetchone()
    with pytest.raises(dbapi.NotSupportedError, match="row-producing"):
        cursor.executemany("query", [(), ()])
    cursor.close()
    with pytest.raises(dbapi.InterfaceError):
        cursor.execute("query")
