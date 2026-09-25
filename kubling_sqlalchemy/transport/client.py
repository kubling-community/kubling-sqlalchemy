"""Synchronous Kubling gRPC client built on the published v1 bindings."""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

import grpc
from kubling import features
from kubling.v1 import (
    capability_pb2,
    command_pb2,
    command_pb2_grpc,
    lob_pb2,
    lob_pb2_grpc,
    transaction_pb2,
    value_pb2,
)

from .codec import (
    BoundParameter,
    KublingLobReference,
    decode_value,
    descriptor_dimensions,
    encode_parameter,
    infer_type_descriptor,
    required_features_for_parameter,
    validate_type_descriptor,
)
from .errors import (
    CapabilityError,
    CodecError,
    ConfigurationError,
    KublingRpcError,
    ProtocolError,
    translate_rpc_error,
)


_ACCEPTABLE_OUTPUT_FEATURES = frozenset(
    {
        features.ARRAY_VALUES_V1,
        features.SPATIAL_VALUES_V1,
        features.LOB_READ_V1,
        features.MULTIPLE_RESULTS_V1,
        features.GENERATED_KEYS_V1,
    }
)


@dataclass(frozen=True, slots=True)
class TlsConfig:
    """TLS trust and optional client certificate material."""

    root_certificates: bytes | None = None
    private_key: bytes | None = None
    certificate_chain: bytes | None = None
    server_name_override: str | None = None

    def __post_init__(self) -> None:
        if (self.private_key is None) != (self.certificate_chain is None):
            raise ConfigurationError(
                "TLS private_key and certificate_chain must be supplied together"
            )
        if self.server_name_override == "":
            raise ConfigurationError("TLS server name override cannot be empty")


@dataclass(frozen=True, slots=True)
class GrpcConfig:
    """Connection settings. TLS is enabled by default; ``tls=None`` is explicit."""

    endpoint: str
    tls: TlsConfig | None = field(default_factory=TlsConfig)
    connect_timeout_seconds: float = 10.0
    rpc_timeout_seconds: float = 30.0
    wait_for_ready: bool = True
    max_send_message_bytes: int = 16 * 1024 * 1024
    max_receive_message_bytes: int = 64 * 1024 * 1024
    channel_options: tuple[tuple[str, str | int], ...] = ()

    def __post_init__(self) -> None:
        if not self.endpoint or not self.endpoint.strip():
            raise ConfigurationError("gRPC endpoint cannot be empty")
        if self.connect_timeout_seconds <= 0 or self.rpc_timeout_seconds <= 0:
            raise ConfigurationError("gRPC timeouts must be positive")
        if self.max_send_message_bytes <= 0 or self.max_receive_message_bytes <= 0:
            raise ConfigurationError("local gRPC message limits must be positive")
        reserved = {"grpc.max_send_message_length", "grpc.max_receive_message_length"}
        if any(name in reserved for name, _ in self.channel_options):
            raise ConfigurationError(
                "use max_send_message_bytes/max_receive_message_bytes for message limits"
            )


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    data_type: str
    nullable: bool
    precision: int
    scale: int
    declared_type: value_pb2.TypeDescriptor


@dataclass(frozen=True, slots=True)
class ResultSetStart:
    result_id: int
    columns: tuple[Column, ...]
    role: int
    parent_result_id: int | None


@dataclass(frozen=True, slots=True)
class ResultRows:
    result_id: int
    rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True, slots=True)
class UpdateResult:
    result_id: int
    counts: tuple[int | None, ...]


@dataclass(frozen=True, slots=True)
class ResultSetEnd:
    result_id: int
    row_count: int


@dataclass(frozen=True, slots=True)
class TransactionStatus:
    transaction_id: str
    state: int


@dataclass(frozen=True, slots=True)
class ExecutionEnd:
    result_count: int
    transaction: TransactionStatus


ExecutionEvent = ResultSetStart | ResultRows | UpdateResult | ResultSetEnd | ExecutionEnd


