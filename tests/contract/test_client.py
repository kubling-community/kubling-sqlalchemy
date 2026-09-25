from concurrent import futures

import grpc
import pytest
from google.protobuf import any_pb2
from google.rpc import code_pb2, status_pb2
from grpc_status import rpc_status
from kubling import features
from kubling.v1 import (
    capability_pb2,
    command_pb2,
    command_pb2_grpc,
    error_pb2,
    lob_pb2,
    lob_pb2_grpc,
    transaction_pb2,
    value_pb2,
)

from kubling_sqlalchemy.transport import (
    BoundParameter,
    CapabilityError,
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


ALL_FEATURES = (
    features.GENERIC_EXECUTE_V1,
    features.TYPED_PARAMETERS_V1,
    features.STRUCTURED_ERRORS_V1,
    features.TRANSACTION_IDS_V1,
    features.TRANSACTION_STATUS_V1,
    features.ARRAY_VALUES_V1,
    features.SPATIAL_VALUES_V1,
    features.LOB_READ_V1,
    features.LOB_WRITE_V1,
    features.MULTIPLE_RESULTS_V1,
    features.GENERATED_KEYS_V1,
)


class ContractService(
    command_pb2_grpc.SessionServiceServicer,
    command_pb2_grpc.QueryServiceServicer,
    lob_pb2_grpc.LobServiceServicer,
):
    def __init__(self):
        self.login_request = None
        self.login_version_override = None
        self.execute_request = None
        self.execute_metadata = None
        self.written_lob = None
        self.released_lobs = []
        self.transaction_number = 0
        self.login_count = 0
        self.logout_count = 0
        self.ping_count = 0
        self.ping_valid = True
        self.execute_requests = []
        self.commit_count = 0
        self.rollback_count = 0

    def Login(self, request, context):
        self.login_count += 1
        self.login_request = request
        return command_pb2.LoginResponse(
            expiring_token="secret-session-token",
            expires_at=2_000_000_000,
            session_id="session-1",
            username=request.username,
            vdb_name=request.vdb_name,
            vdb_version=(
                self.login_version_override
                if self.login_version_override is not None
                else request.vdb_version
            ),
            timezone="UTC",
            affinity=transaction_pb2.Affinity(
                node_id="node-1",
                session_id="session-1",
                routing_token="route-1",
            ),
        )

    def Logout(self, request, context):
        self.logout_count += 1
        return command_pb2.LogoutResponse(success=True)

    def Ping(self, request, context):
        return command_pb2.PingResponse(ok=True)

    def PingSession(self, request, context):
        self.ping_count += 1
        return command_pb2.SessionPingResponse(
            valid=self.ping_valid and request.session_id == "session-1"
        )

    def GetServerInfo(self, request, context):
        assert ("kubling-affinity", "route-1") in tuple(context.invocation_metadata())
        return command_pb2.GetServerInfoResponse(
            server_version="test-server",
            features=ALL_FEATURES,
            capabilities=capability_pb2.Capabilities(
                protocol_version=capability_pb2.ProtocolVersion(major=1, minor=1),
                limits=capability_pb2.ProtocolLimits(
                    max_request_message_bytes=1024 * 1024,
                    max_response_message_bytes=1024 * 1024,
                    max_rows_per_batch=100,
                    max_batch_bytes=1024 * 1024,
                    max_lob_chunk_bytes=3,
                    max_array_dimensions=4,
                    max_lob_bytes=1024,
                ),
                supported_types=[
                    capability_pb2.SupportedType(type=value_type, input=True, output=True)
                    for value_type in range(1, value_pb2.VALUE_TYPE_ARRAY + 1)
                ],
                affinity_requirement=(
                    transaction_pb2.AFFINITY_REQUIREMENT_SESSION_AND_NODE
                ),
                affinity=transaction_pb2.Affinity(
                    node_id="node-1",
                    session_id="session-1",
                    routing_token="route-1",
                ),
                capability_id="capability-1",
                transaction_status_retention_seconds=60,
                lob_reference_retention_seconds=60,
            ),
        )

    def Execute(self, request, context):
        copied_request = command_pb2.ExecuteRequest()
        copied_request.CopyFrom(request)
        self.execute_requests.append(copied_request)
        self.execute_request = request
        self.execute_metadata = tuple(context.invocation_metadata())
        integer = type_descriptor(value_pb2.VALUE_TYPE_INTEGER)
        text = type_descriptor(value_pb2.VALUE_TYPE_STRING)
        transaction = transaction_pb2.TransactionStatus(
            transaction_id=request.transaction_id,
            state=(
                transaction_pb2.TRANSACTION_STATE_ACTIVE
                if request.transaction_id
                else transaction_pb2.TRANSACTION_STATE_NONE
            ),
        )
        if request.sql == "update":
            yield command_pb2.ExecuteResponse(
                update_result=command_pb2.UpdateResult(
                    result_id=1,
                    counts=[command_pb2.UpdateCount(affected_rows=1)],
                )
            )
            yield command_pb2.ExecuteResponse(
                execution_end=command_pb2.ExecutionEnd(
                    result_count=1,
                    transaction=transaction,
                )
            )
            return
        yield command_pb2.ExecuteResponse(
            result_set_start=command_pb2.ResultSetStart(
                result_id=1,
                role=command_pb2.RESULT_SET_ROLE_QUERY,
                columns=[
                    command_pb2.Column(name="id", data_type="integer", declared_type=integer),
                    command_pb2.Column(name="name", data_type="string", declared_type=text),
                ],
            )
        )
        if request.sql == "empty result":
            yield command_pb2.ExecuteResponse(
                result_set_end=command_pb2.ResultSetEnd(result_id=1, row_count=0)
            )
            yield command_pb2.ExecuteResponse(
                execution_end=command_pb2.ExecutionEnd(
                    result_count=1,
                    transaction=transaction,
                )
            )
            return
        yield command_pb2.ExecuteResponse(
            result_rows=command_pb2.ResultRows(
                result_id=1,
                rows=[
                    command_pb2.Row(
                        values=[
                            value_pb2.Value(integer_value=1),
                            value_pb2.Value(string_value="one"),
                        ]
                    )
                ],
            )
        )
        if request.sql == "late failure":
            detail = any_pb2.Any()
            detail.Pack(
                error_pb2.KublingError(
                    stable_code="KBL-TEST",
                    sql_state="42000",
                    vendor_code=42,
                    category=error_pb2.ERROR_CATEGORY_SQL,
                    retryability=error_pb2.RETRYABILITY_NEVER,
                    transaction=transaction_pb2.TransactionStatus(
                        transaction_id="tx-1",
                        state=transaction_pb2.TRANSACTION_STATE_ROLLBACK_ONLY,
                    ),
                    sql_executed=True,
                )
            )
            context.abort_with_status(
                rpc_status.to_status(
                    status_pb2.Status(
                        code=code_pb2.INVALID_ARGUMENT,
                        message="statement failed",
                        details=[detail],
                    )
                )
            )
        yield command_pb2.ExecuteResponse(
            result_rows=command_pb2.ResultRows(
                result_id=1,
                rows=[
                    command_pb2.Row(
                        values=[
                            value_pb2.Value(integer_value=2),
                            value_pb2.Value(string_value="two"),
                        ]
                    )
                ],
            )
        )
        yield command_pb2.ExecuteResponse(
            result_set_end=command_pb2.ResultSetEnd(result_id=1, row_count=2)
        )
        yield command_pb2.ExecuteResponse(
            execution_end=command_pb2.ExecutionEnd(
                result_count=1,
                transaction=transaction,
            )
        )

    def BeginTransaction(self, request, context):
        self.transaction_number += 1
        return command_pb2.BeginTransactionResponse(
            transaction_id=f"tx-{self.transaction_number}",
            affinity=transaction_pb2.Affinity(
                node_id="node-1",
                session_id="session-1",
                routing_token="route-1",
            ),
        )

    def CommitTransaction(self, request, context):
        self.commit_count += 1
        return command_pb2.CommitTransactionResponse(
            success=True,
            transaction=transaction_pb2.TransactionStatus(
                transaction_id=request.transaction_id,
                state=transaction_pb2.TRANSACTION_STATE_COMMITTED,
            ),
        )

    def RollbackTransaction(self, request, context):
        self.rollback_count += 1
        return command_pb2.RollbackTransactionResponse(
            success=True,
            transaction=transaction_pb2.TransactionStatus(
                transaction_id=request.transaction_id,
                state=transaction_pb2.TRANSACTION_STATE_ROLLED_BACK,
            ),
        )

    def GetTransactionStatus(self, request, context):
        return command_pb2.GetTransactionStatusResponse(
            transaction=transaction_pb2.TransactionStatus(
                transaction_id=request.transaction_id,
                state=transaction_pb2.TRANSACTION_STATE_UNKNOWN,
            )
        )

    def ReadLob(self, request, context):
        data = b"abcdef"
        end = len(data) if not request.HasField("length") else request.offset + request.length
        selected = data[request.offset:end]
        if not selected:
            yield lob_pb2.ReadLobResponse(
                offset=request.offset,
                data=b"",
                end_of_read=True,
            )
            return
        for relative in range(0, len(selected), 3):
            chunk = selected[relative : relative + 3]
            yield lob_pb2.ReadLobResponse(
                offset=request.offset + relative,
                data=chunk,
                end_of_read=relative + len(chunk) == len(selected),
            )

    def WriteLob(self, request_iterator, context):
        requests = list(request_iterator)
        assert requests[0].WhichOneof("part") == "start"
        chunks = requests[1:]
        self.written_lob = b"".join(request.chunk.data for request in chunks)
        start = requests[0].start
        return lob_pb2.WriteLobResponse(
            reference=value_pb2.LobReference(
                lob_id="lob-1",
                type=start.type,
                size_bytes=len(self.written_lob),
                session_id="session-1",
                expires_at_unix_ms=2_000_000_000_000,
            )
        )

    def ReleaseLob(self, request, context):
        self.released_lobs.append(request.lob_id)
        return lob_pb2.ReleaseLobResponse()


@pytest.fixture
def contract_server():
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
def session(contract_server):
    service, endpoint = contract_server
    client = KublingGrpcClient(GrpcConfig(endpoint, tls=None))
    current = client.open_session(
        vdb_name="test-vdb",
        username="alice",
        password="not-logged",
    )
    try:
        yield service, current
    finally:
        current.close()
        client.close()


def test_session_negotiates_capabilities_and_streams_typed_rows(session):
    service, current = session
    integer = type_descriptor(value_pb2.VALUE_TYPE_INTEGER)
    stream = current.execute(
        "select ?",
        [BoundParameter(7, integer)],
        batch_size=2,
    )

    events = list(stream)

    assert [type(event) for event in events] == [
        ResultSetStart,
        ResultRows,
        ResultRows,
        ResultSetEnd,
        ExecutionEnd,
    ]
    assert events[1].rows == ((1, "one"),)
    assert events[2].rows == ((2, "two"),)
    assert events[-1].transaction.state == transaction_pb2.TRANSACTION_STATE_NONE
    assert stream.complete is True
    assert service.execute_request.capability_id == "capability-1"
    assert service.execute_request.params[0].declared_type == integer
    assert ("kubling-affinity", "route-1") in service.execute_metadata
    assert service.login_request.password == "not-logged"


def test_login_rejects_a_mismatched_vdb_version(contract_server):
    service, endpoint = contract_server
    service.login_version_override = "0"
    client = KublingGrpcClient(GrpcConfig(endpoint, tls=None))
    try:
        with pytest.raises(ProtocolError, match="different VDB version"):
            client.open_session(
                vdb_name="test-vdb",
                vdb_version="1",
                username="alice",
                password="not-logged",
            )
    finally:
        client.close()


def test_late_rpc_error_never_becomes_success_and_keeps_structured_details(session):
    _, current = session
    stream = current.execute("late failure")

    assert isinstance(next(stream), ResultSetStart)
    assert isinstance(next(stream), ResultRows)
    with pytest.raises(KublingRpcError) as captured:
        next(stream)

    details = captured.value.details
    assert details.grpc_code == grpc.StatusCode.INVALID_ARGUMENT
    assert details.stable_code == "KBL-TEST"
    assert details.sql_state == "42000"
    assert details.vendor_code == 42
    assert details.sql_executed is True
    assert details.transaction_id == "tx-1"
    assert stream.complete is False


def test_empty_result_keeps_schema_and_completes_without_rows(session):
    _, current = session

    events = list(current.execute("empty result"))

    assert [type(event) for event in events] == [
        ResultSetStart,
        ResultSetEnd,
        ExecutionEnd,
    ]
    assert [column.name for column in events[0].columns] == ["id", "name"]
    assert events[1].row_count == 0


def test_transactions_preserve_explicit_ids_and_unknown_status(session):
    _, current = session

    transaction_id = current.begin_transaction()
    committed = current.commit_transaction(transaction_id)
    rolled_back = current.rollback_transaction(transaction_id)
    observed = current.get_transaction_status(transaction_id)

    assert transaction_id == "tx-1"
    assert committed.state == transaction_pb2.TRANSACTION_STATE_COMMITTED
    assert rolled_back.state == transaction_pb2.TRANSACTION_STATE_ROLLED_BACK
    assert observed.transaction_id == transaction_id
    assert observed.state == transaction_pb2.TRANSACTION_STATE_UNKNOWN


def test_lob_write_read_and_release_use_capability_limits(session):
    service, current = session

    reference = current.write_lob(b"abcdef")
    chunks = list(current.read_lob(reference, length=6, max_chunk_bytes=3))
    current.release_lob(reference)

    assert service.written_lob == b"abcdef"
    assert chunks == [b"abc", b"def"]
    assert reference.size_bytes == 6
    assert service.released_lobs == ["lob-1"]

    clob = current.write_lob(
        [b"\xc3", b"\xb1"],
        lob_type=value_pb2.VALUE_TYPE_CLOB,
        size_bytes=2,
    )
    assert clob.type == value_pb2.VALUE_TYPE_CLOB
    assert service.written_lob == "ñ".encode("utf-8")


def test_feature_acceptance_is_explicit_and_checked_before_rpc(session):
    service, current = session
    previous = service.execute_request

    with pytest.raises(CapabilityError, match="unsupported accepted_features"):
        current.execute("select 1", accepted_features=["future_feature_v9"])
    with pytest.raises(CapabilityError, match="explicit"):
        current.execute("insert", return_generated_keys=True)

    assert service.execute_request is previous


def test_tls_is_default_and_mtls_material_must_be_paired():
    assert isinstance(GrpcConfig("server.example:443").tls, TlsConfig)
    assert GrpcConfig("127.0.0.1:50051", tls=None).tls is None
    with pytest.raises(ValueError, match="supplied together"):
        TlsConfig(private_key=b"key")


class _CancelableResponses:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.cancelled = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._responses)

    def cancel(self):
        self.cancelled = True


