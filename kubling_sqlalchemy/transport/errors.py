"""Errors raised by the Kubling gRPC transport."""

from __future__ import annotations

from dataclasses import dataclass

import grpc
from grpc_status import rpc_status
from kubling.v1 import error_pb2


class KublingClientError(Exception):
    """Base class for failures detected by the local gRPC client."""


class ConfigurationError(KublingClientError, ValueError):
    """The client configuration is internally inconsistent."""


class CapabilityError(KublingClientError):
    """The server does not advertise a guarantee required by an operation."""


class ProtocolError(KublingClientError):
    """A successful transport delivered an invalid protocol sequence."""


class CodecError(KublingClientError, ValueError):
    """A Python value cannot be represented by the declared Kubling type."""


@dataclass(frozen=True, slots=True)
class RpcErrorDetails:
    """Machine-readable information retained from a failed RPC."""

    grpc_code: grpc.StatusCode | None
    message: str
    stable_code: str | None = None
    sql_state: str | None = None
    vendor_code: int | None = None
    category: int | None = None
    retryability: int | None = None
    transaction_id: str | None = None
    transaction_state: int | None = None
    sql_executed: bool | None = None


class KublingRpcError(KublingClientError):
    """A gRPC failure with optional structured Kubling details."""

    def __init__(self, details: RpcErrorDetails):
        self.details = details
        code = details.grpc_code.name if details.grpc_code is not None else "UNKNOWN"
        stable = f" [{details.stable_code}]" if details.stable_code else ""
        super().__init__(f"Kubling RPC failed with {code}{stable}: {details.message}")

    @property
    def retryability(self) -> int | None:
        """Return the server classification without interpreting it as permission."""

        return self.details.retryability


def rpc_error_details(error: grpc.RpcError) -> RpcErrorDetails:
    """Extract ``KublingError`` from ``google.rpc.Status`` when it is present."""

    grpc_code = _safe_call(error, "code")
    message = _safe_call(error, "details") or str(error)
    try:
        status = rpc_status.from_call(error)
    except (TypeError, ValueError):
        status = None

    if status is None:
        return RpcErrorDetails(grpc_code=grpc_code, message=message)

    for detail in status.details:
        if not detail.Is(error_pb2.KublingError.DESCRIPTOR):
            continue
        kubling_error = error_pb2.KublingError()
        if not detail.Unpack(kubling_error):
            continue
        transaction_id = None
        transaction_state = None
        if kubling_error.HasField("transaction"):
            transaction_id = kubling_error.transaction.transaction_id or None
            transaction_state = kubling_error.transaction.state
        return RpcErrorDetails(
            grpc_code=grpc_code,
            message=status.message or message,
            stable_code=kubling_error.stable_code or None,
            sql_state=(
                kubling_error.sql_state
                if kubling_error.HasField("sql_state")
                else None
            ),
            vendor_code=(
                kubling_error.vendor_code
                if kubling_error.HasField("vendor_code")
                else None
            ),
            category=kubling_error.category,
            retryability=kubling_error.retryability,
            transaction_id=transaction_id,
            transaction_state=transaction_state,
            sql_executed=(
                kubling_error.sql_executed
                if kubling_error.HasField("sql_executed")
                else None
            ),
        )

    return RpcErrorDetails(grpc_code=grpc_code, message=status.message or message)


def translate_rpc_error(error: grpc.RpcError) -> KublingRpcError:
    """Translate a transport exception without guessing retry or SQL outcome."""

    return KublingRpcError(rpc_error_details(error))


def _safe_call(error: grpc.RpcError, name: str):
    method = getattr(error, name, None)
    if method is None:
        return None
    try:
        return method()
    except Exception:  # pragma: no cover - defensive against third-party errors
        return None


__all__ = [
    "CapabilityError",
    "CodecError",
    "ConfigurationError",
    "KublingClientError",
    "KublingRpcError",
    "ProtocolError",
    "RpcErrorDetails",
    "rpc_error_details",
    "translate_rpc_error",
]