class ExecutionStream(Iterator[ExecutionEvent]):
    """Validate and decode an Execute stream without accumulating row batches."""

    def __init__(
        self,
        call: Iterator[command_pb2.ExecuteResponse],
        *,
        accepted_features: frozenset[str],
        supported_output_types: frozenset[int],
        max_array_dimensions: int,
        max_response_message_bytes: int,
        max_batch_bytes: int,
        max_rows_per_batch: int,
    ) -> None:
        self._call = call
        self._accepted_features = accepted_features
        self._supported_output_types = supported_output_types
        self._max_array_dimensions = max_array_dimensions
        self._max_response_message_bytes = max_response_message_bytes
        self._max_batch_bytes = max_batch_bytes
        self._max_rows_per_batch = max_rows_per_batch
        self._next_result_id = 1
        self._active_result_id: int | None = None
        self._active_columns: tuple[Column, ...] = ()
        self._active_rows = 0
        self._primary_results = 0
        self._update_results: set[int] = set()
        self._complete = False
        self._closed = False
        self.transaction: TransactionStatus | None = None

    @property
    def complete(self) -> bool:
        return self._complete

    def __iter__(self) -> "ExecutionStream":
        return self

    def __next__(self) -> ExecutionEvent:
        if self._complete or self._closed:
            raise StopIteration
        try:
            response = self._next_response()
            event = response.WhichOneof("event")
            if event == "result_set_start":
                return self._start_result_set(response.result_set_start)
            if event == "result_rows":
                return self._rows(response.result_rows)
            if event == "update_result":
                return self._update(response.update_result)
            if event == "result_set_end":
                return self._end_result_set(response.result_set_end)
            if event == "execution_end":
                return self._execution_end(response.execution_end)
            raise ProtocolError("ExecuteResponse has no recognized event")
        except ProtocolError:
            self.close()
            raise

    def close(self) -> None:
        """Cancel an execution that the consumer did not exhaust."""

        if self._closed:
            return
        self._closed = True
        if not self._complete:
            cancel = getattr(self._call, "cancel", None)
            if cancel is not None:
                cancel()

    def __enter__(self) -> "ExecutionStream":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _next_response(self) -> command_pb2.ExecuteResponse:
        try:
            response = next(self._call)
        except StopIteration as exc:
            raise ProtocolError("Execute stream ended without ExecutionEnd") from exc
        except grpc.RpcError as exc:
            raise translate_rpc_error(exc) from exc
        if response.ByteSize() > self._max_response_message_bytes:
            raise ProtocolError("Execute response exceeds the negotiated message limit")
        if response.WhichOneof("event") == "result_rows":
            if len(response.result_rows.rows) > self._max_rows_per_batch:
                raise ProtocolError("result batch exceeds the negotiated row limit")
            if response.result_rows.ByteSize() > self._max_batch_bytes:
                raise ProtocolError("result batch exceeds the negotiated byte limit")
        return response

    def _start_result_set(self, start: command_pb2.ResultSetStart) -> ResultSetStart:
        if self._active_result_id is not None:
            raise ProtocolError("a result set started before the previous one ended")
        self._claim_result_id(start.result_id)
        if start.role not in (
            command_pb2.RESULT_SET_ROLE_QUERY,
            command_pb2.RESULT_SET_ROLE_GENERATED_KEYS,
        ):
            raise ProtocolError("result set role is unspecified or unknown")

        parent = start.parent_result_id if start.HasField("parent_result_id") else None
        if start.role == command_pb2.RESULT_SET_ROLE_GENERATED_KEYS:
            self._require_accepted(features.GENERATED_KEYS_V1)
            if (
                parent is None
                or parent != start.result_id - 1
                or parent not in self._update_results
            ):
                raise ProtocolError("generated keys require a preceding update result")
        elif parent is not None:
            raise ProtocolError("query result cannot declare a parent result")
        else:
            self._claim_primary_result()

        columns: list[Column] = []
        for source in start.columns:
            if not source.HasField("declared_type"):
                raise ProtocolError("Execute columns require declared_type")
            try:
                validate_type_descriptor(source.declared_type)
            except CodecError as exc:
                raise ProtocolError("column has an invalid declared type") from exc
            self._validate_output_descriptor(source.declared_type)
            descriptor = value_pb2.TypeDescriptor()
            descriptor.CopyFrom(source.declared_type)
            columns.append(
                Column(
                    name=source.name,
                    data_type=source.data_type,
                    nullable=source.nullable,
                    precision=source.precision,
                    scale=source.scale,
                    declared_type=descriptor,
                )
            )
        self._active_result_id = start.result_id
        self._active_columns = tuple(columns)
        self._active_rows = 0
        return ResultSetStart(start.result_id, tuple(columns), start.role, parent)

    def _rows(self, batch: command_pb2.ResultRows) -> ResultRows:
        if batch.result_id != self._active_result_id:
            raise ProtocolError("row batch does not belong to the active result set")
        rows: list[tuple[Any, ...]] = []
        for source in batch.rows:
            if len(source.values) != len(self._active_columns):
                raise ProtocolError("row width differs from the result schema")
            try:
                row = tuple(
                    decode_value(value, column.declared_type)
                    for value, column in zip(source.values, self._active_columns)
                )
            except CodecError as exc:
                raise ProtocolError("result row violates its declared schema") from exc
            self._validate_output_values(source.values)
            rows.append(row)
        self._active_rows += len(rows)
        return ResultRows(batch.result_id, tuple(rows))

    def _update(self, update: command_pb2.UpdateResult) -> UpdateResult:
        if self._active_result_id is not None:
            raise ProtocolError("update result arrived inside an open result set")
        self._claim_result_id(update.result_id)
        self._claim_primary_result()
        if not update.counts:
            raise ProtocolError("update result must contain at least one count")
        counts: list[int | None] = []
        for count in update.counts:
            kind = count.WhichOneof("value")
            if kind == "affected_rows":
                counts.append(count.affected_rows)
            elif kind == "unknown":
                counts.append(None)
            else:
                raise ProtocolError("update count has no selected value")
        self._update_results.add(update.result_id)
        return UpdateResult(update.result_id, tuple(counts))

    def _end_result_set(self, end: command_pb2.ResultSetEnd) -> ResultSetEnd:
        if end.result_id != self._active_result_id:
            raise ProtocolError("result-set end does not match the active result")
        if end.row_count != self._active_rows:
            raise ProtocolError("result-set row count differs from streamed rows")
        self._active_result_id = None
        self._active_columns = ()
        self._active_rows = 0
        return ResultSetEnd(end.result_id, end.row_count)

    def _execution_end(self, end: command_pb2.ExecutionEnd) -> ExecutionEnd:
        if self._active_result_id is not None:
            raise ProtocolError("ExecutionEnd arrived before ResultSetEnd")
        if end.result_count != self._next_result_id - 1:
            raise ProtocolError("ExecutionEnd result_count does not match the stream")
        if not end.HasField("transaction"):
            raise ProtocolError("ExecutionEnd requires transaction status")
        transaction = _decode_transaction_status(end.transaction)

        # The terminal event is not success until the stream also ends with gRPC OK.
        try:
            extra = next(self._call)
        except StopIteration:
            self._complete = True
            self.transaction = transaction
            return ExecutionEnd(end.result_count, transaction)
        except grpc.RpcError as exc:
            raise translate_rpc_error(exc) from exc
        raise ProtocolError(
            f"event {extra.WhichOneof('event') or 'unknown'} arrived after ExecutionEnd"
        )

    def _claim_result_id(self, result_id: int) -> None:
        if result_id != self._next_result_id:
            raise ProtocolError(
                f"expected result_id {self._next_result_id}, received {result_id}"
            )
        self._next_result_id += 1

    def _claim_primary_result(self) -> None:
        self._primary_results += 1
        if self._primary_results > 1:
            self._require_accepted(features.MULTIPLE_RESULTS_V1)

    def _require_accepted(self, feature: str) -> None:
        if feature not in self._accepted_features:
            raise ProtocolError(f"server emitted {feature} without client acceptance")

    def _validate_output_descriptor(
        self,
        descriptor: value_pb2.TypeDescriptor,
    ) -> None:
        if descriptor.type not in self._supported_output_types:
            raise ProtocolError("server emitted a type absent from its capabilities")
        dimensions = descriptor_dimensions(descriptor)
        if dimensions:
            self._require_accepted(features.ARRAY_VALUES_V1)
            if not self._max_array_dimensions or dimensions > self._max_array_dimensions:
                raise ProtocolError("array dimensions exceed advertised capabilities")
            self._validate_output_descriptor(descriptor.element_type)

    def _validate_output_values(self, values: Sequence[value_pb2.Value]) -> None:
        for value in values:
            kind = value.WhichOneof("kind")
            if kind == "array_value":
                self._require_accepted(features.ARRAY_VALUES_V1)
                self._validate_output_values(value.array_value.elements)
            elif kind in ("geometry_with_crs", "geography_with_crs"):
                self._require_accepted(features.SPATIAL_VALUES_V1)
            elif kind == "lob_reference":
                self._require_accepted(features.LOB_READ_V1)


