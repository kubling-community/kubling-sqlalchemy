from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
from kubling import features
from kubling.v1 import value_pb2

from kubling_sqlalchemy.transport import (
    CodecError,
    KublingArray,
    KublingBlob,
    KublingChar,
    KublingClob,
    KublingJson,
    KublingLobReference,
    KublingLocalTime,
    KublingLocalTimestamp,
    KublingSpatial,
    KublingXml,
    decode_value,
    encode_parameter,
    encode_value,
    infer_type_descriptor,
    type_descriptor,
)
from kubling_sqlalchemy.transport.codec import required_features_for_parameter


@pytest.mark.parametrize(
    ("source", "kind", "decoded"),
    [
        (None, "null_value", None),
        ("hello", "string_value", "hello"),
        (b"\x00\xff", "varbinary_value", b"\x00\xff"),
        (KublingChar("ñ"), "char_value", KublingChar("ñ")),
        (True, "boolean_value", True),
        (2**31 - 1, "integer_value", 2**31 - 1),
        (2**31, "long_value", 2**31),
        (2**80, "biginteger_value", 2**80),
        (1.25, "double_value", 1.25),
        (Decimal("1.2300E+40"), "bigdecimal_value", Decimal("1.2300E+40")),
        (date(2026, 9, 18), "date_value", date(2026, 9, 18)),
        (
            time(12, 34, 56, 123456),
            "time_value",
            KublingLocalTime("12:34:56.123456"),
        ),
        (
            datetime(2026, 9, 18, 12, 34, 56, 123456),
            "timestamp_value",
            KublingLocalTimestamp("2026-09-18T12:34:56.123456"),
        ),
        (KublingJson('{"answer":42}'), "json_value", KublingJson('{"answer":42}')),
        (KublingXml("<answer>42</answer>"), "xml_value", KublingXml("<answer>42</answer>")),
        (KublingBlob(b"blob"), "blob_value", KublingBlob(b"blob")),
        (KublingClob("clob"), "clob_value", KublingClob("clob")),
    ],
)
def test_scalar_values_round_trip_without_type_loss(source, kind, decoded):
    encoded = encode_value(source)

    assert encoded.WhichOneof("kind") == kind
    assert decode_value(encoded) == decoded


def test_temporal_precision_is_retained_and_loss_is_explicit():
    timestamp = KublingLocalTimestamp("2026-09-18T12:34:56.123456789")
    encoded = encode_value(timestamp)

    assert decode_value(encoded) == timestamp
    with pytest.raises(CodecError, match="precision exceeds"):
        timestamp.to_datetime()
    with pytest.raises(CodecError, match="has no timezone"):
        encode_value(datetime.now(timezone.utc))


def test_declared_types_enforce_ranges_and_typed_null_presence():
    byte_type = type_descriptor(value_pb2.VALUE_TYPE_BYTE)
    typed_null = encode_parameter(None, byte_type)

    assert typed_null.HasField("declared_type")
    assert typed_null.value.WhichOneof("kind") == "null_value"
    assert required_features_for_parameter(typed_null) == {
        features.TYPED_PARAMETERS_V1
    }
    assert encode_parameter(127, byte_type).value.byte_value == 127
    with pytest.raises(CodecError, match="outside"):
        encode_parameter(128, byte_type)


