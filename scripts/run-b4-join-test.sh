#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-${repo_root}/.venv/bin/python}"

if [[ ! -x "${python_bin}" ]]; then
    printf 'No encuentro Python en %s\n' "${python_bin}" >&2
    printf 'Creá .venv o indicá otro intérprete mediante PYTHON=/ruta/python.\n' >&2
    exit 1
fi

if [[ "${1:-}" == "--setup" ]]; then
    shift
    "${python_bin}" -m pip install -e "${repo_root}[dev]"
fi

if ! "${python_bin}" -c \
    'import grpc, pytest, sqlalchemy; import kubling.features, kubling.v1.command_pb2' \
    >/dev/null 2>&1; then
    printf 'Faltan dependencias en %s\n' "${python_bin}" >&2
    printf 'Ejecutá una vez: %s --setup\n' "$0" >&2
    exit 1
fi

if [[ -z "${KUBLING_GRPC_USERNAME:-}" ]]; then
    read -r -p 'Usuario gRPC: ' KUBLING_GRPC_USERNAME
fi

if [[ -z "${KUBLING_GRPC_PASSWORD:-}" ]]; then
    read -r -s -p 'Contraseña gRPC: ' KUBLING_GRPC_PASSWORD
    printf '\n'
fi

export KUBLING_GRPC_USERNAME
export KUBLING_GRPC_PASSWORD
export KUBLING_GRPC_TARGET="${KUBLING_GRPC_TARGET:-127.0.0.1:55051}"
export KUBLING_GRPC_INSECURE="${KUBLING_GRPC_INSECURE:-1}"
export KUBLING_GRPC_VDB="${KUBLING_GRPC_VDB:-GrpcAcceptanceVDB}"
export KUBLING_GRPC_VDB_VERSION="${KUBLING_GRPC_VDB_VERSION:-1}"
export KUBLING_GRPC_TEST_TABLE="${KUBLING_GRPC_TEST_TABLE:-acceptance.GRPC_B2_TEST}"
export KUBLING_GRPC_TEST_MARKER="${KUBLING_GRPC_TEST_MARKER:-grpc-b2}"
export KUBLING_GRPC_TIMEOUT_SECONDS="${KUBLING_GRPC_TIMEOUT_SECONDS:-120}"

cd "${repo_root}"

exec "${python_bin}" -m pytest \
    --integration \
    tests/integration/test_dialect.py::test_core_join_aggregate_grouping_and_cast \
    -vv -s \
    "$@"
