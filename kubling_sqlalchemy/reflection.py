"""SQLAlchemy catalog reflection implemented with Kubling SQL metadata tables."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import exc, text, util
from sqlalchemy.engine import reflection
from sqlalchemy.sql.sqltypes import ARRAY, CHAR, LargeBinary, NullType, Numeric, String

from .transport import KublingClob, KublingLobReference


_REFERENTIAL_ACTIONS = {
    0: "CASCADE",
    1: "RESTRICT",
    2: "SET NULL",
    3: "NO ACTION",
    4: "SET DEFAULT",
}


class KublingReflectionMixin:
    """Reflect Kubling objects without assuming a PostgreSQL default schema."""

    @reflection.cache
    def get_schema_names(self, connection, **kw):
        result = connection.execute(
            text("SELECT Name FROM SYS.Schemas ORDER BY Name")
        )
        return [row[0] for row in result]

    @reflection.cache
    def get_table_names(self, connection, schema=None, **kw):
        schema = self._reflection_schema(schema)
        if schema is None:
            return []
        result = connection.execute(
            text(
                "SELECT Name FROM SYS.Tables "
                "WHERE SchemaName = :schema AND Type = 'Table' "
                "AND IsMaterialized = false "
                "AND Name NOT IN ("
                "SELECT Name FROM SYSADMIN.Views WHERE SchemaName = :schema"
                ") ORDER BY Name"
            ),
            {"schema": schema},
        )
        return [row[0] for row in result]

    @reflection.cache
    def get_view_names(self, connection, schema=None, **kw):
        schema = self._reflection_schema(schema)
        if schema is None:
            return []
        result = connection.execute(
            text(
                "SELECT Name FROM SYSADMIN.Views "
                "WHERE SchemaName = :schema ORDER BY Name"
            ),
            {"schema": schema},
        )
        return [row[0] for row in result]

    @reflection.cache
    def get_materialized_view_names(self, connection, schema=None, **kw):
        schema = self._reflection_schema(schema)
        if schema is None:
            return []
        result = connection.execute(
            text(
                "SELECT Name FROM SYS.Tables "
                "WHERE SchemaName = :schema AND IsMaterialized = true "
                "ORDER BY Name"
            ),
            {"schema": schema},
        )
        return [row[0] for row in result]

    @reflection.cache
    def has_table(self, connection, table_name, schema=None, **kw):
        schema = self._reflection_schema(schema)
        if schema is None:
            return False
        result = connection.execute(
            text(
                "SELECT Name FROM SYS.Tables "
                "WHERE SchemaName = :schema AND Name = :table_name"
            ),
            {"schema": schema, "table_name": table_name},
        )
        return result.first() is not None

    @reflection.cache
    def get_columns(self, connection, table_name, schema=None, **kw):
        schema = self._require_reflection_schema(table_name, schema)
        result = connection.execute(
            text(
                "SELECT c.Name, c.DataType, c.Length, c.Precision, c.Scale, "
                "c.NullType, c.DefaultValue, c.IsAutoIncremented, c.Description "
                "FROM SYS.Columns AS c "
                "WHERE c.SchemaName = :schema AND c.TableName = :table_name "
                "ORDER BY c.Position"
            ),
            {"schema": schema, "table_name": table_name},
        )
        rows = list(result.mappings())
        if not rows:
            raise exc.NoSuchTableError(f"{schema}.{table_name}")
        return [self._reflect_column(row) for row in rows]

    @reflection.cache
    def get_pk_constraint(self, connection, table_name, schema=None, **kw):
        schema = self._require_reflection_schema(table_name, schema)
        rows = self._key_columns(connection, table_name, schema, "Primary")
        if not rows:
            self._ensure_table_exists(connection, table_name, schema, kw)
            return {"name": None, "constrained_columns": []}
        name = rows[0]["KeyName"]
        return {
            "name": name,
            "constrained_columns": [
                row["Name"] for row in rows if row["KeyName"] == name
            ],
        }

    @reflection.cache
    def get_unique_constraints(self, connection, table_name, schema=None, **kw):
        schema = self._require_reflection_schema(table_name, schema)
        rows = self._key_columns(connection, table_name, schema, "Unique")
        constraints: dict[str, list[str]] = {}
        for row in rows:
            constraints.setdefault(row["KeyName"], []).append(row["Name"])
        reflected = [
            {"name": name, "column_names": columns}
            for name, columns in constraints.items()
        ]
        if not reflected:
            self._ensure_table_exists(connection, table_name, schema, kw)
        return reflected

    @reflection.cache
    def get_indexes(self, connection, table_name, schema=None, **kw):
        schema = self._require_reflection_schema(table_name, schema)
        rows = self._key_columns(connection, table_name, schema, "Index")
        indexes: dict[str, list[str]] = {}
        for row in rows:
            indexes.setdefault(row["KeyName"], []).append(row["Name"])
        reflected = [
            {"name": name, "column_names": columns, "unique": False}
            for name, columns in indexes.items()
        ]
        if not reflected:
            self._ensure_table_exists(connection, table_name, schema, kw)
        return reflected

    @reflection.cache
    def get_check_constraints(self, connection, table_name, schema=None, **kw):
        schema = self._require_reflection_schema(table_name, schema)
        self._ensure_table_exists(connection, table_name, schema, kw)
        return []

    @reflection.cache
    def get_table_options(self, connection, table_name, schema=None, **kw):
        schema = self._require_reflection_schema(table_name, schema)
        self._ensure_table_exists(connection, table_name, schema, kw)
        return {}

    @reflection.cache
    def get_foreign_keys(self, connection, table_name, schema=None, **kw):
        schema = self._require_reflection_schema(table_name, schema)
        result = connection.execute(
            text(
                "SELECT FK_NAME, FKCOLUMN_NAME, PKTABLE_SCHEM, PKTABLE_NAME, "
                "PKCOLUMN_NAME, KEY_SEQ, UPDATE_RULE, DELETE_RULE "
                "FROM SYS.ReferenceKeyColumns "
                "WHERE FKTABLE_SCHEM = :schema AND FKTABLE_NAME = :table_name "
                "ORDER BY FK_NAME, KEY_SEQ"
            ),
            {"schema": schema, "table_name": table_name},
        )
        constraints: dict[str, dict[str, Any]] = {}
        for row in result.mappings():
            name = row["FK_NAME"]
            constraint = constraints.setdefault(
                name,
                {
                    "name": name,
                    "constrained_columns": [],
                    "referred_schema": row["PKTABLE_SCHEM"],
                    "referred_table": row["PKTABLE_NAME"],
                    "referred_columns": [],
                    "options": self._referential_options(
                        row["UPDATE_RULE"], row["DELETE_RULE"]
                    ),
                },
            )
            constraint["constrained_columns"].append(row["FKCOLUMN_NAME"])
            constraint["referred_columns"].append(row["PKCOLUMN_NAME"])
        reflected = list(constraints.values())
        if not reflected:
            self._ensure_table_exists(connection, table_name, schema, kw)
        return reflected

    @reflection.cache
    def get_table_comment(self, connection, table_name, schema=None, **kw):
        schema = self._require_reflection_schema(table_name, schema)
        result = connection.execute(
            text(
                "SELECT Description FROM SYS.Tables "
                "WHERE SchemaName = :schema AND Name = :table_name"
            ),
            {"schema": schema, "table_name": table_name},
        ).first()
        if result is None:
            raise exc.NoSuchTableError(f"{schema}.{table_name}")
        return {"text": result[0]}

    @reflection.cache
    def get_view_definition(self, connection, view_name, schema=None, **kw):
        schema = self._require_reflection_schema(view_name, schema)
        result = connection.execute(
            text(
                "SELECT Body FROM SYSADMIN.Views "
                "WHERE SchemaName = :schema AND Name = :view_name"
            ),
            {"schema": schema, "view_name": view_name},
        ).first()
        if result is None:
            raise exc.NoSuchTableError(f"{schema}.{view_name}")
        body = result[0]
        if isinstance(body, KublingClob):
            return body.text
        if isinstance(body, KublingLobReference):
            dbapi_connection = connection.connection.dbapi_connection
            materialized = dbapi_connection.materialize_lob(body)
            if not isinstance(materialized, str):
                raise exc.InvalidRequestError("view definition was returned as a BLOB")
            return materialized
        if isinstance(body, str):
            return body
        raise exc.InvalidRequestError("view definition has an unsupported value type")

    def _reflection_schema(self, schema: str | None) -> str | None:
        return schema if schema is not None else self.default_schema_name

    def _require_reflection_schema(self, object_name: str, schema: str | None) -> str:
        resolved = self._reflection_schema(schema)
        if resolved is None:
            raise exc.NoSuchTableError(
                f"{object_name}; Kubling reflection requires an explicit schema"
            )
        return resolved

    def _ensure_table_exists(
        self,
        connection,
        table_name: str,
        schema: str,
        kw: Mapping[str, Any],
    ) -> None:
        if not self.has_table(
            connection,
            table_name,
            schema=schema,
            info_cache=kw.get("info_cache"),
        ):
            raise exc.NoSuchTableError(f"{schema}.{table_name}")

    def _reflect_column(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "name": row["Name"],
            "type": self._reflect_column_type(row),
            "nullable": row["NullType"] != "No Nulls",
            "default": row["DefaultValue"],
            "autoincrement": bool(row["IsAutoIncremented"]),
            "comment": row["Description"],
        }

    @staticmethod
    def _reflect_column_type(row: Mapping[str, Any]):
        from .dialect import map_kubling_type

        type_name = str(row["DataType"]).lower()
        dimensions = 0
        while type_name.endswith("[]"):
            dimensions += 1
            type_name = type_name[:-2]
        length = row["Length"] if row["Length"] and row["Length"] > 0 else None
        precision = (
            row["Precision"] if row["Precision"] and row["Precision"] > 0 else None
        )
        scale = row["Scale"] if row["Scale"] is not None else None
        try:
            if type_name == "string":
                reflected = String(length=length)
            elif type_name == "char":
                reflected = CHAR(length=length)
            elif type_name == "varbinary":
                reflected = LargeBinary(length=length)
            elif type_name == "bigdecimal":
                reflected = Numeric(precision=precision, scale=scale)
            elif type_name == "biginteger":
                reflected = Numeric(precision=precision, scale=0)
            else:
                reflected = map_kubling_type(type_name)
        except ValueError:
            util.warn(
                f"Did not recognize Kubling type {row['DataType']!r}; using NullType"
            )
            return NullType()
        if dimensions:
            return ARRAY(reflected, dimensions=dimensions)
        return reflected

    @staticmethod
    def _key_columns(connection, table_name: str, schema: str, key_type: str):
        result = connection.execute(
            text(
                "SELECT KeyName, Name, Position FROM SYS.KeyColumns "
                "WHERE SchemaName = :schema AND TableName = :table_name "
                "AND KeyType = :key_type ORDER BY KeyName, Position"
            ),
            {"schema": schema, "table_name": table_name, "key_type": key_type},
        )
        return list(result.mappings())

    @staticmethod
    def _referential_options(update_rule: int, delete_rule: int) -> dict[str, str]:
        options = {}
        if update_rule in _REFERENTIAL_ACTIONS:
            options["onupdate"] = _REFERENTIAL_ACTIONS[update_rule]
        if delete_rule in _REFERENTIAL_ACTIONS:
            options["ondelete"] = _REFERENTIAL_ACTIONS[delete_rule]
        return options


__all__ = ["KublingReflectionMixin"]
