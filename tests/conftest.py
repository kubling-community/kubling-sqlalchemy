import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests using the KUBLING_GRPC_* environment variables.",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--integration"):
        return
    skip = pytest.mark.skip(reason="Use --integration to enable database access.")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def engine(request):
    if not request.config.getoption("--integration"):
        pytest.skip("Use --integration to enable database access.")
    required = (
        "KUBLING_GRPC_TARGET",
        "KUBLING_GRPC_VDB",
        "KUBLING_GRPC_VDB_VERSION",
        "KUBLING_GRPC_USERNAME",
        "KUBLING_GRPC_PASSWORD",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.fail(
            "--integration requires " + ", ".join(missing) + ".",
            pytrace=False,
        )
    query = {
        "endpoint": os.environ["KUBLING_GRPC_TARGET"],
        "vdb_version": os.environ["KUBLING_GRPC_VDB_VERSION"],
        "connect_timeout": os.environ.get("KUBLING_GRPC_TIMEOUT_SECONDS", "30"),
        "rpc_timeout": os.environ.get("KUBLING_GRPC_TIMEOUT_SECONDS", "30"),
    }
    if os.environ.get("KUBLING_GRPC_INSECURE") == "1":
        query["insecure"] = "true"
    else:
        tls_options = {
            "ca_file": "KUBLING_GRPC_CA_FILE",
            "client_key_file": "KUBLING_GRPC_CLIENT_KEY_FILE",
            "client_cert_file": "KUBLING_GRPC_CLIENT_CERT_FILE",
            "server_name": "KUBLING_GRPC_SERVER_NAME",
        }
        for option, variable in tls_options.items():
            value = os.environ.get(variable)
            if value:
                query[option] = value
    url = URL.create(
        "kubling",
        username=os.environ["KUBLING_GRPC_USERNAME"],
        password=os.environ["KUBLING_GRPC_PASSWORD"],
        database=os.environ["KUBLING_GRPC_VDB"],
        query=query,
    )
    engine = create_engine(
        url,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def schema_name():
    table = os.environ.get("KUBLING_GRPC_TEST_TABLE", "acceptance.GRPC_B2_TEST")
    return table.rpartition(".")[0]


@pytest.fixture
def table_name():
    table = os.environ.get("KUBLING_GRPC_TEST_TABLE", "acceptance.GRPC_B2_TEST")
    return table.rpartition(".")[2]