class LobReadStream(Iterator[bytes]):
    """Incremental, offset-validated LOB reader."""

    def __init__(
        self,
        call: Iterator[lob_pb2.ReadLobResponse],
        *,
        offset: int,
        length: int | None,
        max_chunk_bytes: int,
    ) -> None:
        self._call = call
        self._next_offset = offset
        self._length = length
        self._max_chunk_bytes = max_chunk_bytes
        self._read = 0
        self._complete = False
        self._closed = False

    @property
    def complete(self) -> bool:
        return self._complete

    def __iter__(self) -> "LobReadStream":
        return self

    def __next__(self) -> bytes:
        if self._complete or self._closed:
            raise StopIteration
        try:
            try:
                response = next(self._call)
            except StopIteration as exc:
                raise ProtocolError("LOB stream ended without end_of_read") from exc
            except grpc.RpcError as exc:
                raise translate_rpc_error(exc) from exc
            if response.offset != self._next_offset:
                raise ProtocolError("LOB chunks are not contiguous")
            if len(response.data) > self._max_chunk_bytes:
                raise ProtocolError("LOB chunk exceeds the advertised limit")
            self._next_offset += len(response.data)
            self._read += len(response.data)
            if self._length is not None and self._read > self._length:
                raise ProtocolError("LOB server returned more bytes than requested")
            if response.end_of_read:
                if self._length is not None and self._read != self._length:
                    raise ProtocolError("LOB range ended before the requested length")
                try:
                    extra = next(self._call)
                except StopIteration:
                    self._complete = True
                except grpc.RpcError as exc:
                    raise translate_rpc_error(exc) from exc
                else:
                    raise ProtocolError(
                        f"LOB chunk at offset {extra.offset} arrived after end_of_read"
                    )
            return response.data
        except ProtocolError:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._complete:
            cancel = getattr(self._call, "cancel", None)
            if cancel is not None:
                cancel()

    def __enter__(self) -> "LobReadStream":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class KublingGrpcClient:
    """Own a gRPC channel and create authenticated Kubling sessions."""

    def __init__(self, config: GrpcConfig, *, channel: grpc.Channel | None = None):
        self.config = config
        self._owns_channel = channel is None
        self._channel = channel or _create_channel(config)
        self._session_stub = command_pb2_grpc.SessionServiceStub(self._channel)
        self._query_stub = command_pb2_grpc.QueryServiceStub(self._channel)
        self._lob_stub = lob_pb2_grpc.LobServiceStub(self._channel)
        self._closed = False

    def wait_until_ready(self, timeout: float | None = None) -> None:
        self._ensure_open()
        try:
            grpc.channel_ready_future(self._channel).result(
                timeout=timeout or self.config.connect_timeout_seconds
            )
        except grpc.FutureTimeoutError as exc:
            raise KublingRpcError(
                _deadline_details("gRPC channel did not become ready before its deadline")
            ) from exc

    def ping(self, *, timeout: float | None = None) -> bool:
        self._ensure_open()
        try:
            response = self._session_stub.Ping(
                command_pb2.PingRequest(),
                timeout=timeout or self.config.rpc_timeout_seconds,
                wait_for_ready=self.config.wait_for_ready,
            )
        except grpc.RpcError as exc:
            raise translate_rpc_error(exc) from exc
        return response.ok

    def open_session(
        self,
        *,
        vdb_name: str,
        username: str,
        password: str,
        vdb_version: str = "",
        application_name: str = "kubling-sqlalchemy",
        properties: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> "KublingSession":
        self._ensure_open()
        if not vdb_name:
            raise ConfigurationError("vdb_name cannot be empty")
        self.wait_until_ready()
        request = command_pb2.LoginRequest(
            vdb_name=vdb_name,
            vdb_version=vdb_version,
            username=username,
            password=password,
            application_name=application_name,
            properties=dict(properties or {}),
        )
        try:
            response = self._session_stub.Login(
                request,
                timeout=timeout or self.config.rpc_timeout_seconds,
                wait_for_ready=self.config.wait_for_ready,
            )
        except grpc.RpcError as exc:
            raise translate_rpc_error(exc) from exc
        if not response.expiring_token or not response.session_id:
            raise ProtocolError("Login response requires token and session ID")
        session = KublingSession(
            config=self.config,
            login=response,
            session_stub=self._session_stub,
            query_stub=self._query_stub,
            lob_stub=self._lob_stub,
        )
        try:
            if response.vdb_name != vdb_name:
                raise ProtocolError("Login response returned a different VDB name")
            if vdb_version and response.vdb_version != vdb_version:
                raise ProtocolError("Login response returned a different VDB version")
            session.refresh_capabilities(timeout=timeout)
        except Exception:
            session._close_best_effort()
            raise
        return session

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_channel:
            self._channel.close()

    def __enter__(self) -> "KublingGrpcClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ProtocolError("gRPC client is closed")


class KublingSession:
    """An authenticated, capability-bound Kubling session."""

    def __init__(
        self,
        *,
        config: GrpcConfig,
        login: command_pb2.LoginResponse,
        session_stub: command_pb2_grpc.SessionServiceStub,
        query_stub: command_pb2_grpc.QueryServiceStub,
        lob_stub: lob_pb2_grpc.LobServiceStub,
    ) -> None:
        self._config = config
        self._login = command_pb2.LoginResponse()
        self._login.CopyFrom(login)
        self._session_stub = session_stub
        self._query_stub = query_stub
        self._lob_stub = lob_stub
        self._affinity = transaction_pb2.Affinity()
        if login.HasField("affinity"):
            self._affinity.CopyFrom(login.affinity)
        self._server_info: command_pb2.GetServerInfoResponse | None = None
        self._features: frozenset[str] = frozenset()
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._login.session_id

    @property
    def expires_at(self) -> int:
        return self._login.expires_at

    @property
    def vdb_name(self) -> str:
        return self._login.vdb_name

    @property
    def vdb_version(self) -> str:
        return self._login.vdb_version

    @property
    def server_info(self) -> command_pb2.GetServerInfoResponse:
        self._ensure_open()
        if self._server_info is None:
            raise ProtocolError("server capabilities have not been loaded")
        copied = command_pb2.GetServerInfoResponse()
        copied.CopyFrom(self._server_info)
        return copied

    @property
    def advertised_features(self) -> frozenset[str]:
        return self._features

    def refresh_capabilities(
        self,
        *,
        timeout: float | None = None,
    ) -> command_pb2.GetServerInfoResponse:
        self._ensure_open()
        request = command_pb2.GetServerInfoRequest(
            expiring_token=self._login.expiring_token
        )
        response = self._unary(
            self._query_stub.GetServerInfo,
            request,
            timeout=timeout,
        )
        self._validate_server_info(response)
        copied = command_pb2.GetServerInfoResponse()
        copied.CopyFrom(response)
        self._server_info = copied
        self._features = frozenset(response.features)
        self._affinity.CopyFrom(response.capabilities.affinity)
        return self.server_info

    def ping(self, *, timeout: float | None = None) -> bool:
        response = self._unary(
            self._session_stub.PingSession,
            command_pb2.SessionPingRequest(
                expiring_token=self._login.expiring_token,
                session_id=self.session_id,
            ),
            timeout=timeout,
        )
        return response.valid

    def execute(
        self,
        sql: str,
        parameters: Iterable[Any | BoundParameter | command_pb2.Parameter] = (),
        *,
        transaction_id: str = "",
        batch_size: int = 0,
        return_generated_keys: bool = False,
        accepted_features: Iterable[str] = (),
        timeout: float | None = None,
    ) -> ExecutionStream:
        self._ensure_capabilities()
        if not sql:
            raise ConfigurationError("SQL cannot be empty")
        accepted = frozenset(accepted_features)
        unknown_acceptance = accepted - _ACCEPTABLE_OUTPUT_FEATURES
        if unknown_acceptance:
            raise CapabilityError(
                f"unsupported accepted_features: {sorted(unknown_acceptance)!r}"
            )
        self._require_features(accepted)
        if return_generated_keys and features.GENERATED_KEYS_V1 not in accepted:
            raise CapabilityError(
                "generated keys require explicit generated_keys_v1 acceptance"
            )

        encoded = tuple(self._encode_parameters(parameters))
        required = set()
        for parameter in encoded:
            required.update(required_features_for_parameter(parameter))
        if transaction_id:
            required.add(features.TRANSACTION_IDS_V1)
        self._require_features(required)
        self._validate_input_parameters(encoded)

        capabilities = self._capabilities
        if batch_size < 0:
            raise ConfigurationError("batch_size cannot be negative")
        if batch_size and batch_size > capabilities.limits.max_rows_per_batch:
            raise CapabilityError("batch_size exceeds the advertised row limit")
        request = command_pb2.ExecuteRequest(
            expiring_token=self._login.expiring_token,
            sql=sql,
            params=encoded,
            transaction_id=transaction_id,
            batch_size=batch_size,
            return_generated_keys=return_generated_keys,
            capability_id=capabilities.capability_id,
            accepted_features=sorted(accepted),
        )
        request_limit = min(
            capabilities.limits.max_request_message_bytes,
            self._config.max_send_message_bytes,
        )
        if len(request.SerializeToString()) > request_limit:
            raise CapabilityError("Execute request exceeds the advertised message limit")
        try:
            call = self._query_stub.Execute(
                request,
                timeout=timeout or self._config.rpc_timeout_seconds,
                metadata=self._metadata,
                wait_for_ready=self._config.wait_for_ready,
            )
        except grpc.RpcError as exc:
            raise translate_rpc_error(exc) from exc
        return ExecutionStream(
            iter(call),
            accepted_features=accepted,
            supported_output_types=self._supported_types(output=True),
            max_array_dimensions=capabilities.limits.max_array_dimensions,
            max_response_message_bytes=min(
                capabilities.limits.max_response_message_bytes,
                self._config.max_receive_message_bytes,
            ),
            max_batch_bytes=capabilities.limits.max_batch_bytes,
            max_rows_per_batch=capabilities.limits.max_rows_per_batch,
        )

    def begin_transaction(self, *, timeout: float | None = None) -> str:
        self._require_features({features.TRANSACTION_IDS_V1})
        response = self._unary(
            self._query_stub.BeginTransaction,
            command_pb2.BeginTransactionRequest(
                expiring_token=self._login.expiring_token
            ),
            timeout=timeout,
        )
        if not response.transaction_id:
            raise ProtocolError("BeginTransaction did not return a transaction ID")
        if response.HasField("affinity"):
            self._affinity.CopyFrom(response.affinity)
        return response.transaction_id

    def commit_transaction(
        self,
        transaction_id: str,
        *,
        timeout: float | None = None,
    ) -> TransactionStatus:
        if not transaction_id:
            raise ConfigurationError("commit requires a transaction ID")
        self._require_features({features.TRANSACTION_IDS_V1})
        response = self._unary(
            self._query_stub.CommitTransaction,
            command_pb2.CommitTransactionRequest(
                expiring_token=self._login.expiring_token,
                transaction_id=transaction_id,
            ),
            timeout=timeout,
        )
        if not response.success or not response.HasField("transaction"):
            raise ProtocolError("CommitTransaction did not report a successful status")
        return _decode_transaction_status(response.transaction)

    def rollback_transaction(
        self,
        transaction_id: str,
        *,
        timeout: float | None = None,
    ) -> TransactionStatus:
        if not transaction_id:
            raise ConfigurationError("rollback requires a transaction ID")
        self._require_features({features.TRANSACTION_IDS_V1})
        response = self._unary(
            self._query_stub.RollbackTransaction,
            command_pb2.RollbackTransactionRequest(
                expiring_token=self._login.expiring_token,
                transaction_id=transaction_id,
            ),
            timeout=timeout,
        )
        if not response.success or not response.HasField("transaction"):
            raise ProtocolError("RollbackTransaction did not report a successful status")
        return _decode_transaction_status(response.transaction)

    def get_transaction_status(
        self,
        transaction_id: str,
        *,
        timeout: float | None = None,
    ) -> TransactionStatus:
        if not transaction_id:
            raise ConfigurationError("transaction status requires an ID")
        self._require_features(
            {features.TRANSACTION_IDS_V1, features.TRANSACTION_STATUS_V1}
        )
        response = self._unary(
            self._query_stub.GetTransactionStatus,
            command_pb2.GetTransactionStatusRequest(
                expiring_token=self._login.expiring_token,
                transaction_id=transaction_id,
            ),
            timeout=timeout,
        )
        if not response.HasField("transaction"):
            raise ProtocolError("GetTransactionStatus omitted transaction status")
        return _decode_transaction_status(response.transaction)

    def read_lob(
        self,
        reference: KublingLobReference,
        *,
        offset: int = 0,
        length: int | None = None,
        max_chunk_bytes: int = 0,
        timeout: float | None = None,
    ) -> LobReadStream:
        self._require_features({features.LOB_READ_V1})
        self._validate_lob_reference(reference)
        if offset < 0 or length is not None and length < 0:
            raise ConfigurationError("LOB offset and length cannot be negative")
        if reference.size_bytes is not None:
            if offset > reference.size_bytes:
                raise ConfigurationError("LOB offset exceeds the referenced size")
            if length is not None and offset + length > reference.size_bytes:
                raise ConfigurationError("LOB range exceeds the referenced size")
        maximum = self._capabilities.limits.max_lob_chunk_bytes
        if max_chunk_bytes < 0 or max_chunk_bytes > maximum:
            raise CapabilityError("LOB chunk size exceeds the advertised limit")
        request = lob_pb2.ReadLobRequest(
            expiring_token=self._login.expiring_token,
            lob_id=reference.lob_id,
            offset=offset,
            max_chunk_bytes=max_chunk_bytes,
            capability_id=self._capabilities.capability_id,
        )
        if length is not None:
            request.length = length
        try:
            call = self._lob_stub.ReadLob(
                request,
                timeout=timeout or self._config.rpc_timeout_seconds,
                metadata=self._metadata,
                wait_for_ready=self._config.wait_for_ready,
            )
        except grpc.RpcError as exc:
            raise translate_rpc_error(exc) from exc
        return LobReadStream(
            iter(call),
            offset=offset,
            length=length,
            max_chunk_bytes=max_chunk_bytes or maximum,
        )

    def write_lob(
        self,
        data: bytes | str | Iterable[bytes],
        *,
        lob_type: int | None = None,
        size_bytes: int | None = None,
        timeout: float | None = None,
    ) -> KublingLobReference:
        self._require_features({features.LOB_READ_V1, features.LOB_WRITE_V1})
        if isinstance(data, str):
            if lob_type not in (None, value_pb2.VALUE_TYPE_CLOB):
                raise ConfigurationError("text LOB data requires CLOB")
            lob_type = value_pb2.VALUE_TYPE_CLOB
            payload: Iterable[bytes] = (data.encode("utf-8"),)
        elif isinstance(data, bytes):
            if lob_type not in (None, value_pb2.VALUE_TYPE_BLOB):
                raise ConfigurationError("byte LOB data requires BLOB")
            lob_type = value_pb2.VALUE_TYPE_BLOB
            payload = (data,)
        else:
            if lob_type not in (value_pb2.VALUE_TYPE_BLOB, value_pb2.VALUE_TYPE_CLOB):
                raise ConfigurationError("streaming LOB data requires BLOB or CLOB type")
            payload = data
        if size_bytes is None and isinstance(data, (bytes, str)):
            size_bytes = len(next(iter(payload)))
            payload = (data.encode("utf-8"),) if isinstance(data, str) else (data,)
        if size_bytes is not None and size_bytes < 0:
            raise ConfigurationError("LOB size cannot be negative")
        maximum = self._capabilities.limits.max_lob_bytes
        if size_bytes is not None and size_bytes > maximum:
            raise CapabilityError("LOB exceeds the advertised size limit")

        requests = self._lob_write_requests(payload, lob_type, size_bytes)
        try:
            response = self._lob_stub.WriteLob(
                requests,
                timeout=timeout or self._config.rpc_timeout_seconds,
                metadata=self._metadata,
                wait_for_ready=self._config.wait_for_ready,
            )
        except grpc.RpcError as exc:
            raise translate_rpc_error(exc) from exc
        if not response.HasField("reference"):
            raise ProtocolError("WriteLob response omitted its reference")
        reference = _decode_lob_reference(response.reference)
        self._validate_lob_reference(reference)
        if reference.type != lob_type:
            raise ProtocolError("WriteLob returned a different LOB type")
        return reference

    def release_lob(
        self,
        reference: KublingLobReference,
        *,
        timeout: float | None = None,
    ) -> None:
        self._require_features({features.LOB_READ_V1})
        self._validate_lob_reference(reference)
        self._unary(
            self._lob_stub.ReleaseLob,
            lob_pb2.ReleaseLobRequest(
                expiring_token=self._login.expiring_token,
                lob_id=reference.lob_id,
                capability_id=self._capabilities.capability_id,
            ),
            timeout=timeout,
        )

    def close(self, *, timeout: float | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            response = self._session_stub.Logout(
                command_pb2.LogoutRequest(expiring_token=self._login.expiring_token),
                timeout=timeout or self._config.rpc_timeout_seconds,
                metadata=self._metadata,
                wait_for_ready=self._config.wait_for_ready,
            )
        except grpc.RpcError as exc:
            raise translate_rpc_error(exc) from exc
        finally:
            self._login.ClearField("expiring_token")
        if not response.success:
            raise ProtocolError("Logout did not confirm session closure")

    def __enter__(self) -> "KublingSession":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @property
    def _capabilities(self) -> capability_pb2.Capabilities:
        self._ensure_capabilities()
        return self._server_info.capabilities  # type: ignore[union-attr]

    @property
    def _metadata(self) -> tuple[tuple[str, str], ...]:
        if self._affinity.routing_token:
            return (("kubling-affinity", self._affinity.routing_token),)
        return ()

    def _unary(self, rpc, request, *, timeout: float | None):
        self._ensure_open()
        try:
            return rpc(
                request,
                timeout=timeout or self._config.rpc_timeout_seconds,
                metadata=self._metadata,
                wait_for_ready=self._config.wait_for_ready,
            )
        except grpc.RpcError as exc:
            raise translate_rpc_error(exc) from exc

    def _validate_server_info(self, response: command_pb2.GetServerInfoResponse) -> None:
        advertised = frozenset(response.features)
        if features.GENERIC_EXECUTE_V1 not in advertised:
            raise CapabilityError("server does not advertise generic_execute_v1")
        if not response.HasField("capabilities"):
            raise CapabilityError("server omitted authenticated capabilities")
        capabilities = response.capabilities
        if not capabilities.HasField("protocol_version"):
            raise CapabilityError("server omitted its protocol version")
        version = capabilities.protocol_version
        if version.major != 1 or version.minor < 1:
            raise CapabilityError(
                f"unsupported protocol version {version.major}.{version.minor}"
            )
        if not capabilities.capability_id:
            raise CapabilityError("server omitted capability_id")
        if not capabilities.HasField("limits"):
            raise CapabilityError("server omitted protocol limits")
        limits = capabilities.limits
        required_limits = (
            limits.max_request_message_bytes,
            limits.max_response_message_bytes,
            limits.max_rows_per_batch,
            limits.max_batch_bytes,
        )
        if any(limit <= 0 for limit in required_limits):
            raise CapabilityError("generic_execute_v1 requires positive protocol limits")
        if not capabilities.HasField("affinity"):
            raise CapabilityError("server omitted effective affinity")
        self._validate_affinity(capabilities)
        if features.ARRAY_VALUES_V1 in advertised and limits.max_array_dimensions <= 0:
            raise CapabilityError("array_values_v1 requires a positive dimension limit")
        if advertised.intersection({features.LOB_READ_V1, features.LOB_WRITE_V1}):
            if limits.max_lob_chunk_bytes <= 0 or limits.max_lob_bytes <= 0:
                raise CapabilityError("LOB features require positive size limits")
        if not capabilities.supported_types:
            raise CapabilityError("server omitted supported logical types")

    def _validate_affinity(self, capabilities: capability_pb2.Capabilities) -> None:
        requirement = capabilities.affinity_requirement
        affinity = capabilities.affinity
        if requirement == transaction_pb2.AFFINITY_REQUIREMENT_UNSPECIFIED:
            raise CapabilityError("server affinity requirement is unspecified")
        needs_session = requirement in (
            transaction_pb2.AFFINITY_REQUIREMENT_SESSION,
            transaction_pb2.AFFINITY_REQUIREMENT_SESSION_AND_NODE,
        )
        needs_node = requirement in (
            transaction_pb2.AFFINITY_REQUIREMENT_NODE,
            transaction_pb2.AFFINITY_REQUIREMENT_SESSION_AND_NODE,
        )
        if needs_session and affinity.session_id != self.session_id:
            raise CapabilityError("capability affinity does not match the session")
        if needs_node and not affinity.node_id:
            raise CapabilityError("capability affinity omitted its node ID")

    def _supported_types(self, *, output: bool) -> frozenset[int]:
        return frozenset(
            item.type
            for item in self._capabilities.supported_types
            if (item.output if output else item.input)
        )

    def _validate_input_parameters(
        self,
        parameters: Sequence[command_pb2.Parameter],
    ) -> None:
        supported = self._supported_types(output=False)
        max_dimensions = self._capabilities.limits.max_array_dimensions
        for parameter in parameters:
            if not parameter.HasField("value"):
                raise CodecError("parameter has no value")
            if parameter.HasField("declared_type"):
                self._validate_input_descriptor(
                    parameter.declared_type,
                    supported,
                    max_dimensions,
                )
            else:
                inferred = infer_type_descriptor(parameter.value)
                if inferred is not None:
                    self._validate_input_descriptor(
                        inferred,
                        supported,
                        max_dimensions,
                    )

    def _validate_input_descriptor(
        self,
        descriptor: value_pb2.TypeDescriptor,
        supported: frozenset[int],
        max_dimensions: int,
    ) -> None:
        if descriptor.type not in supported:
            raise CapabilityError("declared parameter type is not supported for input")
        dimensions = descriptor_dimensions(descriptor)
        if dimensions:
            if not max_dimensions or dimensions > max_dimensions:
                raise CapabilityError("array dimensions exceed advertised capabilities")
            self._validate_input_descriptor(
                descriptor.element_type,
                supported,
                max_dimensions,
            )

    def _require_features(self, required: Iterable[str]) -> None:
        self._ensure_capabilities()
        missing = frozenset(required) - self._features
        if missing:
            raise CapabilityError(f"server does not advertise {sorted(missing)!r}")

    def _encode_parameters(
        self,
        parameters: Iterable[Any | BoundParameter | command_pb2.Parameter],
    ) -> Iterator[command_pb2.Parameter]:
        for value in parameters:
            if isinstance(value, command_pb2.Parameter):
                copied = command_pb2.Parameter()
                copied.CopyFrom(value)
                yield copied
            else:
                yield encode_parameter(value)

    def _lob_write_requests(
        self,
        payload: Iterable[bytes],
        lob_type: int,
        size_bytes: int | None,
    ) -> Iterator[lob_pb2.WriteLobRequest]:
        start = lob_pb2.LobWriteStart(
            expiring_token=self._login.expiring_token,
            type=lob_type,
            capability_id=self._capabilities.capability_id,
        )
        if size_bytes is not None:
            start.size_bytes = size_bytes
        yield lob_pb2.WriteLobRequest(start=start)

        maximum_chunk = self._capabilities.limits.max_lob_chunk_bytes
        maximum_size = self._capabilities.limits.max_lob_bytes
        offset = 0
        utf8_decoder = (
            codecs.getincrementaldecoder("utf-8")(errors="strict")
            if lob_type == value_pb2.VALUE_TYPE_CLOB
            else None
        )
        for source in payload:
            if not isinstance(source, bytes):
                raise CodecError("LOB streams must yield bytes")
            if utf8_decoder is not None:
                try:
                    utf8_decoder.decode(source, final=False)
                except UnicodeDecodeError as exc:
                    raise CodecError("CLOB stream must contain valid UTF-8") from exc
            for begin in range(0, len(source), maximum_chunk):
                chunk = source[begin : begin + maximum_chunk]
                if offset + len(chunk) > maximum_size:
                    raise CapabilityError("LOB exceeds the advertised size limit")
                yield lob_pb2.WriteLobRequest(
                    chunk=lob_pb2.LobChunk(offset=offset, data=chunk)
                )
                offset += len(chunk)
        if utf8_decoder is not None:
            try:
                utf8_decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise CodecError("CLOB stream must contain complete UTF-8") from exc
        if size_bytes is not None and offset != size_bytes:
            raise CodecError("LOB stream length differs from declared size_bytes")

    def _validate_lob_reference(self, reference: KublingLobReference) -> None:
        if reference.session_id != self.session_id:
            raise CapabilityError("LOB reference belongs to a different session")

    def _ensure_capabilities(self) -> None:
        self._ensure_open()
        if self._server_info is None:
            raise ProtocolError("server capabilities have not been loaded")

    def _ensure_open(self) -> None:
        if self._closed:
            raise ProtocolError("Kubling session is closed")

    def _close_best_effort(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _create_channel(config: GrpcConfig) -> grpc.Channel:
    options = [
        ("grpc.max_send_message_length", config.max_send_message_bytes),
        ("grpc.max_receive_message_length", config.max_receive_message_bytes),
        *config.channel_options,
    ]
    if config.tls is None:
        return grpc.insecure_channel(config.endpoint, options=options)
    if config.tls.server_name_override is not None:
        options.extend(
            (
                ("grpc.ssl_target_name_override", config.tls.server_name_override),
                ("grpc.default_authority", config.tls.server_name_override),
            )
        )
    credentials = grpc.ssl_channel_credentials(
        root_certificates=config.tls.root_certificates,
        private_key=config.tls.private_key,
        certificate_chain=config.tls.certificate_chain,
    )
    return grpc.secure_channel(config.endpoint, credentials, options=options)


def _decode_transaction_status(
    status: transaction_pb2.TransactionStatus,
) -> TransactionStatus:
    state = status.state
    transaction_id = status.transaction_id
    if state == transaction_pb2.TRANSACTION_STATE_NONE and transaction_id:
        raise ProtocolError("NONE transaction status must have an empty ID")
    if state in (
        transaction_pb2.TRANSACTION_STATE_ACTIVE,
        transaction_pb2.TRANSACTION_STATE_ROLLBACK_ONLY,
        transaction_pb2.TRANSACTION_STATE_COMMITTED,
        transaction_pb2.TRANSACTION_STATE_ROLLED_BACK,
    ) and not transaction_id:
        raise ProtocolError("transaction outcome requires a transaction ID")
    return TransactionStatus(transaction_id, state)


def _decode_lob_reference(reference: value_pb2.LobReference) -> KublingLobReference:
    return KublingLobReference(
        lob_id=reference.lob_id,
        type=reference.type,
        session_id=reference.session_id,
        size_bytes=reference.size_bytes if reference.HasField("size_bytes") else None,
        expires_at_unix_ms=(
            reference.expires_at_unix_ms
            if reference.HasField("expires_at_unix_ms")
            else None
        ),
    )


def _deadline_details(message: str):
    from .errors import RpcErrorDetails

    return RpcErrorDetails(grpc_code=grpc.StatusCode.DEADLINE_EXCEEDED, message=message)


__all__ = [
    "Column",
    "ExecutionEnd",
    "ExecutionEvent",
    "ExecutionStream",
    "GrpcConfig",
    "KublingGrpcClient",
    "KublingSession",
    "LobReadStream",
    "ResultRows",
    "ResultSetEnd",
    "ResultSetStart",
    "TlsConfig",
    "TransactionStatus",
    "UpdateResult",
]
