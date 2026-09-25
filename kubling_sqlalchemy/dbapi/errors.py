"""PEP 249 exceptions and translation from the native Kubling transport."""

from __future__ import annotations

from typing import TypeVar

import grpc
from kubling.v1 import error_pb2

from kubling_sqlalchemy.transport import (
    CapabilityError as TransportCapabilityError,
    CodecError as TransportCodecError,
    ConfigurationError as TransportConfigurationError,
    KublingRpcError,
    ProtocolError as TransportProtocolError,
    RpcErrorDetails,
)


class Warning(Exception):
    """Important warning raised by the driver."""


class Error(Exception):
    """Base class for all DB-API errors."""

    def __init__(
        self,
        message: str,
        *,
        details: RpcErrorDetails | None = None,
    ) -> None:
        super().__init__(message)
        self.details = details
        self.grpc_code = details.grpc_code if details is not None else None
        self.stable_code = details.stable_code if details is not None else None
        self.sqlstate = details.sql_state if details is not None else None
        self.vendor_code = details.vendor_code if details is not None else None
        self.retryability = details.retryability if details is not None else None
        self.transaction_id = details.transaction_id if details is not None else None
        self.transaction_state = (
            details.transaction_state if details is not None else None
        )
        self.sql_executed = details.sql_executed if details is not None else None


class InterfaceError(Error):
    """The local DB-API interface was used incorrectly or is unavailable."""


class DatabaseError(Error):
    """Base class for database and transport failures."""


class DataError(DatabaseError):
    """A value cannot be represented or processed."""


class OperationalError(DatabaseError):
    """An operational or connectivity failure occurred."""


class IntegrityError(DatabaseError):
    """A relational constraint was violated."""


class InternalError(DatabaseError):
    """The server or protocol returned an invalid internal result."""


class ProgrammingError(DatabaseError):
    """SQL or DB-API input is invalid."""


class NotSupportedError(DatabaseError):
    """The requested capability is not supported."""


_ErrorType = TypeVar("_ErrorType", bound=Error)


def make_error(
    error_type: type[_ErrorType],
    message: str,
    *,
    details: RpcErrorDetails | None = None,
) -> _ErrorType:
    return error_type(message, details=details)


def translate_exception(error: Exception) -> Error:
    """Map a transport failure without losing machine-readable details."""

    if isinstance(error, Error):
        return error
    if isinstance(error, KublingRpcError):
        error_type = _rpc_error_type(error.details)
        return error_type(str(error), details=error.details)
    if isinstance(error, TransportCodecError):
        return DataError(str(error))
    if isinstance(error, TransportCapabilityError):
        return NotSupportedError(str(error))
    if isinstance(error, TransportProtocolError):
        return InternalError(str(error))
    if isinstance(error, TransportConfigurationError):
        return InterfaceError(str(error))
    return InterfaceError(str(error))


def _rpc_error_type(details: RpcErrorDetails) -> type[DatabaseError]:
    sqlstate = details.sql_state or ""
    if sqlstate.startswith("22"):
        return DataError
    if sqlstate.startswith("23"):
        return IntegrityError
    if sqlstate.startswith("0A"):
        return NotSupportedError
    if sqlstate.startswith(("07", "2A", "34", "3D", "3F", "42")):
        return ProgrammingError
    if sqlstate.startswith(("08", "40", "53", "54", "55", "57", "58")):
        return OperationalError

    categories: dict[int, type[DatabaseError]] = {
        error_pb2.ERROR_CATEGORY_AUTHENTICATION: OperationalError,
        error_pb2.ERROR_CATEGORY_AUTHORIZATION: OperationalError,
        error_pb2.ERROR_CATEGORY_VALIDATION: DataError,
        error_pb2.ERROR_CATEGORY_SQL: ProgrammingError,
        error_pb2.ERROR_CATEGORY_CONSTRAINT: IntegrityError,
        error_pb2.ERROR_CATEGORY_UNSUPPORTED: NotSupportedError,
        error_pb2.ERROR_CATEGORY_RESOURCE: OperationalError,
        error_pb2.ERROR_CATEGORY_TRANSIENT: OperationalError,
        error_pb2.ERROR_CATEGORY_INTERNAL: InternalError,
    }
    if details.category in categories:
        return categories[details.category]

    grpc_types: dict[grpc.StatusCode, type[DatabaseError]] = {
        grpc.StatusCode.CANCELLED: OperationalError,
        grpc.StatusCode.UNKNOWN: InternalError,
        grpc.StatusCode.INVALID_ARGUMENT: ProgrammingError,
        grpc.StatusCode.DEADLINE_EXCEEDED: OperationalError,
        grpc.StatusCode.NOT_FOUND: OperationalError,
        grpc.StatusCode.ALREADY_EXISTS: IntegrityError,
        grpc.StatusCode.PERMISSION_DENIED: OperationalError,
        grpc.StatusCode.RESOURCE_EXHAUSTED: OperationalError,
        grpc.StatusCode.FAILED_PRECONDITION: OperationalError,
        grpc.StatusCode.ABORTED: OperationalError,
        grpc.StatusCode.OUT_OF_RANGE: DataError,
        grpc.StatusCode.UNIMPLEMENTED: NotSupportedError,
        grpc.StatusCode.INTERNAL: InternalError,
        grpc.StatusCode.UNAVAILABLE: OperationalError,
        grpc.StatusCode.DATA_LOSS: InternalError,
        grpc.StatusCode.UNAUTHENTICATED: OperationalError,
    }
    return grpc_types.get(details.grpc_code, DatabaseError)


__all__ = [
    "DataError",
    "DatabaseError",
    "Error",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "NotSupportedError",
    "OperationalError",
    "ProgrammingError",
    "Warning",
    "make_error",
    "translate_exception",
]
