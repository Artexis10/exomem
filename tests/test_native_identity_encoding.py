"""Protect existing opaque identities and their ordering during compact encoding."""

import sqlite3

import pytest

from exomem.collection_store import native_identity


def test_current_uuid_keys_do_not_override_an_open_opaque_ingress_contract():
    keys = ["00112233-4455-6677-8899-aabbccddeeff"]
    assert native_identity.choose(keys, canonical_uuid_ingress=False) == native_identity.UTF8
    assert native_identity.choose([], canonical_uuid_ingress=False) == native_identity.UTF8
    assert native_identity.choose(keys, canonical_uuid_ingress=True) == native_identity.UUID16


def test_uuid_looking_identities_keep_distinct_spellings_after_sqlite_reopen(tmp_path):
    keys = ["00112233-4455-6677-8899-aabbccddeeff",
            "00112233-4455-6677-8899-AABBCCDDEEFF",
            "00112233445566778899aabbccddeeff", "opaque/item", "\u03bc/item"]
    codec = native_identity.choose(iter(keys), canonical_uuid_ingress=True)
    assert codec == native_identity.UTF8
    database = tmp_path / "identities.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE identities(key BLOB PRIMARY KEY) STRICT, WITHOUT ROWID")
        connection.executemany("INSERT INTO identities VALUES (?)",
                               ((native_identity.encode(key, codec),) for key in keys))
    with sqlite3.connect(database) as connection:
        restored = [native_identity.decode(row[0], codec)
                    for row in connection.execute("SELECT key FROM identities ORDER BY key")]
    assert restored == sorted(keys)
    assert len(set(restored)) == len(keys)


def test_uuid16_keeps_the_existing_canonical_text_order(tmp_path):
    keys = ["00112233-4455-6677-8899-aabbccddeeff", "00000000-0000-0000-0000-000000000001",
            "ffffffff-ffff-ffff-ffff-ffffffffffff"]
    database = tmp_path / "uuid-order.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE identities(key BLOB PRIMARY KEY) STRICT, WITHOUT ROWID")
        connection.executemany("INSERT INTO identities VALUES (?)",
                               ((native_identity.encode(key, native_identity.UUID16),) for key in keys))
        cells = [row[0] for row in connection.execute("SELECT key FROM identities ORDER BY key")]
    assert all(len(cell) == 16 for cell in cells)
    assert [native_identity.decode(cell, native_identity.UUID16) for cell in cells] == sorted(keys)


def test_uuid16_rejects_noncanonical_spelling_instead_of_merging_identities():
    with pytest.raises(ValueError):
        native_identity.encode("00112233-4455-6677-8899-AABBCCDDEEFF", native_identity.UUID16)
