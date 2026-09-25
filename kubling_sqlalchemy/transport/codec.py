"""Loss-aware conversion between Python values and Kubling protobuf values."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

from kubling import features
from kubling.v1 import command_pb2, value_pb2

from .errors import CodecError


_LOCAL_TIME = re.compile(
    r"^(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d):(?P<second>[0-5]\d)"
    r"(?:\.(?P<fraction>\d{1,9}))?$"
)
_LOCAL_TIMESTAMP = re.compile(
    r"^(?P<day>\d{4}-\d{2}-\d{2})T(?P<clock>.+)$"
)


@dataclass(frozen=True, slots=True)
class KublingChar:
    value: str

    def __post_init__(self) -> None:
        if (
            len(self.value) != 1
            or ord(self.value) > 0xFFFF
            or 0xD800 <= ord(self.value) <= 0xDFFF
        ):
            raise CodecError("CHAR requires one Unicode BMP scalar value")


@dataclass(frozen=True, slots=True)
class KublingJson:
    """A JSON document whose original text is retained exactly."""

    text: str

    def __post_init__(self) -> None:
        try:
            json.loads(self.text)
        except (TypeError, ValueError) as exc:
            raise CodecError("invalid JSON document") from exc

    @classmethod
    def from_value(cls, value: Any) -> "KublingJson":
        try:
            return cls(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
        except (TypeError, ValueError) as exc:
            raise CodecError("value is not JSON serializable") from exc

    def to_python(self) -> Any:
        return json.loads(self.text)


@dataclass(frozen=True, slots=True)
class KublingXml:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise CodecError("XML requires text")


@dataclass(frozen=True, slots=True)
class KublingBlob:
    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise CodecError("BLOB requires bytes")


@dataclass(frozen=True, slots=True)
class KublingClob:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise CodecError("CLOB requires text")


@dataclass(frozen=True, slots=True)
class KublingLocalTime:
    """An exact local time, retaining up to the protocol's nanosecond precision."""

    text: str

    def __post_init__(self) -> None:
        _parse_local_time(self.text)

    @classmethod
    def from_time(cls, value: time) -> "KublingLocalTime":
        if value.utcoffset() is not None:
            raise CodecError("Kubling TIME has no timezone")
        return cls(value.replace(tzinfo=None).isoformat())

    def to_time(self) -> time:
        match = _parse_local_time(self.text)
        nanoseconds = int((match.group("fraction") or "").ljust(9, "0"))
        if nanoseconds % 1_000:
            raise CodecError("TIME precision exceeds Python microseconds")
        return time(
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            nanoseconds // 1_000,
        )


@dataclass(frozen=True, slots=True)
class KublingLocalTimestamp:
    """An exact local timestamp without an inferred timezone."""

    text: str

    def __post_init__(self) -> None:
        _parse_local_timestamp(self.text)

    @classmethod
    def from_datetime(cls, value: datetime) -> "KublingLocalTimestamp":
        if value.utcoffset() is not None:
            raise CodecError("Kubling TIMESTAMP has no timezone")
        return cls(value.replace(tzinfo=None).isoformat())

    def to_datetime(self) -> datetime:
        day, clock = _parse_local_timestamp(self.text)
        return datetime.combine(day, KublingLocalTime(clock).to_time())


@dataclass(frozen=True, slots=True)
class KublingSpatial:
    """Plain WKB with optional CRS information; missing SRID remains unknown."""

    wkb: bytes
    geography: bool = False
    srid: int | None = None
    crs: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.wkb, bytes):
            raise CodecError("spatial WKB must be bytes")
        if not self.wkb:
            raise CodecError("spatial WKB cannot be empty")
        if self.srid is not None and not -(2**31) <= self.srid < 2**31:
            raise CodecError("spatial SRID must fit a signed 32-bit integer")
        if self.crs == "":
            raise CodecError("CRS must be absent or non-empty")