@pytest.mark.parametrize(
    ("source", "expected_type"),
    [
        ("text", value_pb2.VALUE_TYPE_STRING),
        (b"bytes", value_pb2.VALUE_TYPE_VARBINARY),
        (KublingChar("x"), value_pb2.VALUE_TYPE_CHAR),
        (True, value_pb2.VALUE_TYPE_BOOLEAN),
        (1, value_pb2.VALUE_TYPE_INTEGER),
        (2**31, value_pb2.VALUE_TYPE_LONG),
        (2**80, value_pb2.VALUE_TYPE_BIGINTEGER),
        (1.5, value_pb2.VALUE_TYPE_DOUBLE),
        (Decimal("1.5"), value_pb2.VALUE_TYPE_BIGDECIMAL),
        (date(2026, 9, 20), value_pb2.VALUE_TYPE_DATE),
        (time(12, 30), value_pb2.VALUE_TYPE_TIME),
        (datetime(2026, 9, 20, 12, 30), value_pb2.VALUE_TYPE_TIMESTAMP),
        (KublingJson('{"ok":true}'), value_pb2.VALUE_TYPE_JSON),
        (KublingXml("<ok/>"), value_pb2.VALUE_TYPE_XML),
        (KublingBlob(b"blob"), value_pb2.VALUE_TYPE_BLOB),
        (KublingClob("clob"), value_pb2.VALUE_TYPE_CLOB),
    ],
)
def test_non_null_parameters_infer_declared_type(source, expected_type):
    parameter = encode_parameter(source)

    assert parameter.declared_type.type == expected_type
    assert infer_type_descriptor(parameter.value) == parameter.declared_type
    assert features.TYPED_PARAMETERS_V1 in required_features_for_parameter(parameter)


def test_null_requires_an_explicit_type_and_arrays_retain_their_element_type():
    assert not encode_parameter(None).HasField("declared_type")

    integer = type_descriptor(value_pb2.VALUE_TYPE_INTEGER)
    parameter = encode_parameter(KublingArray((1, None), integer))

    assert parameter.declared_type.type == value_pb2.VALUE_TYPE_ARRAY
    assert parameter.declared_type.element_type == integer


def test_array_spatial_and_lob_features_are_derived_from_wire_values():
    integer = type_descriptor(value_pb2.VALUE_TYPE_INTEGER)
    array_type = type_descriptor(value_pb2.VALUE_TYPE_ARRAY, element_type=integer)
    array_parameter = encode_parameter([1, None, 3], array_type)
    decoded = decode_value(array_parameter.value, array_type)

    assert decoded == KublingArray((1, None, 3), integer)
    assert required_features_for_parameter(array_parameter) == {
        features.TYPED_PARAMETERS_V1,
        features.ARRAY_VALUES_V1,
    }

    spatial = encode_parameter(
        KublingSpatial(b"wkb", srid=4326, crs="EPSG:4326"),
        type_descriptor(value_pb2.VALUE_TYPE_GEOMETRY),
    )
    assert decode_value(spatial.value).srid == 4326
    assert required_features_for_parameter(spatial) == {
        features.TYPED_PARAMETERS_V1,
        features.SPATIAL_VALUES_V1,
    }

    reference = KublingLobReference(
        lob_id="lob-1",
        type=value_pb2.VALUE_TYPE_BLOB,
        session_id="session-1",
        size_bytes=3,
        expires_at_unix_ms=2_000_000_000_000,
    )
    lob = encode_parameter(reference, type_descriptor(value_pb2.VALUE_TYPE_BLOB))
    assert decode_value(lob.value) == reference
    assert features.LOB_WRITE_V1 in required_features_for_parameter(lob)


def test_empty_array_keeps_its_element_type_and_differs_from_null():
    string = type_descriptor(value_pb2.VALUE_TYPE_STRING)
    array = KublingArray((), string)
    encoded = encode_value(array)

    assert encoded.WhichOneof("kind") == "array_value"
    assert len(encoded.array_value.elements) == 0
    assert encoded.array_value.element_type == string
    assert encode_value(None).WhichOneof("kind") == "null_value"


def test_invalid_type_descriptors_and_server_values_are_rejected():
    with pytest.raises(CodecError, match="UNKNOWN"):
        type_descriptor(value_pb2.VALUE_TYPE_UNKNOWN)
    with pytest.raises(CodecError, match="requires element_type"):
        type_descriptor(value_pb2.VALUE_TYPE_ARRAY)
    with pytest.raises(CodecError, match="does not match"):
        decode_value(
            value_pb2.Value(string_value="wrong"),
            type_descriptor(value_pb2.VALUE_TYPE_INTEGER),
        )
    with pytest.raises(CodecError, match="expiry"):
        KublingLobReference(
            lob_id="lob-1",
            type=value_pb2.VALUE_TYPE_BLOB,
            session_id="session-1",
        )
