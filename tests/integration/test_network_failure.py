import os
import socket
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep

import grpc
import pytest

from kubling_sqlalchemy import dbapi
from kubling_sqlalchemy.transport import TlsConfig


pytestmark = pytest.mark.integration


def _required_env(name):
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"--integration requires {name}.", pytrace=False)
    return value


def _optional_bytes(name):
    path = os.environ.get(name)
    return Path(path).read_bytes() if path else None


def _target_address():
    target = _required_env("KUBLING_GRPC_TARGET")
    host, separator, port = target.rpartition(":")
    if not separator or not host or not port.isdigit():
        pytest.skip("network-cut acceptance requires a host:port target")
    return host, int(port)


def _tls_config(*, proxy_host=None):
    if os.environ.get("KUBLING_GRPC_INSECURE") == "1":
        return None
    original_host, _ = _target_address()
    return TlsConfig(
        root_certificates=_optional_bytes("KUBLING_GRPC_CA_FILE"),
        private_key=_optional_bytes("KUBLING_GRPC_CLIENT_KEY_FILE"),
        certificate_chain=_optional_bytes("KUBLING_GRPC_CLIENT_CERT_FILE"),
        server_name_override=(
            os.environ.get("KUBLING_GRPC_SERVER_NAME")
            or (original_host if proxy_host is not None else None)
        ),
    )


def _connect(endpoint, *, through_proxy=False):
    timeout = float(os.environ.get("KUBLING_GRPC_TIMEOUT_SECONDS", "30"))
    return dbapi.connect(
        endpoint,
        tls=_tls_config(proxy_host=endpoint if through_proxy else None),
        vdb_name=_required_env("KUBLING_GRPC_VDB"),
        vdb_version=_required_env("KUBLING_GRPC_VDB_VERSION"),
        username=_required_env("KUBLING_GRPC_USERNAME"),
        password=_required_env("KUBLING_GRPC_PASSWORD"),
        connect_timeout_seconds=min(timeout, 5),
        rpc_timeout_seconds=min(timeout, 5),
        application_name="kubling-sqlalchemy-network-cut-acceptance",
        autocommit=True,
    )


def _fetchall(connection, sql, parameters=()):
    cursor = connection.cursor().execute(sql, parameters)
    rows = cursor.fetchall()
    cursor.close()
    return rows


class _TcpCutProxy:
    def __init__(self, upstream):
        self._upstream = upstream
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.1)
        self.port = self._listener.getsockname()[1]
        self._stopped = Event()
        self._lock = Lock()
        self._sockets = []
        self._thread = Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self):
        while not self._stopped.is_set():
            try:
                downstream, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            upstream = socket.create_connection(self._upstream, timeout=5)
            upstream.settimeout(None)
            with self._lock:
                self._sockets.extend((downstream, upstream))
            Thread(target=self._pump, args=(downstream, upstream), daemon=True).start()
            Thread(target=self._pump, args=(upstream, downstream), daemon=True).start()

    def _pump(self, source, destination):
        try:
            while not self._stopped.is_set():
                data = source.recv(65536)
                if not data:
                    return
                destination.sendall(data)
        except OSError:
            return

    def cut(self):
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            self._listener.close()
        except OSError:
            pass
        with self._lock:
            sockets = tuple(self._sockets)
            self._sockets.clear()
        for current in sockets:
            try:
                current.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                current.close()
            except OSError:
                pass
        self._thread.join(timeout=1)


def test_live_network_cut_interrupts_stream_and_releases_server_request(request):
    if not request.config.getoption("--integration"):
        pytest.skip("Use --integration to enable database access.")
    upstream = _target_address()
    proxy = _TcpCutProxy(upstream)
    target = None
    observer = _connect(_required_env("KUBLING_GRPC_TARGET"))
    try:
        target = _connect(f"127.0.0.1:{proxy.port}", through_proxy=True)
        session_id = target.session_id
        cursor = target.cursor()
        cursor.arraysize = 1
        cursor.execute(
            "SELECT ? FROM SYS.Columns AS a CROSS JOIN SYS.Columns AS b LIMIT 100000",
            ("x" * 65536,),
        )
        assert cursor.fetchone() is not None
        assert _fetchall(
            observer,
            "SELECT SessionId, ExecutionId FROM SYSADMIN.REQUESTS "
            "WHERE SessionId = ?",
            (session_id,),
        )

        proxy.cut()
        with pytest.raises(dbapi.OperationalError) as captured:
            while cursor.fetchone() is not None:
                pass
        assert captured.value.grpc_code == grpc.StatusCode.UNAVAILABLE

        started = monotonic()
        while _fetchall(
            observer,
            "SELECT SessionId, ExecutionId FROM SYSADMIN.REQUESTS "
            "WHERE SessionId = ?",
            (session_id,),
        ):
            if monotonic() - started >= 5:
                pytest.fail(
                    "server request remained visible after network cut",
                    pytrace=False,
                )
            sleep(0.05)
    finally:
        proxy.cut()
        if target is not None:
            try:
                target.close()
            except dbapi.Error:
                pass
        observer.close()

    healthy = _connect(_required_env("KUBLING_GRPC_TARGET"))
    try:
        cursor = healthy.cursor().execute("SELECT 1")
        assert cursor.fetchone() == (1,)
        cursor.close()
    finally:
        healthy.close()