@dataclass(frozen=True, slots=True)
class KublingLobReference:
    lob_id: str
    type: int
    session_id: str
    size_bytes: int | None = None
    expires_at_unix_ms: int | None = None

    def __post_init__(self) -> None:
        if not self.lob_id or not self.session_id:
            raise CodecError("LOB references require non-empty LOB and session IDs")
        if self.type not in (value_pb2.VALUE_TYPE_BLOB, value_pb2.VALUE_TYPE_CLOB):
            raise CodecError("LOB references can only describe BLOB or CLOB")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise CodecError("LOB size cannot be negative")
        if self.expires_at_unix_ms is None or self.expires_at_unix_ms <= 0:
            raise CodecError("LOB references require a positive expiry")


@dataclass(frozen=True, slots=True)
class KublingArray:
    elements: tuple[Any, ...]
    element_type: value_pb2.TypeDescriptor

    def __init__(
        self,
        elements: Iterable[Any],
        element_type: value_pb2.TypeDescriptor,
    ) -> None:
        descriptor = copy_type_descriptor(element_type)
        validate_type_descriptor(descriptor)
        object.__setattr__(self, "elements", tuple(elements))
        object.__setattr__(self, "element_type", descriptor)


@dataclass(frozen=True, slots=True)
class BoundParameter:
    value: Any
    declared_type: value_pb2.TypeDescriptor | None = None


def type_descriptor(
    value_type: int,
    *,
    element_type: value_pb2.TypeDescriptor | None = None,
    precision: int | None = None,
    scale: int | None = None,
) -> value_pb2.TypeDescriptor:
    descriptor = value_pb2.TypeDescriptor(type=value_type)
    if element_type is not None:
        descriptor.element_type.CopyFrom(element_type)
    if precision is not None:
        descriptor.precision = precision
    if scale is not None:
        descriptor.scale = scale
    validate_type_descriptor(descriptor)
    return descriptor


def copy_type_descriptor(
    descriptor: value_pb2.TypeDescriptor,
) -> value_pb2.TypeDescriptor:
    copied = value_pb2.TypeDescriptor()
    copied.CopyFrom(descriptor)
    return copied


def validate_type_descriptor(descriptor: value_pb2.TypeDescriptor) -> None:
    value_type = descriptor.type
    if value_type not in value_pb2.ValueType.values():
        raise CodecError(f"unknown declared type number: {value_type}")
    if value_type == value_pb2.VALUE_TYPE_UNKNOWN:
        raise CodecError("UNKNOWN is not a valid declared type")

    is_array = value_type == value_pb2.VALUE_TYPE_ARRAY
    if is_array != descriptor.HasField("element_type"):
        requirement = "requires" if is_array else "cannot contain"
        raise CodecError(f"declared type {value_pb2.ValueType.Name(value_type)} {requirement} element_type")
    if is_array:
        validate_type_descriptor(descriptor.element_type)

    has_precision = descriptor.HasField("precision")
    has_scale = descriptor.HasField("scale")
    if has_precision and descriptor.precision <= 0:
        raise CodecError("precision must be positive")
    if has_precision and value_type not in (
        value_pb2.VALUE_TYPE_BIGINTEGER,
        value_pb2.VALUE_TYPE_BIGDECIMAL,
    ):
        raise CodecError("precision is only valid for BIGINTEGER or BIGDECIMAL")
    if has_scale:
        if value_type != value_pb2.VALUE_TYPE_BIGDECIMAL or not has_precision:
            raise CodecError("scale requires BIGDECIMAL with precision")
        if descriptor.scale > descriptor.precision:
            raise CodecError("scale cannot exceed precision")


def descriptor_dimensions(descriptor: value_pb2.TypeDescriptor) -> int:
    validate_type_descriptor(descriptor)
    dimensions = 0
    current = descriptor
    while current.type == value_pb2.VALUE_TYPE_ARRAY:
        dimensions += 1
        current = current.element_type
    return dimensions


def encode_parameter(
    value: Any,
    declared_type: value_pb2.TypeDescriptor | None = None,
) -> command_pb2.Parameter:
    if isinstance(value, BoundParameter):
        if declared_type is not None:
            raise CodecError("declared type was supplied twice")
        declared_type = value.declared_type
        value = value.value
    encoded = encode_value(value, declared_type)
    if declared_type is None:
        declared_type = infer_type_descriptor(encoded)
    parameter = command_pb2.Parameter(value=encoded)
    if declared_type is not None:
        validate_type_descriptor(declared_type)
        parameter.declared_type.CopyFrom(declared_type)
    return parameter


