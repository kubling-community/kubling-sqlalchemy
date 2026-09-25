"""Kubling's synchronous PEP 249 interface over the native gRPC transport."""

from __future__ import annotations

from datetime import date, datetime, time
from time import localtime

from kubling.v1 import value_pb2

from kubling_sqlalchemy.transport import GrpcConfig, KublingGrpcClient, TlsConfig

from .connection import Connection
from .cursor import ColumnDescription, Cursor
from .errors import (
    DataError,
    DatabaseError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    Warning,
    translate_exception,
)


apilevel = "2.0"
threadsafety = 1
paramstyle = "qmark"


class DBAPITypeObject:
    """Compare a DB-API category with Kubling logical type codes."""

    def __init__(self, *values: int) -> None:
        self.values = frozenset(values)

    def __eq__(self, other) -> bool:
        return other in self.values

    def __ne__(self, other) -> bool:
        return other not in self.values


STRING = DBAPITypeObject(
    value_pb2.VALUE_TYPE_STRING,
    value_pb2.VALUE_TYPE_CHAR,
    value_pb2.VALUE_TYPE_CLOB,
    value_pb2.VALUE_TYPE_XML,
    value_pb2.VALUE_TYPE_JSON,
)
BINARY = DBAPITypeObject(
    value_pb2.VALUE_TYPE_VARBINARY,
    value_pb2.VALUE_TYPE_BLOB,
    value_pb2.VALUE_TYPE_GEOMETRY,
    value_pb2.VALUE_TYPE_GEOGRAPHY,
)
NUMBER = DBAPITypeObject(
    value_pb2.VALUE_TYPE_BYTE,
    value_pb2.VALUE_TYPE_SHORT,
    value_pb2.VALUE_TYPE_INTEGER,
    value_pb2.VALUE_TYPE_LONG,
    value_pb2.VALUE_TYPE_BIGINTEGER,
    value_pb2.VALUE_TYPE_FLOAT,
    value_pb2.VALUE_TYPE_DOUBLE,
    value_pb2.VALUE_TYPE_BIGDECIMAL,
)
DATETIME = DBAPITypeObject(
    value_pb2.VALUE_TYPE_DATE,
    value_pb2.VALUE_TYPE_TIME,
    value_pb2.VALUE_TYPE_TIMESTAMP,
)
ROWID = DBAPITypeObject()


def connect(
    endpoint: str,
    *,
    vdb_name: str,
    username: str,
    password: str,
    vdb_version: str = "",
    tls: TlsConfig | None = TlsConfig(),
    connect_timeout_seconds: float = 10.0,
    rpc_timeout_seconds: float = 30.0,
    wait_for_ready: bool = True,
    max_send_message_bytes: int = 16 * 1024 * 1024,
    max_receive_message_bytes: int = 64 * 1024 * 1024,
    channel_options: tuple[tuple[str, str | int], ...] = (),
    application_name: str = "kubling-dbapi",
    properties: dict[str, str] | None = None,
    autocommit: bool = False,
) -> Connection:
    """Open one authenticated Kubling session and return its DB-API connection."""

    if not isinstance(endpoint, str) or not endpoint.strip():
        raise InterfaceError("endpoint must be a non-empty string")
    if not isinstance(vdb_name, str) or not vdb_name:
        raise InterfaceError("vdb_name must be a non-empty string")
    if type(autocommit) is not bool:
        raise InterfaceError("autocommit must be a bool")
    try:
        config = GrpcConfig(
            endpoint=endpoint,
            tls=tls,
            connect_timeout_seconds=connect_timeout_seconds,
            rpc_timeout_seconds=rpc_timeout_seconds,
            wait_for_ready=wait_for_ready,
            max_send_message_bytes=max_send_message_bytes,
            max_receive_message_bytes=max_receive_message_bytes,
            channel_options=channel_options,
        )
        client = KublingGrpcClient(config)
    except Exception as exc:
        raise translate_exception(exc) from exc
    try:
        session = client.open_session(
            vdb_name=vdb_name,
            vdb_version=vdb_version,
            username=username,
            password=password,
            application_name=application_name,
            properties=properties,
        )
    except Exception as exc:
        client.close()
        raise translate_exception(exc) from exc
    return Connection(client, session, autocommit=autocommit)


def Date(year: int, month: int, day: int) -> date:
    return date(year, month, day)


def Time(hour: int, minute: int, second: int) -> time:
    return time(hour, minute, second)


def Timestamp(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int,
) -> datetime:
    return datetime(year, month, day, hour, minute, second)


def DateFromTicks(ticks: float) -> date:
    return Date(*localtime(ticks)[:3])


def TimeFromTicks(ticks: float) -> time:
    return Time(*localtime(ticks)[3:6])


def TimestampFromTicks(ticks: float) -> datetime:
    return Timestamp(*localtime(ticks)[:6])


def Binary(value) -> bytes:
    return bytes(value)


__all__ = [
    "Binary",
    "BINARY",
    "ColumnDescription",
    "Connection",
    "Cursor",
    "DataError",
    "DATETIME",
    "DatabaseError",
    "Date",
    "DateFromTicks",
    "Error",
    "IntegrityError",
    "InterfaceError",
    "InternalError",
    "NotSupportedError",
    "NUMBER",
    "OperationalError",
    "ProgrammingError",
    "ROWID",
    "STRING",
    "Time",
    "TimeFromTicks",
    "Timestamp",
    "TimestampFromTicks",
    "Warning",
    "DBAPITypeObject",
    "apilevel",
    "connect",
    "paramstyle",
    "threadsafety",
]
