import grpc
import pytest
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Table,
    Time,
    bindparam,
    cast,
    column,
    delete,
    exc,
    func,
    insert,
    select,
    table,
    update,
)
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.engine import URL, make_url
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.schema import CreateTable, MetaData

from kubling_sqlalchemy import dbapi
from kubling_sqlalchemy.dialect import (
    Geography,
    Geometry,
    KublingDialect,
    XML,
    map_kubling_type,
)
from kubling_sqlalchemy.transport import RpcErrorDetails, TlsConfig


def test_dialect_uses_native_dbapi_and_declares_only_verified_capabilities():
    dialect = KublingDialect()

    assert issubclass(KublingDialect, DefaultDialect)
    assert not issubclass(KublingDialect, PGDialect)
    assert dialect.import_dbapi() is dbapi
    assert dialect.name == "kubling"
    assert dialect.driver == "grpc"
    assert dialect.paramstyle == "qmark"
    assert dialect.positional is True
    assert dialect.supports_native_boolean is True
    assert dialect.supports_native_decimal is True
    assert dialect.supports_statement_cache is True
    assert dialect.supports_multivalues_insert is False
    assert dialect.insert_returning is False
    assert dialect.update_returning is False
    assert dialect.delete_returning is False
    assert dialect.postfetch_lastrowid is False


def test_url_requires_explicit_grpc_endpoint_and_preserves_credentials():
    dialect = KublingDialect()
    url = make_url(
        "kubling://alice:p%40ss@server.example:55051/Analytics"
        "?vdb_version=1&insecure=true&connect_timeout=4.5&rpc_timeout=9"
        "&wait_for_ready=false&max_send_message_bytes=1024"
        "&max_receive_message_bytes=2048&application_name=unit"
        "&property.role=reader"
    )

    args, kwargs = dialect.create_connect_args(url)

    assert args == []
    assert kwargs == {
        "endpoint": "server.example:55051",
        "vdb_name": "Analytics",
        "vdb_version": "1",
        "username": "alice",
        "password": "p@ss",
        "tls": None,
        "connect_timeout_seconds": 4.5,
        "rpc_timeout_seconds": 9.0,
        "wait_for_ready": False,
        "max_send_message_bytes": 1024,
        "max_receive_message_bytes": 2048,
        "application_name": "unit",
        "properties": {"role": "reader"},
    }

    with pytest.raises(exc.ArgumentError, match="explicit port"):
        dialect.create_connect_args(make_url("kubling://u:p@host/VDB"))
    with pytest.raises(exc.ArgumentError, match="VDB name"):
        dialect.create_connect_args(make_url("kubling://u:p@host:55051"))


def test_url_supports_ipv6_explicit_targets_and_tls_files(tmp_path):
    dialect = KublingDialect()
    args, ipv6 = dialect.create_connect_args(
        make_url("kubling://u:p@[::1]:55051/VDB?vdb_version=1&insecure=true")
    )
    assert args == []
    assert ipv6["endpoint"] == "[::1]:55051"

    ca = tmp_path / "ca.pem"
    key = tmp_path / "client.key"
    cert = tmp_path / "client.pem"
    ca.write_bytes(b"ca")
    key.write_bytes(b"key")
    cert.write_bytes(b"cert")
    url = URL.create(
        "kubling",
        username="u",
        password="p",
        database="VDB",
        query={
            "endpoint": "dns:///kubling.example:443",
            "ca_file": str(ca),
            "client_key_file": str(key),
            "client_cert_file": str(cert),
            "server_name": "kubling.example",
        },
    )

    _, kwargs = dialect.create_connect_args(url)

    assert kwargs["endpoint"] == "dns:///kubling.example:443"
    assert kwargs["tls"] == TlsConfig(
        root_certificates=b"ca",
        private_key=b"key",
        certificate_chain=b"cert",
        server_name_override="kubling.example",
    )


@pytest.mark.parametrize(
    "url, message",
    [
        ("kubling://u:p@host:1/VDB?unknown=x", "unknown"),
        ("kubling://u:p@host:1/VDB?insecure=maybe", "true or false"),
        ("kubling://u:p@host:1/VDB?connect_timeout=0", "positive"),
        ("kubling://u:p@host:1/VDB?max_send_message_bytes=x", "integer"),
        (
            "kubling://u:p@host:1/VDB?endpoint=dns%3A%2F%2F%2Fx%3A1",
            "either URL host/port",
        ),
        (
            "kubling://u:p@host:1/VDB?insecure=true&server_name=host",
            "cannot be combined",
        ),
    ],
)
def test_url_rejects_ambiguous_or_invalid_options(url, message):
    with pytest.raises(exc.ArgumentError, match=message):
        KublingDialect().create_connect_args(make_url(url))