def encode_value(
    value: Any,
    declared_type: value_pb2.TypeDescriptor | None = None,
) -> value_pb2.Value:
    if declared_type is not None:
        validate_type_descriptor(declared_type)
    if value is None:
        return value_pb2.Value(null_value=value_pb2.NullValue())
    if declared_type is not None:
        return _encode_declared(value, declared_type)

    if isinstance(value, KublingChar):
        return value_pb2.Value(char_value=value.value)
    if isinstance(value, bool):
        return value_pb2.Value(boolean_value=value)
    if isinstance(value, int):
        if -(2**31) <= value < 2**31:
            return value_pb2.Value(integer_value=value)
        if -(2**63) <= value < 2**63:
            return value_pb2.Value(long_value=value)
        return value_pb2.Value(biginteger_value=str(value))
    if isinstance(value, float):
        return value_pb2.Value(double_value=value)
    if isinstance(value, Decimal):
        return value_pb2.Value(bigdecimal_value=_decimal_text(value))
    if isinstance(value, datetime):
        return value_pb2.Value(
            timestamp_value=KublingLocalTimestamp.from_datetime(value).text
        )
    if isinstance(value, date):
        return value_pb2.Value(date_value=value.isoformat())
    if isinstance(value, time):
        return value_pb2.Value(time_value=KublingLocalTime.from_time(value).text)
    if isinstance(value, KublingLocalTimestamp):
        return value_pb2.Value(timestamp_value=value.text)
    if isinstance(value, KublingLocalTime):
        return value_pb2.Value(time_value=value.text)
    if isinstance(value, str):
        return value_pb2.Value(string_value=value)
    if isinstance(value, bytes):
        return value_pb2.Value(varbinary_value=value)
    if isinstance(value, KublingJson):
        return value_pb2.Value(json_value=value.text)
    if isinstance(value, KublingXml):
        return value_pb2.Value(xml_value=value.text)
    if isinstance(value, KublingBlob):
        return value_pb2.Value(blob_value=value_pb2.BlobValue(data=value.data))
    if isinstance(value, KublingClob):
        return value_pb2.Value(clob_value=value_pb2.ClobValue(data=value.text))
    if isinstance(value, KublingSpatial):
        return _encode_spatial(value)
    if isinstance(value, KublingArray):
        descriptor = type_descriptor(
            value_pb2.VALUE_TYPE_ARRAY,
            element_type=value.element_type,
        )
        return _encode_declared(value, descriptor)
    if isinstance(value, KublingLobReference):
        return value_pb2.Value(lob_reference=_encode_lob_reference(value))
    raise CodecError(f"unsupported Python value: {type(value).__name__}")