def test_closing_unconsumed_execution_cancels_the_rpc():
    from kubling_sqlalchemy.transport.client import ExecutionStream

    responses = _CancelableResponses(
        [
            command_pb2.ExecuteResponse(
                update_result=command_pb2.UpdateResult(
                    result_id=1,
                    counts=[command_pb2.UpdateCount(affected_rows=1)],
                )
            )
        ]
    )
    stream = ExecutionStream(
        responses,
        accepted_features=frozenset(),
        supported_output_types=frozenset({value_pb2.VALUE_TYPE_INTEGER}),
        max_array_dimensions=0,
        max_response_message_bytes=1024,
        max_batch_bytes=1024,
        max_rows_per_batch=10,
    )

    assert next(stream) == UpdateResult(1, (1,))
    stream.close()

    assert responses.cancelled is True
    with pytest.raises(StopIteration):
        next(stream)


def test_missing_execution_end_is_a_protocol_error():
    from kubling_sqlalchemy.transport.client import ExecutionStream

    stream = ExecutionStream(
        iter(
            [
                command_pb2.ExecuteResponse(
                    update_result=command_pb2.UpdateResult(
                        result_id=1,
                        counts=[command_pb2.UpdateCount(affected_rows=0)],
                    )
                )
            ]
        ),
        accepted_features=frozenset(),
        supported_output_types=frozenset(),
        max_array_dimensions=0,
        max_response_message_bytes=1024,
        max_batch_bytes=1024,
        max_rows_per_batch=10,
    )

    assert next(stream) == UpdateResult(1, (0,))
    with pytest.raises(ProtocolError, match="without ExecutionEnd"):
        next(stream)
