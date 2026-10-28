"""The `ordered-v1` item key bytes are frozen: a change would re-key every stored row."""

from __future__ import annotations

import random

import pytest

from exomem import item_keys
from exomem import structured_collections as collections

# One row per field type and edge. The hex is the stored key; it is the contract, not a snapshot of output.
VECTORS = [
    ("date", [("date", "2024-03-01")], "01804d47"),
    ("date before the epoch", [("date", "1969-12-31")], "017fffff"),
    ("null", [("date", None)], "02"),
    ("datetime with an offset", [("datetime", "2024-03-01T07:45:00+01:00")], "0180061293b7003300803c"),
    ("datetime at UTC", [("datetime", "2024-03-01T06:45:00+00:00")], "0180061293b70033008000"),
    ("datetime without an offset", [("datetime", "2024-03-01T06:45:00")], "0180061293b70033000000"),
    ("integer zero", [("integer", 0)], "0114"),
    ("negative integer", [("integer", -3)], "0113fc"),
    ("integer of two bytes", [("integer", 256)], "01160100"),
    ("integer beyond 8 bytes", [("integer", 2**64)], "011d09010000000000000000"),
    ("negative integer beyond 8 bytes", [("integer", -(2**64))], "010bf6feffffffffffffffff"),
    ("JSON integer in a number field", [("number", 5)], "01c014000000000000011505"),
    ("JSON float in a number field", [("number", 5.0)], "01c01400000000000000"),
    ("negative float", [("number", -1.5)], "014007ffffffffffff00"),
    ("true", [("boolean", True)], "0101"),
    ("false", [("boolean", False)], "0100"),
    ("empty string", [("string", "")], "0100"),
    ("string with an embedded NUL", [("string", "a\x00b")], "016100ff6200"),
    ("string in decomposed Unicode", [("string", "cafe\u0301")], "01636166c3a900"),
    ("two fields", [("string", "a"), ("integer", -3)], "0161000113fc"),
]


@pytest.mark.parametrize(("components", "expected"), [pytest.param(c, e, id=i) for i, c, e in VECTORS])
def test_ordered_key_bytes_are_frozen(components, expected):
    assert item_keys.ordered_key(components, lambda: "unused").hex() == expected


def test_member_position_key_bytes_are_frozen():
    assert item_keys.member_position_key("ab" * 32, 7).hex() == "feabababababababababababababababab1507"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "14"), (1, "1501"), (-1, "13fe"), (255, "15ff"), (-255, "1300"), (256, "160100"), (-256, "12feff"),
     (65535, "16ffff"), (2**63, "1c8000000000000000"), (2**64 - 1, "1d08ffffffffffffffff"),
     (2**64, "1d09010000000000000000"), (-(2**64 - 1), "0bf70000000000000000")],
)
def test_integer_encoding_matches_the_tuple_layer_vectors(value, expected):
    assert item_keys.integer(value).hex() == expected


def test_key_over_the_limit_keeps_its_prefix_and_ends_in_the_natural_key_digest():
    key = item_keys.ordered_key([("string", "x" * 400)], lambda: "SERIAL")
    assert len(key) == item_keys.MAX_KEY_BYTES
    assert key[:288] == b"\x01" + b"x" * 287
    assert key[288:].hex() == "1f2f95810b05875aac27992c992772e62261ae74315f958657b974b8229fa9a4"
    assert len(item_keys.spell(key)) == 512


def test_a_null_before_a_string_with_embedded_nul_cannot_collide_with_a_longer_string():
    # With 0xFF as the null byte, these two natural keys encoded alike: 0xFF both escapes a NUL and marks null.
    string = "string"
    first = item_keys.ordered_key([(string, "a"), (string, None), (string, "p\x00\x01q")], lambda: "")
    second = item_keys.ordered_key([(string, "a\x00\x01p"), (string, None), (string, "q")], lambda: "")
    assert first != second


def test_a_key_with_no_field_value_is_refused():
    with pytest.raises(ValueError, match="at least one field"):
        item_keys.ordered_key([], lambda: "")


@pytest.mark.parametrize(
    ("field_type", "value"),
    [("integer", True), ("integer", 1.5), ("number", float("nan")), ("number", float("inf")), ("boolean", 1),
     ("string", 1), ("date", "not a date"), ("datetime", "2024-03-01T06:45:00+00:00:30"), ("object", {})],
)
def test_a_value_the_field_type_cannot_hold_has_no_ordered_key(field_type, value):
    with pytest.raises(ValueError):
        item_keys.ordered_key([(field_type, value)], lambda: "")