def decode_value(
    value: value_pb2.Value,
    declared_type: value_pb2.TypeDescriptor | None = None,
) -> Any:
    kind = value.WhichOneof("kind")
    if kind is None:
        raise CodecError("Value has no selected kind")
    if declared_type is not None:
        validate_type_descriptor(declared_type)
        _validate_wire_type(value, declared_type)
    if kind == "null_value":
        return None
    if kind == "string_value":
        return value.string_value
    if kind == "varbinary_value":
        return value.varbinary_value
    if kind == "char_value":
        return KublingChar(value.char_value)
    if kind == "boolean_value":
        return value.boolean_value
    if kind == "byte_value":
        return _checked_integer(value.byte_value, -128, 127, "BYTE")
    if kind == "short_value":
        return _checked_integer(value.short_value, -(2**15), 2**15 - 1, "SHORT")
    if kind == "integer_value":
        return value.integer_value
    if kind == "long_value":
        return value.long_value
    if kind == "biginteger_value":
        try:
            return int(value.biginteger_value)
        except ValueError as exc:
            raise CodecError("invalid BIGINTEGER value") from exc
    if kind in ("float_value", "double_value"):
        return getattr(value, kind)
    if kind == "bigdecimal_value":
        try:
            decoded = Decimal(value.bigdecimal_value)
        except InvalidOperation as exc:
            raise CodecError("invalid BIGDECIMAL value") from exc
        if not decoded.is_finite():
            raise CodecError("BIGDECIMAL must be finite")
        return decoded
    if kind == "date_value":
        try:
            return date.fromisoformat(value.date_value)
        except ValueError as exc:
            raise CodecError("invalid DATE value") from exc
    if kind == "time_value":
        return KublingLocalTime(value.time_value)
    if kind == "timestamp_value":
        return KublingLocalTimestamp(value.timestamp_value)
    if kind == "blob_value":
        return KublingBlob(value.blob_value.data)
    if kind == "clob_value":
        return KublingClob(value.clob_value.data)
    if kind == "xml_value":
        return KublingXml(value.xml_value)
    if kind == "json_value":
        return KublingJson(value.json_value)
    if kind in ("geometry_value", "geography_value"):
        return KublingSpatial(
            getattr(value, kind),
            geography=kind == "geography_value",
        )
    if kind in ("geometry_with_crs", "geography_with_crs"):
        spatial = getattr(value, kind)
        return KublingSpatial(
            spatial.wkb,
            geography=kind == "geography_with_crs",
            srid=spatial.srid if spatial.HasField("srid") else None,
            crs=spatial.crs if spatial.HasField("crs") else None,
        )
    if kind == "array_value":
        validate_type_descriptor(
            type_descriptor(
                value_pb2.VALUE_TYPE_ARRAY,
                element_type=value.array_value.element_type,
            )
        )
        return KublingArray(
            (
                decode_value(element, value.array_value.element_type)
                for element in value.array_value.elements
            ),
            value.array_value.element_type,
        )
    if kind == "lob_reference":
        reference = value.lob_reference
        return KublingLobReference(
            lob_id=reference.lob_id,
            type=reference.type,
            session_id=reference.session_id,
            size_bytes=(reference.size_bytes if reference.HasField("size_bytes") else None),
            expires_at_unix_ms=(
                reference.expires_at_unix_ms
                if reference.HasField("expires_at_unix_ms")
                else None
            ),
        )
    raise CodecError(f"unsupported protobuf value kind: {kind}")


def required_features_for_parameter(parameter: command_pb2.Parameter) -> frozenset[str]:
    required: set[str] = set()
    if parameter.HasField("declared_type"):
        validate_type_descriptor(parameter.declared_type)
        required.add(features.TYPED_PARAMETERS_V1)
    _collect_value_features(parameter.value, required)
    return frozenset(required)


def infer_type_descriptor(
    value: value_pb2.Value,
) -> value_pb2.TypeDescriptor | None:
    """Infer the protocol type carried by a non-null wire value."""

    kind = value.WhichOneof("kind")
    if kind in (None, "null_value"):
        return None
    if kind == "array_value":
        return type_descriptor(
            value_pb2.VALUE_TYPE_ARRAY,
            element_type=value.array_value.element_type,
        )
    if kind == "lob_reference":
        return type_descriptor(value.lob_reference.type)
    value_type = {
        "string_value": value_pb2.VALUE_TYPE_STRING,
        "varbinary_value": value_pb2.VALUE_TYPE_VARBINARY,
        "char_value": value_pb2.VALUE_TYPE_CHAR,
        "boolean_value": value_pb2.VALUE_TYPE_BOOLEAN,
        "byte_value": value_pb2.VALUE_TYPE_BYTE,
        "short_value": value_pb2.VALUE_TYPE_SHORT,
        "integer_value": value_pb2.VALUE_TYPE_INTEGER,
        "long_value": value_pb2.VALUE_TYPE_LONG,
        "biginteger_value": value_pb2.VALUE_TYPE_BIGINTEGER,
        "float_value": value_pb2.VALUE_TYPE_FLOAT,
        "double_value": value_pb2.VALUE_TYPE_DOUBLE,
        "bigdecimal_value": value_pb2.VALUE_TYPE_BIGDECIMAL,
        "date_value": value_pb2.VALUE_TYPE_DATE,
        "time_value": value_pb2.VALUE_TYPE_TIME,
        "timestamp_value": value_pb2.VALUE_TYPE_TIMESTAMP,
        "blob_value": value_pb2.VALUE_TYPE_BLOB,
        "clob_value": value_pb2.VALUE_TYPE_CLOB,
        "geometry_value": value_pb2.VALUE_TYPE_GEOMETRY,
        "geometry_with_crs": value_pb2.VALUE_TYPE_GEOMETRY,
        "geography_value": value_pb2.VALUE_TYPE_GEOGRAPHY,
        "geography_with_crs": value_pb2.VALUE_TYPE_GEOGRAPHY,
        "json_value": value_pb2.VALUE_TYPE_JSON,
        "xml_value": value_pb2.VALUE_TYPE_XML,
    }.get(kind)
    if value_type is None:
        raise CodecError(f"unsupported protobuf value kind: {kind}")
    return type_descriptor(value_type)


