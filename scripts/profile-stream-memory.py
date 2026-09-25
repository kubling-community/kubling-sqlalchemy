#!/usr/bin/env python3
"""Measure incremental DB-API streaming without retaining result rows."""

from __future__ import annotations

import json
import os
import time
import tracemalloc
from getpass import getpass
from pathlib import Path
from typing import Any

from kubling_sqlalchemy import dbapi
from kubling_sqlalchemy.transport import KublingLobReference, TlsConfig


DEFAULT_SQL = (
    "SELECT a.Name, b.Name FROM SYS.Columns AS a "
    "CROSS JOIN SYS.Columns AS b LIMIT 10000"
)
PARAMETER_SQL = (
    "SELECT ? FROM SYS.Columns AS a "
    "CROSS JOIN SYS.Columns AS b LIMIT {expected_rows}"
)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def optional_bytes(name: str) -> bytes | None:
    path = os.environ.get(name)
    return Path(path).read_bytes() if path else None


def resident_bytes() -> int:
    fields = Path("/proc/self/statm").read_text().split()
    return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")


def value_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(str(value).encode("utf-8"))


def main() -> None:
    expected_rows = int(os.environ.get("KUBLING_GRPC_MEMORY_EXPECTED_ROWS", "10000"))
    arraysize = int(os.environ.get("KUBLING_GRPC_MEMORY_ARRAYSIZE", "32"))
    pause_seconds = float(os.environ.get("KUBLING_GRPC_MEMORY_PAUSE_SECONDS", "0.002"))
    string_bytes = int(os.environ.get("KUBLING_GRPC_MEMORY_STRING_BYTES", "0"))
    materialize_lobs = os.environ.get("KUBLING_GRPC_MEMORY_MATERIALIZE_LOBS") == "1"
    if string_bytes < 0:
        raise SystemExit("KUBLING_GRPC_MEMORY_STRING_BYTES cannot be negative")
    max_rss_delta = int(
        os.environ.get("KUBLING_GRPC_MEMORY_MAX_RSS_DELTA_BYTES", str(64 * 1024 * 1024))
    )
    timeout = float(os.environ.get("KUBLING_GRPC_TIMEOUT_SECONDS", "30"))
    insecure = os.environ.get("KUBLING_GRPC_INSECURE") == "1"
    tls = None
    if not insecure:
        tls = TlsConfig(
            root_certificates=optional_bytes("KUBLING_GRPC_CA_FILE"),
            private_key=optional_bytes("KUBLING_GRPC_CLIENT_KEY_FILE"),
            certificate_chain=optional_bytes("KUBLING_GRPC_CLIENT_CERT_FILE"),
            server_name_override=os.environ.get("KUBLING_GRPC_SERVER_NAME"),
        )

    connection = dbapi.connect(
        required_env("KUBLING_GRPC_TARGET"),
        tls=tls,
        vdb_name=required_env("KUBLING_GRPC_VDB"),
        vdb_version=required_env("KUBLING_GRPC_VDB_VERSION"),
        username=required_env("KUBLING_GRPC_USERNAME"),
        password=os.environ.get("KUBLING_GRPC_PASSWORD") or getpass("Password: "),
        connect_timeout_seconds=timeout,
        rpc_timeout_seconds=timeout,
        application_name="kubling-sqlalchemy-memory-profile",
        autocommit=True,
    )

    rows = 0
    payload_bytes = 0
    max_row_bytes = 0
    lob_references = 0
    lob_bytes_materialized = 0
    pending_lob_rows: list[tuple[int, tuple[KublingLobReference, ...]]] = []
    rss_before = resident_bytes()
    max_rss = rss_before
    started = time.monotonic()
    tracemalloc.start()
    try:
        cursor = connection.cursor()
        cursor.arraysize = arraysize
        sql = os.environ.get("KUBLING_GRPC_MEMORY_SQL")
        parameters = ()
        if string_bytes:
            sql = sql or PARAMETER_SQL.format(expected_rows=expected_rows)
            parameters = ("x" * string_bytes,)
        cursor.execute(sql or DEFAULT_SQL, parameters)
        while True:
            batch = cursor.fetchmany()
            if not batch:
                break
            for row in batch:
                references = tuple(
                    value for value in row if isinstance(value, KublingLobReference)
                )
                row_bytes = sum(
                    value_size(value)
                    for value in row
                    if not isinstance(value, KublingLobReference)
                )
                lob_references += len(references)
                rows += 1
                if references and materialize_lobs:
                    pending_lob_rows.append((row_bytes, references))
                else:
                    row_bytes += sum(reference.size_bytes for reference in references)
                    payload_bytes += row_bytes
                    max_row_bytes = max(max_row_bytes, row_bytes)
            max_rss = max(max_rss, resident_bytes())
            if pause_seconds:
                time.sleep(pause_seconds)

        for inline_bytes, references in pending_lob_rows:
            row_bytes = inline_bytes
            for reference in references:
                materialized = connection.materialize_lob(reference)
                materialized_bytes = value_size(materialized)
                if materialized_bytes != reference.size_bytes:
                    raise SystemExit(
                        "materialized LOB size differs from its reference: "
                        f"{materialized_bytes} != {reference.size_bytes}"
                    )
                row_bytes += materialized_bytes
                lob_bytes_materialized += materialized_bytes
                max_rss = max(max_rss, resident_bytes())
                if pause_seconds:
                    time.sleep(pause_seconds)
            payload_bytes += row_bytes
            max_row_bytes = max(max_row_bytes, row_bytes)
    finally:
        connection.close()
        _, python_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    result = {
        "rows": rows,
        "payload_bytes": payload_bytes,
        "max_row_bytes": max_row_bytes,
        "arraysize": arraysize,
        "parameter_string_bytes": string_bytes,
        "lob_references": lob_references,
        "lob_bytes_materialized": lob_bytes_materialized,
        "materialize_lobs": materialize_lobs,
        "pause_seconds_per_batch": pause_seconds,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "rss_before_bytes": rss_before,
        "max_rss_bytes": max_rss,
        "rss_delta_bytes": max_rss - rss_before,
        "python_peak_bytes": python_peak,
        "rss_delta_limit_bytes": max_rss_delta,
    }
    print(json.dumps(result, sort_keys=True))

    if rows != expected_rows:
        raise SystemExit(f"expected {expected_rows} rows, received {rows}")
    if result["rss_delta_bytes"] > max_rss_delta:
        raise SystemExit(
            f"RSS grew by {result['rss_delta_bytes']} bytes; limit is {max_rss_delta}"
        )


if __name__ == "__main__":
    main()
