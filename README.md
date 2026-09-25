# Kubling SQLAlchemy Dialect

[![Kubling license](https://img.shields.io/badge/license-Apache%202.0-blue.svg?style=flat-square)](LICENSE)
![PyPI](https://img.shields.io/pypi/v/kubling-sqlalchemy?style=flat-square)
[![Contributions welcome](https://img.shields.io/badge/contributions-welcome-brightgreen?style=flat-square)](#contributing)

`kubling-sqlalchemy` connects SQLAlchemy 2 applications to Kubling through its
native client gRPC API. It includes a synchronous DB-API 2.0 driver and a Kubling
SQL compiler. The published `kubling-grpc` package supplies the protocol bindings.

The current scope covers SQLAlchemy Core queries, parameter binding, result
streaming, basic writes, explicit transactions, autocommit, connection pooling and
catalog reflection. Apache Superset integration belongs to its own project.

Version 26.2 replaces the old PostgreSQL/psycopg2 transport. Existing URLs must be
updated with the gRPC endpoint, the VDB version and, for local plaintext servers,
`insecure=true`.

| Component | Supported version |
| --- | --- |
| Python | 3.10–3.14 |
| SQLAlchemy | 2.0.36–2.0.x |
| `kubling-grpc` | 1.1.1 |
| Kubling server | Verified with 26.2-RC5 |
| Apache Superset | Verified with the 7.0 development line; 6.1 and older are incompatible |

## Installation

Python 3.10 or newer is required.

```bash
python -m pip install kubling-sqlalchemy
```

## SQLAlchemy usage

Kubling gRPC uses an explicit endpoint. TLS is enabled by default:

```python
from sqlalchemy import create_engine, text

engine = create_engine(
    "kubling://username:password@kubling.example:55051/Analytics"
    "?vdb_version=1"
)

with engine.connect() as connection:
    rows = connection.execute(
        text("SELECT name FROM inventory.products WHERE category = :category"),
        {"category": "books"},
    )
    for row in rows:
        print(row.name)
```

When using SQLAlchemy expression objects, named `bindparam()` values are compiled
to the driver's positional `qmark` parameters:

```python
from sqlalchemy import Integer, String, bindparam, column, select, table

products = table(
    "products",
    column("name", String),
    column("category", String),
    column("rank", Integer),
    schema="inventory",
)
statement = (
    select(products.c.name)
    .where(products.c.category == bindparam("category"))
    .order_by(products.c.rank)
    .limit(10)
)

with engine.connect() as connection:
    rows = connection.execute(statement, {"category": "books"}).all()
```

For a local plaintext server, opt in explicitly:

```text
kubling://username:password@127.0.0.1:55051/Analytics?vdb_version=1&insecure=true
```

`kubling+grpc://` is an equivalent, more explicit alias. There is no implicit
port. An endpoint can instead be supplied as a query option, which is useful for
IPv6 or custom gRPC resolvers:

```text
kubling://username:password@/Analytics?endpoint=dns:///kubling.example:443&vdb_version=1
```

Supported connection options are:

| Option | Meaning | Default |
| --- | --- | --- |
| `vdb_version` | VDB version sent during login | empty |
| `insecure` | Disable TLS when `true` | `false` |
| `ca_file` | PEM root CA file | system roots |
| `client_cert_file` | PEM client certificate for mTLS | unset |
| `client_key_file` | PEM client private key for mTLS | unset |
| `server_name` | TLS server-name override | unset |
| `connect_timeout` | Channel/login timeout in seconds | `10` |
| `rpc_timeout` | RPC timeout in seconds | `30` |
| `wait_for_ready` | Wait for the channel when `true` | `true` |
| `max_send_message_bytes` | Maximum outbound gRPC message size | `16777216` |
| `max_receive_message_bytes` | Maximum inbound gRPC message size | `67108864` |
| `application_name` | Client name sent at login | `kubling-sqlalchemy` |
| `property.<name>` | Additional VDB login property | unset |

The client certificate and private key must be configured together. TLS options
cannot be combined with `insecure=true`. Unknown or repeated URL options fail
early instead of being silently ignored.

## Direct DB-API usage

The driver can be used without SQLAlchemy:

```python
from kubling_sqlalchemy import dbapi

connection = dbapi.connect(
    endpoint="kubling.example:443",
    vdb_name="Analytics",
    vdb_version="1",
    username="user",
    password="...",
)
try:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT marker, seq FROM acceptance.GRPC_B2_TEST WHERE seq = ?",
        (1,),
    )
    print(cursor.fetchall())
    connection.commit()
finally:
    connection.close()
```

The driver uses DB-API 2.0 `qmark` parameters. Transactions begin lazily unless
`autocommit=True`. A connection permits one unfinished result stream at a time;
closing its cursor cancels that stream and releases the connection.

## Types

The driver handles strings, booleans, bytes, integral and floating-point numbers,
`Decimal`, dates, local times and timestamps, XML, JSON, geometry, geography, arrays
and BLOB/CLOB values. SQLAlchemy reflects those values to its closest standard type;
unknown catalog types become `NullType` with a warning.

DB-API rows return standard Python `time` and `datetime` values when the server value
fits Python's microsecond precision. Values with finer precision retain their
lossless `KublingLocalTime` or `KublingLocalTimestamp` wrapper.

Large BLOB and CLOB values can be returned as references. Call
`connection.materialize_lob(reference)` while the session is active to read and
release one. Array and LOB operations are enabled only when the server advertises
the corresponding capability.

## Catalog reflection

The dialect reflects schemas, tables, views, materialized views, columns, comments,
primary keys, unique constraints, foreign keys and catalog indexes through Kubling's
`SYS` and `SYSADMIN` relations. `Inspector`, `Table(..., autoload_with=...)` and
`MetaData.reflect()` use the same gRPC connection and parameter binding as normal
queries.

Kubling VDBs do not expose one universal default schema. Pass `schema=` explicitly
when listing or reflecting objects. With no configured default schema,
`Inspector.get_table_names()` returns an empty list and table-scoped reflection
raises `NoSuchTableError`; the dialect never substitutes `public` implicitly.

Column reflection preserves length, precision, scale, nullability, default,
autoincrement and comments reported by the catalog. Unknown Kubling types produce a
SQLAlchemy `NullType` warning instead of being assigned an unsafe conversion.

## Current limits

- SQLAlchemy 1.4 and the former PostgreSQL transport are no longer supported.
- A connection has one active result stream; server-side cursors are not exposed.
- `RETURNING`, multi-value inserts, sequences and identity columns are disabled.
- DDL support depends on the source behind the VDB. The dialect does not emulate it.
- SQLAlchemy ORM has not been validated separately.
- Superset support requires its SQLAlchemy 2 line. Released versions through 6.1
  still use SQLAlchemy 1.4 and cannot install this package.

## Development

Install the project and its development tools in a Python 3.10+ virtual
environment:

```bash
python -m pip install -e '.[dev]'
python -m pytest
python -m build
```

The default test run uses in-process gRPC services and does not contact an
external server. To run the live acceptance tests, configure an isolated Kubling
instance and add `--integration`:

```bash
export KUBLING_GRPC_TARGET=localhost:55051
export KUBLING_GRPC_VDB=GrpcAcceptanceVDB
export KUBLING_GRPC_VDB_VERSION=1
export KUBLING_GRPC_USERNAME=test-user
export KUBLING_GRPC_PASSWORD='...'
export KUBLING_GRPC_TEST_TABLE=acceptance.GRPC_B2_TEST
export KUBLING_GRPC_INSECURE=1

python -m pytest --integration -m integration
```

For TLS, omit `KUBLING_GRPC_INSECURE` and optionally set
`KUBLING_GRPC_CA_FILE`, `KUBLING_GRPC_CLIENT_CERT_FILE`,
`KUBLING_GRPC_CLIENT_KEY_FILE` and `KUBLING_GRPC_SERVER_NAME`.

The live transport and DB-API tests also use
`KUBLING_GRPC_LATE_ERROR_SQL` for their controlled late-stream error fixture.
Incremental memory behavior can be measured without retaining rows by running
`python scripts/profile-stream-memory.py` with the same connection variables. The
script reports row and payload sizes, Python peak allocation and process RSS growth;
its SQL, row count, batch size, consumer pause and RSS limit can be overridden with
the `KUBLING_GRPC_MEMORY_*` variables. Set
`KUBLING_GRPC_MEMORY_STRING_BYTES` to generate a parameterized result with an exact
ASCII payload size per row; the default parameterized query returns the requested
row count from the system catalog. Set `KUBLING_GRPC_MEMORY_MATERIALIZE_LOBS=1`
when the query returns LOB references to read, validate and release each payload.
Tests, local connection scripts and internal planning files are excluded from
distribution artifacts. Building the project does not publish it. See
[RELEASING.md](RELEASING.md) for the release procedure.

## Contributing

Issues and pull requests are welcome. Please include focused tests for behavioral
changes and run the local suite before submitting a change.