def _encode_declared(
    value: Any,
    descriptor: value_pb2.TypeDescriptor,
) -> value_pb2.Value:
    value_type = descriptor.type
    if value_type == value_pb2.VALUE_TYPE_STRING:
        return value_pb2.Value(string_value=_require(value, str, "STRING"))
    if value_type == value_pb2.VALUE_TYPE_VARBINARY:
        return value_pb2.Value(varbinary_value=_require(value, bytes, "VARBINARY"))
    if value_type == value_pb2.VALUE_TYPE_CHAR:
        if not isinstance(value, (str, KublingChar)):
            raise CodecError("CHAR requires str or KublingChar")
        char = value if isinstance(value, KublingChar) else KublingChar(value)
        return value_pb2.Value(char_value=char.value)
    if value_type == value_pb2.VALUE_TYPE_BOOLEAN:
        if type(value) is not bool:
            raise CodecError("BOOLEAN requires bool")
        return value_pb2.Value(boolean_value=value)
    integer_fields = {
        value_pb2.VALUE_TYPE_BYTE: ("byte_value", -128, 127),
        value_pb2.VALUE_TYPE_SHORT: ("short_value", -(2**15), 2**15 - 1),
        value_pb2.VALUE_TYPE_INTEGER: ("integer_value", -(2**31), 2**31 - 1),
        value_pb2.VALUE_TYPE_LONG: ("long_value", -(2**63), 2**63 - 1),
    }
    if value_type in integer_fields:
        if type(value) is not int:
            raise CodecError(f"{value_pb2.ValueType.Name(value_type)} requires int")
        field, minimum, maximum = integer_fields[value_type]
        _checked_integer(value, minimum, maximum, value_pb2.ValueType.Name(value_type))
        return value_pb2.Value(**{field: value})
    if value_type == value_pb2.VALUE_TYPE_BIGINTEGER:
        if type(value) is not int:
            raise CodecError("BIGINTEGER requires int")
        return value_pb2.Value(biginteger_value=str(value))
    if value_type in (value_pb2.VALUE_TYPE_FLOAT, value_pb2.VALUE_TYPE_DOUBLE):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CodecError(f"{value_pb2.ValueType.Name(value_type)} requires a number")
        field = "float_value" if value_type == value_pb2.VALUE_TYPE_FLOAT else "double_value"
        return value_pb2.Value(**{field: float(value)})
    if value_type == value_pb2.VALUE_TYPE_BIGDECIMAL:
        if not isinstance(value, Decimal):
            raise CodecError("BIGDECIMAL requires Decimal")
        return value_pb2.Value(bigdecimal_value=_decimal_text(value))
    if value_type == value_pb2.VALUE_TYPE_DATE:
        if isinstance(value, datetime) or not isinstance(value, date):
            raise CodecError("DATE requires datetime.date")
        return value_pb2.Value(date_value=value.isoformat())
    if value_type == value_pb2.VALUE_TYPE_TIME:
        if not isinstance(value, (time, KublingLocalTime)):
            raise CodecError("TIME requires datetime.time or KublingLocalTime")
        local = value if isinstance(value, KublingLocalTime) else KublingLocalTime.from_time(value)
        return value_pb2.Value(time_value=local.text)
    if value_type == value_pb2.VALUE_TYPE_TIMESTAMP:
        if not isinstance(value, (datetime, KublingLocalTimestamp)):
            raise CodecError(
                "TIMESTAMP requires datetime.datetime or KublingLocalTimestamp"
            )
        local = (
            value
            if isinstance(value, KublingLocalTimestamp)
            else KublingLocalTimestamp.from_datetime(value)
        )
        return value_pb2.Value(timestamp_value=local.text)
    if value_type == value_pb2.VALUE_TYPE_BLOB:
        if isinstance(value, KublingLobReference):
            return value_pb2.Value(lob_reference=_encode_lob_reference(value))
        data = value.data if isinstance(value, KublingBlob) else _require(value, bytes, "BLOB")
        return value_pb2.Value(blob_value=value_pb2.BlobValue(data=data))
    if value_type == value_pb2.VALUE_TYPE_CLOB:
        if isinstance(value, KublingLobReference):
            return value_pb2.Value(lob_reference=_encode_lob_reference(value))
        text = value.text if isinstance(value, KublingClob) else _require(value, str, "CLOB")
        return value_pb2.Value(clob_value=value_pb2.ClobValue(data=text))
    if value_type in (value_pb2.VALUE_TYPE_GEOMETRY, value_pb2.VALUE_TYPE_GEOGRAPHY):
        if isinstance(value, bytes):
            value = KublingSpatial(
                value,
                geography=value_type == value_pb2.VALUE_TYPE_GEOGRAPHY,
            )
        if not isinstance(value, KublingSpatial):
            raise CodecError("spatial types require KublingSpatial or WKB bytes")
        if value.geography != (value_type == value_pb2.VALUE_TYPE_GEOGRAPHY):
            raise CodecError("spatial value does not match its declared type")
        return _encode_spatial(value)
    if value_type == value_pb2.VALUE_TYPE_JSON:
        document = value if isinstance(value, KublingJson) else KublingJson.from_value(value)
        return value_pb2.Value(json_value=document.text)
    if value_type == value_pb2.VALUE_TYPE_XML:
        text = value.text if isinstance(value, KublingXml) else _require(value, str, "XML")
        return value_pb2.Value(xml_value=text)
    if value_type == value_pb2.VALUE_TYPE_ARRAY:
        if isinstance(value, KublingArray):
            if value.element_type != descriptor.element_type:
                raise CodecError("array element type differs from its declared type")
            elements: Sequence[Any] = value.elements
        elif isinstance(value, (list, tuple)):
            elements = value
        else:
            raise CodecError("ARRAY requires KublingArray, list or tuple")
        array = value_pb2.ArrayValue()
        array.element_type.CopyFrom(descriptor.element_type)
        array.elements.extend(
            encode_value(element, descriptor.element_type) for element in elements
        )
        return value_pb2.Value(array_value=array)
    raise CodecError(f"unsupported declared type: {value_pb2.ValueType.Name(value_type)}")