@pytest.mark.parametrize(
    ("field_type", "options", "expected"),
    [("string", (), True), ("link", (), True), ("integer", (), True), ("number", (), True), ("boolean", (), True),
     ("date", (), True), ("datetime", (), True), ("enum", ("open", "closed"), True),
     ("enum", (1, 2), False), ("enum", ("open", True), False), ("array", (), False), ("object", (), False)],
    ids=["string", "link", "integer", "number", "boolean", "date", "datetime", "text enum", "integer enum",
         "mixed enum", "array", "object"],
)
def test_a_natural_key_field_has_an_ordered_encoding_only_when_it_is_a_scalar_the_encoding_reads(
    field_type, options, expected,
):
    """A collection created ordered over a field that has no encoding could never key its rows."""
    assert item_keys.encodes(field_type, options) is expected


def _sort_key(row):
    # The natural order the encoding must follow: value before null, then the value itself, field by field.
    # Text compares as NFC UTF-8, which is code point order.
    return tuple((1,) if value is None else (0, value) for value in row)


def _random_row(rng):
    def maybe(value):
        return None if rng.random() < 0.25 else value

    return (
        maybe(f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"),
        maybe("".join(rng.choice("ab\x00\x01\u00e9") for _ in range(rng.randint(0, 4)))),
        maybe(rng.choice([0, 1, -1, 255, 256, -256, 2**40, -(2**40), 2**70, -(2**70)])),
        maybe(rng.choice([0.0, 0.5, -0.5, 1.0, -1.0, 1e300, -1e300, 1e-300])),
        maybe(rng.choice([True, False])),
    )


FIELD_TYPES = ("date", "string", "integer", "number", "boolean")


def test_sorting_ordered_keys_sorts_natural_keys_and_the_spelling_sorts_alike():
    rng = random.Random(7)
    rows = list({_random_row(rng) for _ in range(600)})
    keyed = [(item_keys.ordered_key(list(zip(FIELD_TYPES, row, strict=True)), lambda: ""), row) for row in rows]
    assert len({key for key, _ in keyed}) == len(rows), "two natural keys share one key"

    by_key = [row for _, row in sorted(keyed, key=lambda pair: pair[0])]
    assert by_key == sorted(rows, key=_sort_key)
    by_text = [row for _, row in sorted(keyed, key=lambda pair: item_keys.spell(pair[0]))]
    assert by_text == by_key


def test_incomplete_member_keys_sort_after_every_complete_key():
    complete = item_keys.ordered_key([("string", None)], lambda: "")
    incomplete = item_keys.member_position_key("00" * 32, 0)
    assert complete < incomplete


def test_a_number_field_keeps_five_and_five_point_zero_apart_and_in_order():
    as_integer = item_keys.ordered_key([("number", 5)], lambda: "")
    as_float = item_keys.ordered_key([("number", 5.0)], lambda: "")
    assert as_integer != as_float
    assert item_keys.ordered_key([("number", 4.5)], lambda: "") < min(as_integer, as_float)
    assert max(as_integer, as_float) < item_keys.ordered_key([("number", 5.5)], lambda: "")


def test_spelling_is_lowercase_unpadded_base32hex_of_one_to_320_bytes():
    assert item_keys.spell(bytes([1, 0x80, 0x4D, 0x47])) == "0604qho"
    assert len(item_keys.spell(bytes(320))) == 512
    assert item_keys.unspell("0604qho") == bytes([1, 0x80, 0x4D, 0x47])
    for key in (b"\x00", b"\xff" * 5, bytes(range(256)) + b"\x01" * 64):
        assert item_keys.unspell(item_keys.spell(key)) == key
    for refused in (b"", bytes(321)):
        with pytest.raises(ValueError):
            item_keys.spell(refused)


@pytest.mark.parametrize(
    "text", ["", "0604QHO", "0604qho=", "0604qh", "0604qhw", "0604qhp", "0604qh\u00e9", "0604 qho", "0" * 520],
    ids=["empty", "uppercase", "padded", "impossible length", "outside the alphabet", "stray trailing bits", "not ascii",
         "space", "over 320 bytes"],
)
def test_unspell_accepts_only_the_canonical_spelling(text):
    with pytest.raises(ValueError):
        item_keys.unspell(text)


def test_hashed_scheme_key_is_the_uuid5_of_the_serialized_natural_key():
    serialized = collections.natural_key_serialization(
        2, ("occurred_on", "title"), {"occurred_on": "2026-07-01", "title": "cafe\u0301"},
        field_types={"occurred_on": "date"})
    assert collections.inferred_item_key("bf7d5ef7-2e68-4b5f-8e4e-f0f58eb9ccaf", serialized) == (
        "3e504a8f-8308-583c-9685-cb143da103cf")
