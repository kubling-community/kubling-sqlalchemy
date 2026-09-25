from importlib import import_module, metadata


def test_dialect_entry_point_loads_implementation():
    entry_points = metadata.distribution("kubling-sqlalchemy").entry_points
    dialect = import_module("kubling_sqlalchemy.dialect")
    registered = {
        entry.name: entry
        for entry in entry_points
        if entry.group == "sqlalchemy.dialects"
    }

    assert registered["kubling"].load() is dialect.KublingDialect
    assert registered["kubling.grpc"].load() is dialect.KublingDialect
    assert dialect.KublingDialect.name == "kubling"
    assert dialect.KublingDialect.driver == "grpc"


def test_official_bindings_coexist_with_dialect():
    # Loading the dialect must not hide the separately installed kubling namespace.
    import_module("kubling_sqlalchemy.dialect")
    import_module("kubling.features")
    import_module("kubling.v1.command_pb2")
    import_module("kubling.v1.command_pb2_grpc")
    import_module("kubling.v1.error_pb2")
    import_module("grpc_status.rpc_status")