def _encode_spatial(value: KublingSpatial) -> value_pb2.Value:
    spatial = value_pb2.SpatialValue(wkb=value.wkb)
    if value.srid is not None:
        spatial.srid = value.srid
    if value.crs is not None:
        spatial.crs = value.crs
    field = "geography_with_crs" if value.geography else "geometry_with_crs"
    return value_pb2.Value(**{field: spatial})


def _encode_lob_reference(value: KublingLobReference) -> value_pb2.LobReference:
    reference = value_pb2.LobReference(
        lob_id=value.lob_id,
        type=value.type,
        session_id=value.session_id,
        expires_at_unix_ms=value.expires_at_unix_ms,
    )
    if value.size_bytes is not None:
        reference.size_bytes = value.size_bytes
    return reference


def _validate_wire_type(
    value: value_pb2.Value,
    descriptor: value_pb2.TypeDescriptor,
) -> None:
    kind = value.WhichOneof("kind")
    if kind == "null_value":
        return
    expected = {
        value_pb2.VALUE_TYPE_STRING: {"string_value"},
        value_pb2.VALUE_TYPE_VARBINARY: {"varbinary_value"},
        value_pb2.VALUE_TYPE_CHAR: {"char_value"},
        value_pb2.VALUE_TYPE_BOOLEAN: {"boolean_value"},
        value_pb2.VALUE_TYPE_BYTE: {"byte_value"},
        value_pb2.VALUE_TYPE_SHORT: {"short_value"},
        value_pb2.VALUE_TYPE_INTEGER: {"integer_value"},
        value_pb2.VALUE_TYPE_LONG: {"long_value"},
        value_pb2.VALUE_TYPE_BIGINTEGER: {"biginteger_value"},
        value_pb2.VALUE_TYPE_FLOAT: {"float_value"},
        value_pb2.VALUE_TYPE_DOUBLE: {"double_value"},
        value_pb2.VALUE_TYPE_BIGDECIMAL: {"bigdecimal_value"},
        value_pb2.VALUE_TYPE_DATE: {"date_value"},
        value_pb2.VALUE_TYPE_TIME: {"time_value"},
        value_pb2.VALUE_TYPE_TIMESTAMP: {"timestamp_value"},
        value_pb2.VALUE_TYPE_BLOB: {"blob_value", "lob_reference"},
        value_pb2.VALUE_TYPE_CLOB: {"clob_value", "lob_reference"},
        value_pb2.VALUE_TYPE_GEOMETRY: {"geometry_value", "geometry_with_crs"},
        value_pb2.VALUE_TYPE_GEOGRAPHY: {"geography_value", "geography_with_crs"},
        value_pb2.VALUE_TYPE_JSON: {"json_value"},
        value_pb2.VALUE_TYPE_XML: {"xml_value"},
        value_pb2.VALUE_TYPE_ARRAY: {"array_value"},
    }.get(descriptor.type, set())
    if kind not in expected:
        raise CodecError(
            f"wire value {kind} does not match {value_pb2.ValueType.Name(descriptor.type)}"
        )
    if kind == "lob_reference" and value.lob_reference.type != descriptor.type:
        raise CodecError("LOB reference type differs from the declared column type")
    if kind == "array_value" and value.array_value.element_type != descriptor.element_type:
        raise CodecError("array element descriptor differs from the declared column type")


