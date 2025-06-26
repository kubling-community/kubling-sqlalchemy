import logging

from sqlalchemy import text
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.engine import reflection
from sqlalchemy.types import (
    BigInteger, Boolean, Date, DateTime, Float, Integer, JSON, LargeBinary,
    Numeric, PickleType, SmallInteger, String, Text, UserDefinedType,
)


class Geography(UserDefinedType):
    def get_col_spec(self):
        return "GEOGRAPHY"


class Geometry(UserDefinedType):
    def get_col_spec(self):
        return "GEOMETRY"


KUBLING_TYPE_MAP = {
    "string": String,
    "bigdecimal": Numeric,
    "biginteger": BigInteger,
    "blob": LargeBinary,
    "boolean": Boolean,
    "byte": SmallInteger,
    "char": lambda length=1: String(length),  # Default to length=1 for CHAR
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
    "timestamp": DateTime
}


def map_kubling_type(kubling_type, **kwargs):
    if kubling_type in KUBLING_TYPE_MAP:
        type_def = KUBLING_TYPE_MAP[kubling_type]
        return type_def(**kwargs) if callable(type_def) else type_def
    raise ValueError(f"Unsupported Kubling type: {kubling_type}")


class KublingDialect(PGDialect):
    name = "kubling"
    driver = "psycopg2"

    @classmethod
    def dbapi(cls):
        """
        Import and return the DBAPI module for Kubling.
        """
        try:
            import psycopg2
            return psycopg2
        except ImportError as e:
            raise ImportError(
                "The 'psycopg2' package is required for the Kubling dialect. "
                "Install it using 'pip install psycopg2-binary'."
            ) from e

    def create_connect_args(self, url):
        """
        Construct connection arguments from the SQLAlchemy URL.
        """
        # Extract parameters from the URL
        kwargs = {
            "dbname": url.database,
            "user": url.username,  # Correct parameter
            "password": url.password,
            "host": url.host,
            "port": url.port or 35432,  # Default port for Kubling
        }

        # Remove None values
        kwargs = {key: value for key, value in kwargs.items() if value is not None}

        # Return the args and kwargs for psycopg2.connect
        logging.debug(f"Generated connection kwargs: {kwargs}")
        return [], kwargs

    @reflection.cache
    def get_schema_names(self, connection, **kw):
        query = text("""
                    SELECT Name
                    FROM SYS.SCHEMAS
                    """)
        result = connection.execute(query)
        return [row[0] for row in result]

    @reflection.cache
    def get_table_names(self, connection, schema=None, **kwargs):
        logging.info(f"get_table_names called with schema: {schema}")
        # Override to fetch tables from Kubling-specific system tables
        query = text("""
                    SELECT Name
                    FROM SYS.Tables
                    WHERE SchemaName = :schema
                    """)
        result = connection.execute(query, {"schema": schema or "public"})
        return [row[0] for row in result]

    @reflection.cache
    def get_columns(self, connection, table_name, schema=None, **kwargs):
        # Override to fetch column details from Kubling system tables
        query = text("""
        SELECT Name, DataType, (CASE WHEN NullType = 'Nullable' THEN true ELSE false END) as nullable
                FROM SYS.COLUMNS 
                WHERE TableName = :table_name AND SchemaName = :schema
        """)
        result = connection.execute(query, {"schema": schema or "public", "table_name": table_name}).mappings()
        columns = []
        for row in result:
            kubling_type = row["DataType"]
            sqlalchemy_type = map_kubling_type(kubling_type)
            columns.append({
                "name": row["Name"],
                "type": sqlalchemy_type,
                "nullable": row["nullable"],
            })
        return columns

    @reflection.cache
    def get_view_names(self, connection, schema=None, **kw):
        query = text("""
            SELECT Name
            FROM SYSADMIN.VIEWS
            WHERE SchemaName = :schema
            """)
        result = connection.execute(query, {"schema": schema or "public"})
        return [row[0] for row in result]

    @reflection.cache
    def get_foreign_keys(self, connection, table_name, schema=None, **kw):
        query = text("""
            SELECT
                sk.name as keyName,
                ss.name as SchemaName,
                st.name as TableName,
                sk.colNames as ColNames
            FROM SYS.KEYS sk
                JOIN SYS.SCHEMAS ss on ss.UID = sk.RefSchemaUID
                JOIN SYS.TABLES st on st.UID = sk.RefTableUID
            WHERE sk.TYPE = 'Foreign'
            AND sk.TableName = :table_name
            AND sk.SchemaName = :schema
            """)
        result = connection.execute(query, {"table_name": table_name, "schema": schema or "public"})

        foreign_keys = []
        for row in result.mappings():
            foreign_keys.append({
                "name": row["keyName"],
                "constrained_columns": row["ColNames"],
                "referred_schema": row["SchemaName"],
                "referred_table": row["TableName"],
                "referred_columns": row["ColNames"],
            })
        return foreign_keys

    @reflection.cache
    def get_indexes(self, connection, table_name, schema=None, **kw):
        return []

    @reflection.cache
    def get_temp_table_names(self, connection, schema=None, **kw):
        return []

    @reflection.cache
    def get_temp_view_names(self, connection, schema=None, **kw):
        return []

    @reflection.cache
    def get_check_constraints(self, connection, table_name, schema=None, **kw):
        return []

    @reflection.cache
    def get_pk_constraint(self, connection, table_name, schema=None, **kw):
        pk_query = text("""
            SELECT Name, ColPositions
            FROM SYS.Keys
            WHERE Type = 'Primary'
              AND TableName = :table_name
              AND SchemaName = :schema
        """)
        pk_result = connection.execute(pk_query, {
            "table_name": table_name,
            "schema": schema or "public"
        }).mappings()

        pk_row = next(pk_result, None)
        if not pk_row:
            return {"constrained_columns": [], "name": None}

        col_positions = pk_row["ColPositions"]
        if not col_positions:
            return {"constrained_columns": [], "name": pk_row["Name"]}

        col_query = text("""
            SELECT Name
            FROM SYS.Columns
            WHERE TableName = :table_name AND SchemaName = :schema
        """)
        col_result = connection.execute(col_query, {
            "table_name": table_name,
            "schema": schema or "public"
        }).mappings()

        column_names = [row["Name"] for row in col_result]
        pos_to_name = {i + 1: name for i, name in enumerate(column_names)}

        constrained_columns = [
            pos_to_name.get(pos) for pos in col_positions if pos in pos_to_name
        ]

        return {
            "constrained_columns": constrained_columns,
            "name": pk_row["Name"]
        }

    @reflection.cache
    def get_unique_constraints(self, connection, table_name, schema=None, **kw):
        query = text("""
            SELECT Name, ColPositions
            FROM SYS.Keys
            WHERE Type = 'Unique'
              AND TableName = :table_name
              AND SchemaName = :schema
        """)
        result = connection.execute(query, {
            "table_name": table_name,
            "schema": schema or "public"
        }).mappings()

        col_query = text("""
            SELECT Name
            FROM SYS.Columns
            WHERE TableName = :table_name AND SchemaName = :schema
        """)
        col_result = connection.execute(col_query, {
            "table_name": table_name,
            "schema": schema or "public"
        }).mappings()
        column_names = [row["Name"] for row in col_result]
        pos_to_name = {i + 1: name for i, name in enumerate(column_names)}

        constraints = []
        for row in result:
            cols = [pos_to_name.get(pos) for pos in row["ColPositions"] if pos in pos_to_name]
            constraints.append({
                "name": row["Name"],
                "column_names": cols,
            })
        return constraints

    @reflection.cache
    def get_table_comment(self, connection, table_name, schema=None, **kw):
        query = text("""
            SELECT Description
            FROM SYS.Tables
            WHERE Name = :table_name AND SchemaName = :schema
        """)
        result = connection.execute(query, {
            "table_name": table_name,
            "schema": schema or "public"
        }).mappings()
        row = next(result, None)
        return {"text": row["Description"] if row and row["Description"] else None}

    def has_table(self, connection, table_name, schema=None):
        query = text("""
            SELECT 1
            FROM SYS.Tables
            WHERE Name = :table_name AND SchemaName = :schema
        """)
        result = connection.execute(query, {
            "table_name": table_name,
            "schema": schema or "public"
        }).scalar()
        return bool(result)


# Register the Kubling dialect
from sqlalchemy.dialects import registry

registry.register("kubling", "kubling.dialect", "KublingDialect")