def test_core_compilation_uses_identifiers_qmark_and_stable_parameter_order():
    dialect = KublingDialect()
    left = table(
        "Order",
        column("select", String),
        column("seq", Integer),
        schema="Acceptance",
    )
    right = left.alias("other")
    statement = (
        select(left.c.select, func.count(right.c.seq).label("total"))
        .select_from(left.join(right, left.c.seq == right.c.seq))
        .where(left.c.select == bindparam("marker"))
        .group_by(left.c.select)
        .having(func.count(right.c.seq) > bindparam("minimum"))
        .order_by(left.c.select)
        .limit(5)
        .offset(2)
    )

    compiled = statement.compile(dialect=dialect)

    assert 'FROM "Acceptance"."Order" JOIN "Acceptance"."Order" AS other' in str(
        compiled
    )
    assert '"Acceptance"."Order"."select" = ?' in str(compiled)
    assert "GROUP BY" in str(compiled)
    assert "HAVING count(other.seq) > ?" in str(compiled)
    assert "LIMIT ? OFFSET ?" in str(compiled)
    assert compiled.positiontup == ["marker", "minimum", "param_1", "param_2"]
    assert statement._generate_cache_key() == statement._generate_cache_key()


def test_casts_ddl_and_writes_use_kubling_types_and_positional_parameters():
    dialect = KublingDialect()
    metadata = MetaData()
    records = Table(
        "records",
        metadata,
        Column("id", BigInteger),
        Column("amount", Numeric(12, 3)),
        Column("enabled", Boolean),
        Column("created", DateTime),
        Column("day", Date),
        Column("clock", Time),
        Column("payload", LargeBinary),
        Column("name", String),
    )

    ddl = str(CreateTable(records).compile(dialect=dialect))
    assert "id LONG" in ddl
    assert "amount BIGDECIMAL(12,3)" in ddl
    assert "enabled BOOLEAN" in ddl
    assert "created TIMESTAMP" in ddl
    assert "payload VARBINARY" in ddl
    cast_sql = str(
        select(
            cast(bindparam("name"), String),
            cast(bindparam("amount"), Numeric(12, 3)),
        ).compile(dialect=dialect)
    )
    assert "CAST(? AS STRING)" in cast_sql
    assert "CAST(? AS BIGDECIMAL(12,3))" in cast_sql

    lightweight = table(
        "records",
        column("id", Integer),
        column("name", String),
    )
    writes = [
        insert(lightweight).values(id=1, name="one"),
        update(lightweight)
        .where(lightweight.c.id == bindparam("target"))
        .values(name=bindparam("name")),
        delete(lightweight).where(lightweight.c.id == bindparam("target")),
    ]
    compiled = [statement.compile(dialect=dialect) for statement in writes]
    assert all("?" in str(item) for item in compiled)
    with pytest.raises(exc.CompileError, match="RETURNING"):
        insert(lightweight).returning(lightweight.c.id).compile(dialect=dialect)


def test_type_mapping_keeps_spatial_metadata_and_rejects_unknown_types():
    assert isinstance(map_kubling_type("integer"), Integer)
    assert isinstance(map_kubling_type("VARBINARY"), LargeBinary)
    assert isinstance(map_kubling_type("XML"), XML)
    assert Geometry().geometry_type == "GEOMETRY"
    assert Geometry().srid == 4326
    assert Geography().geometry_type == "GEOGRAPHY"
    assert Geography().srid == 4326
    with pytest.raises(ValueError, match="Unsupported"):
        map_kubling_type("future-type")


class _IsolationConnection:
    autocommit = False


def test_isolation_ping_and_disconnect_rules_are_explicit():
    dialect = KublingDialect()
    connection = _IsolationConnection()
    connection.ping = lambda: True

    assert dialect.do_ping(connection) is True
    assert dialect.get_isolation_level(connection) == "KUBLING DEFAULT"
    dialect.set_isolation_level(connection, "AUTOCOMMIT")
    assert connection.autocommit is True
    assert dialect.detect_autocommit_setting(connection) is True
    dialect.set_isolation_level(connection, "KUBLING DEFAULT")
    assert connection.autocommit is False

    unavailable = dbapi.OperationalError(
        "unavailable",
        details=RpcErrorDetails(grpc.StatusCode.UNAVAILABLE, "unavailable"),
    )
    sql_connection = dbapi.OperationalError(
        "connection",
        details=RpcErrorDetails(
            grpc.StatusCode.INTERNAL,
            "connection",
            sql_state="08006",
        ),
    )
    statement_timeout = dbapi.OperationalError(
        "timeout",
        details=RpcErrorDetails(grpc.StatusCode.DEADLINE_EXCEEDED, "timeout"),
    )
    assert dialect.is_disconnect(unavailable, None, None) is True
    assert dialect.is_disconnect(sql_connection, None, None) is True
    assert dialect.is_disconnect(statement_timeout, None, None) is False
    assert dialect.is_disconnect(dbapi.InterfaceError("connection is closed"), None, None)


def test_pool_termination_swallows_only_dbapi_cleanup_failures():
    class BrokenConnection:
        closed = False

        def close(self):
            self.closed = True
            raise dbapi.OperationalError("session already expired")

    connection = BrokenConnection()

    KublingDialect().do_terminate(connection)

    assert connection.closed is True