def _collect_value_features(value: value_pb2.Value, required: set[str]) -> None:
    kind = value.WhichOneof("kind")
    if kind == "array_value":
        required.add(features.ARRAY_VALUES_V1)
        for element in value.array_value.elements:
            _collect_value_features(element, required)
    elif kind in ("geometry_with_crs", "geography_with_crs"):
        required.add(features.SPATIAL_VALUES_V1)
    elif kind == "lob_reference":
        required.add(features.LOB_WRITE_V1)


def _parse_local_time(text: str) -> re.Match[str]:
    if not isinstance(text, str):
        raise CodecError("TIME requires text")
    match = _LOCAL_TIME.fullmatch(text)
    if match is None:
        raise CodecError("TIME must use HH:mm:ss[.n] without a timezone")
    return match


def _parse_local_timestamp(text: str) -> tuple[date, str]:
    if not isinstance(text, str):
        raise CodecError("TIMESTAMP requires text")
    match = _LOCAL_TIMESTAMP.fullmatch(text)
    if match is None:
        raise CodecError("TIMESTAMP must use yyyy-MM-ddTHH:mm:ss[.n]")
    try:
        day = date.fromisoformat(match.group("day"))
    except ValueError as exc:
        raise CodecError("invalid TIMESTAMP date") from exc
    clock = match.group("clock")
    _parse_local_time(clock)
    return day, clock


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise CodecError("BIGDECIMAL must be finite")
    return str(value)


def _checked_integer(value: int, minimum: int, maximum: int, name: str) -> int:
    if value < minimum or value > maximum:
        raise CodecError(f"{name} is outside [{minimum}, {maximum}]")
    return value


def _require(value: Any, expected: type, name: str):
    if not isinstance(value, expected):
        raise CodecError(f"{name} requires {expected.__name__}")
    return value


__all__ = [
    "BoundParameter",
    "KublingArray",
    "KublingBlob",
    "KublingChar",
    "KublingClob",
    "KublingJson",
    "KublingLobReference",
    "KublingLocalTime",
    "KublingLocalTimestamp",
    "KublingSpatial",
    "KublingXml",
    "copy_type_descriptor",
    "decode_value",
    "descriptor_dimensions",
    "encode_parameter",
    "encode_value",
    "infer_type_descriptor",
    "required_features_for_parameter",
    "type_descriptor",
    "validate_type_descriptor",
]
