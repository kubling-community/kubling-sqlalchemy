"""Native SQLAlchemy dialect for Kubling's client gRPC transport."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import grpc
from sqlalchemy import exc
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.sql.compiler import (
    DDLCompiler,
    GenericTypeCompiler,
    IdentifierPreparer,
    SQLCompiler,
)
from sqlalchemy.types import (
    BigInteger,
    Boolean,
    CHAR,
    Date,
    DateTime,
    Float,
    Integer,
    JSON,
    LargeBinary,
    Numeric,
    PickleType,
    SmallInteger,
    String,
    Text,
    Time,
    UserDefinedType,
)

from . import dbapi as kubling_dbapi
from .reflection import KublingReflectionMixin
from .transport import TlsConfig


class Geography(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "GEOGRAPHY"

    @property
    def geometry_type(self):
        return "GEOGRAPHY"

    @property
    def srid(self):
        return 4326


class Geometry(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "GEOMETRY"

    @property
    def geometry_type(self):
        return "GEOMETRY"

    @property
    def srid(self):
        return 4326


class XML(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "XML"


KUBLING_TYPE_MAP = {
    "string": String,
    "bigdecimal": Numeric,
    "biginteger": BigInteger,
    "blob": LargeBinary,
    "boolean": Boolean,
    "byte": SmallInteger,
    "char": lambda length=1: CHAR(length),
    "clob": Text,
    "date": Date,
    "double": Float,
    "float": Float,
    "geography": Geography,
    "geometry": Geometry,
    "integer": Integer,
    "json": JSON,
    "long": BigInteger,
    "object": PickleType,
    "short": SmallInteger,
    "time": Time,
    "timestamp": DateTime,
    "varbinary": LargeBinary,
    "xml": XML,
}


def map_kubling_type(kubling_type: str, **kwargs):
    try:
        type_def = KUBLING_TYPE_MAP[kubling_type.lower()]
    except (AttributeError, KeyError) as error:
        raise ValueError(f"Unsupported Kubling type: {kubling_type}") from error
    return type_def(**kwargs)


class KublingTypeCompiler(GenericTypeCompiler):
    """Compile SQLAlchemy types to names understood by Kubling SQL."""

    def visit_string(self, type_, **kw):
        return "STRING"

    visit_VARCHAR = visit_string
    visit_NVARCHAR = visit_string
    visit_unicode = visit_string

    def visit_CHAR(self, type_, **kw):
        return "CHAR"

    visit_NCHAR = visit_CHAR

    def visit_text(self, type_, **kw):
        return "CLOB"

    visit_TEXT = visit_text
    visit_CLOB = visit_text
    visit_NCLOB = visit_text
    visit_unicode_text = visit_text

    def visit_large_binary(self, type_, **kw):
        return "VARBINARY"

    visit_VARBINARY = visit_large_binary
    visit_BINARY = visit_large_binary

    def visit_BLOB(self, type_, **kw):
        return "BLOB"

    def visit_boolean(self, type_, **kw):
        return "BOOLEAN"

    visit_BOOLEAN = visit_boolean

    def visit_small_integer(self, type_, **kw):
        return "SHORT"

    visit_SMALLINT = visit_small_integer

    def visit_integer(self, type_, **kw):
        return "INTEGER"

    visit_INTEGER = visit_integer

    def visit_big_integer(self, type_, **kw):
        return "LONG"

    visit_BIGINT = visit_big_integer

    def visit_numeric(self, type_, **kw):
        if type_.precision is None:
            return "BIGDECIMAL"
        if type_.scale is None:
            return f"BIGDECIMAL({type_.precision})"
        return f"BIGDECIMAL({type_.precision},{type_.scale})"

    visit_NUMERIC = visit_numeric
    visit_DECIMAL = visit_numeric

    def visit_float(self, type_, **kw):
        return "DOUBLE"

    visit_FLOAT = visit_float
    visit_REAL = visit_float
    visit_DOUBLE = visit_float
    visit_DOUBLE_PRECISION = visit_float

    def visit_date(self, type_, **kw):
        return "DATE"

    visit_DATE = visit_date

    def visit_time(self, type_, **kw):
        return "TIME"

    visit_TIME = visit_time

    def visit_datetime(self, type_, **kw):
        return "TIMESTAMP"

    visit_DATETIME = visit_datetime
    visit_TIMESTAMP = visit_datetime

    def visit_JSON(self, type_, **kw):
        return "JSON"


class KublingSQLCompiler(SQLCompiler):
    """Kubling SQL compiler using positional DB-API parameters."""

    def returning_clause(self, *args, **kwargs):
        raise exc.CompileError("Kubling SQLAlchemy does not support RETURNING")


class KublingDDLCompiler(DDLCompiler):
    pass


class KublingIdentifierPreparer(IdentifierPreparer):
    pass


class KublingDialect(KublingReflectionMixin, DefaultDialect):
    name = "kubling"
    driver = "grpc"
    default_paramstyle = "qmark"

    statement_compiler = KublingSQLCompiler
    ddl_compiler = KublingDDLCompiler
    type_compiler_cls = KublingTypeCompiler
    preparer = KublingIdentifierPreparer

    supports_statement_cache = True
    supports_native_boolean = True
    supports_native_decimal = True
    supports_sane_rowcount = True
    supports_sane_multi_rowcount = True
    supports_multivalues_insert = False
    supports_empty_insert = False
    supports_default_values = False
    supports_sequences = False
    supports_identity_columns = False
    supports_server_side_cursors = False
    supports_native_enum = False
    supports_native_uuid = False
    supports_comments = False
    supports_constraint_comments = False
    postfetch_lastrowid = False
    insert_returning = False
    update_returning = False
    delete_returning = False
    use_insertmanyvalues = False

    @classmethod
    def import_dbapi(cls):
        return kubling_dbapi

    def create_connect_args(self, url):
        query = dict(url.query)
        endpoint_option = _pop_scalar(query, "endpoint")
        if endpoint_option:
            if url.host is not None or url.port is not None:
                raise exc.ArgumentError(
                    "use either URL host/port or the endpoint query option, not both"
                )
            endpoint = endpoint_option
        else:
            if not url.host:
                raise exc.ArgumentError("Kubling gRPC URL requires a host")
            if url.port is None:
                raise exc.ArgumentError(
                    "Kubling gRPC URL requires an explicit port; no legacy default is used"
                )
            host = f"[{url.host}]" if ":" in url.host else url.host
            endpoint = f"{host}:{url.port}"

        if not url.database:
            raise exc.ArgumentError("Kubling gRPC URL requires a VDB name")

        insecure = _pop_bool(query, "insecure", default=False)
        wait_for_ready = _pop_bool(query, "wait_for_ready", default=True)
        root_certificates = _pop_file(query, "ca_file")
        private_key = _pop_file(query, "client_key_file")
        certificate_chain = _pop_file(query, "client_cert_file")
        server_name = _pop_scalar(query, "server_name")
        if insecure and any(
            value is not None
            for value in (
                root_certificates,
                private_key,
                certificate_chain,
                server_name,
            )
        ):
            raise exc.ArgumentError(
                "insecure=true cannot be combined with TLS certificate options"
            )
        try:
            tls = None if insecure else TlsConfig(
                root_certificates=root_certificates,
                private_key=private_key,
                certificate_chain=certificate_chain,
                server_name_override=server_name,
            )
        except ValueError as error:
            raise exc.ArgumentError(str(error)) from error

        properties: dict[str, str] = {}
        for key in tuple(query):
            if not key.startswith("property."):
                continue
            property_name = key.removeprefix("property.")
            if not property_name:
                raise exc.ArgumentError("VDB property names cannot be empty")
            properties[property_name] = _pop_scalar(query, key, required=True)

        kwargs: dict[str, Any] = {
            "endpoint": endpoint,
            "vdb_name": url.database,
            "vdb_version": _pop_scalar(query, "vdb_version") or "",
            "username": url.username or "",
            "password": url.password or "",
            "tls": tls,
            "connect_timeout_seconds": _pop_float(
                query, "connect_timeout", default=10.0
            ),
            "rpc_timeout_seconds": _pop_float(query, "rpc_timeout", default=30.0),
            "wait_for_ready": wait_for_ready,
            "max_send_message_bytes": _pop_int(
                query, "max_send_message_bytes", default=16 * 1024 * 1024
            ),
            "max_receive_message_bytes": _pop_int(
                query, "max_receive_message_bytes", default=64 * 1024 * 1024
            ),
            "application_name": (
                _pop_scalar(query, "application_name") or "kubling-sqlalchemy"
            ),
            "properties": properties or None,
        }
        if query:
            raise exc.ArgumentError(
                "unknown Kubling gRPC URL options: " + ", ".join(sorted(query))
            )
        return [], kwargs

    def do_ping(self, dbapi_connection) -> bool:
        return dbapi_connection.ping()

    def do_terminate(self, dbapi_connection) -> None:
        """Discard an invalid pooled connection without surfacing cleanup errors."""

        try:
            dbapi_connection.close()
        except kubling_dbapi.Error:
            pass

    def is_disconnect(self, error, connection, cursor) -> bool:
        if isinstance(error, kubling_dbapi.InterfaceError):
            return "closed" in str(error).lower()
        if not isinstance(error, kubling_dbapi.OperationalError):
            return False
        if error.sqlstate and error.sqlstate.startswith("08"):
            return True
        return error.grpc_code in {
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.UNAUTHENTICATED,
        }

    def get_isolation_level(self, dbapi_connection):
        return "AUTOCOMMIT" if dbapi_connection.autocommit else "KUBLING DEFAULT"

    def get_default_isolation_level(self, dbapi_connection):
        return "KUBLING DEFAULT"

    def get_isolation_level_values(self, dbapi_connection):
        return ("AUTOCOMMIT", "KUBLING DEFAULT")

    def set_isolation_level(self, dbapi_connection, level):
        if level == "AUTOCOMMIT":
            dbapi_connection.autocommit = True
        elif level == "KUBLING DEFAULT":
            dbapi_connection.autocommit = False
        else:
            raise exc.ArgumentError(f"unsupported Kubling isolation level: {level}")

    def detect_autocommit_setting(self, dbapi_connection) -> bool:
        return dbapi_connection.autocommit

    def _get_server_version_info(self, connection):
        version = connection.connection.dbapi_connection.server_version
        match = re.match(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", version)
        if match is None:
            return None
        return tuple(int(part) for part in match.groups() if part is not None)


def _pop_scalar(
    query: dict[str, Any],
    name: str,
    *,
    required: bool = False,
) -> str | None:
    value = query.pop(name, None)
    if isinstance(value, tuple):
        raise exc.ArgumentError(f"Kubling URL option {name!r} may only appear once")
    if value is None:
        if required:
            raise exc.ArgumentError(f"Kubling URL option {name!r} requires a value")
        return None
    if not isinstance(value, str) or (required and not value):
        raise exc.ArgumentError(f"Kubling URL option {name!r} requires a value")
    return value


def _pop_bool(query: dict[str, Any], name: str, *, default: bool) -> bool:
    value = _pop_scalar(query, name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise exc.ArgumentError(f"Kubling URL option {name!r} must be true or false")


def _pop_float(query: dict[str, Any], name: str, *, default: float) -> float:
    value = _pop_scalar(query, name)
    if value is None:
        return default
    try:
        converted = float(value)
    except ValueError as error:
        raise exc.ArgumentError(f"Kubling URL option {name!r} must be numeric") from error
    if converted <= 0:
        raise exc.ArgumentError(f"Kubling URL option {name!r} must be positive")
    return converted


def _pop_int(query: dict[str, Any], name: str, *, default: int) -> int:
    value = _pop_scalar(query, name)
    if value is None:
        return default
    try:
        converted = int(value)
    except ValueError as error:
        raise exc.ArgumentError(f"Kubling URL option {name!r} must be an integer") from error
    if converted <= 0:
        raise exc.ArgumentError(f"Kubling URL option {name!r} must be positive")
    return converted


def _pop_file(query: dict[str, Any], name: str) -> bytes | None:
    value = _pop_scalar(query, name)
    if value is None:
        return None
    try:
        return Path(value).read_bytes()
    except OSError as error:
        raise exc.ArgumentError(f"cannot read Kubling URL option {name!r}") from error


__all__ = [
    "Geography",
    "Geometry",
    "KUBLING_TYPE_MAP",
    "KublingDDLCompiler",
    "KublingDialect",
    "KublingIdentifierPreparer",
    "KublingSQLCompiler",
    "KublingTypeCompiler",
    "XML",
    "map_kubling_type",
]
