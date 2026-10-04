"""Collection store guard tokens (move-structured-collections-to-sqlite P1a.6).

The wire keeps its 64-lowercase-hex guard fields; in the store they derive
from the collection generation and the row version, domain-separated (§3):

    container_hash = sha256("exomem-collection-generation:v1\\0" + collection_id + "\\0"
                            + generation + "\\0" + audit_head)
    item_version   = sha256("exomem-collection-row:v1\\0" + collection_id + "\\0" + item_key
                            + "\\0" + row_version + "\\0" + payload_hash)
    manifest_hash  = sha256(manifest_text bytes)

``payload_hash`` is today's ``records._payload_hash`` derivation, byte for byte,
over the stored semantic body. The frozen vectors below were produced by
today's file-mode code and by the design formulas; a change to either side
turns them red. All data is invented.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

import pytest
from record_fixtures import LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT, ledger_item

from exomem import records
from exomem import structured_collections as collections
from exomem.collection_store import tokens

COLLECTION_ID = "5e9d1e2f-8f0a-4b6c-9d21-6f0f4a2b7c31"
ITEM_KEY = "3f0c3c9e-1c1f-5a8e-9b7e-0f6a1c2d3e4f"
HEX64 = re.compile(r"[0-9a-f]{64}")


def _payload_cases() -> dict[str, tuple[str, dict[str, object], str, str]]:
    """name -> (item_key, values, semantic body, frozen payload hash)."""
    return {
        "ledger_empty_body": (
            ITEM_KEY,
            ledger_item(),
            "",
            "2c2ed092802463178890d34184e2b0cd87a3c5514cdc02046b02d161b1fc6d2b",
        ),
        "nfd_values_nested_objects_and_body": (
            "key-nfd",
            ledger_item(
                slug=unicodedata.normalize("NFD", "café-note"),
                details={"z": "é", "a": [unicodedata.normalize("NFD", "Å"), 1, None]},
            ),
            unicodedata.normalize("NFD", "Résumé body\n"),
            "c562ecddb1222d67a10cac56ac2909be8461b3bb757580753eb2ce4239704bf4",
        ),
        "crlf_body_and_empty_list": (
            "key-crlf",
            ledger_item(word_count=0, channels=[]),
            "Line one.\r\n\r\nLine two.\r\n",
            "bd9d474fd003751423fbe9bdfdd0fd1d6dad408768a8bcd64a28ebb511481a18",
        ),
    }


@pytest.fixture
def manifest(tmp_path: Path) -> collections.CollectionManifest:
    return collections.parse_manifest_bytes(
        tmp_path, LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT.encode("utf-8")
    )


@pytest.mark.parametrize("case", sorted(_payload_cases()))
def test_payload_hash_matches_todays_derivation(
    case: str, manifest: collections.CollectionManifest
) -> None:
    item_key, values, body, frozen = _payload_cases()[case]
    stored = tokens.payload_hash(manifest.schema.version, item_key, values, body)
    assert stored == records._payload_hash(manifest, item_key, values, body)
    assert stored == frozen


def test_payload_hash_ignores_value_key_order_and_normalization_form() -> None:
    values = {"b": "é", "a": {"y": 1, "x": [unicodedata.normalize("NFD", "Å")]}}
    reordered = {"a": {"x": ["Å"], "y": 1}, "b": "é"}
    assert tokens.payload_hash(1, "k", values, "") == tokens.payload_hash(1, "k", reordered, "")
    assert tokens.payload_hash(1, "k", values, "") != tokens.payload_hash(2, "k", values, "")
    assert tokens.payload_hash(1, "k", values, "") != tokens.payload_hash(1, "k2", values, "")
    assert tokens.payload_hash(1, "k", values, "") != tokens.payload_hash(1, "k", values, "x")


def test_payload_hash_refuses_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        tokens.payload_hash(1, "k", {"reading": float("nan")}, "")


def test_container_hash_is_the_domain_separated_generation_digest() -> None:
    head = "1" * 64
    token = tokens.container_hash(COLLECTION_ID, 7, head)
    expected = hashlib.sha256(
        f"exomem-collection-generation:v1\0{COLLECTION_ID}\0007\0{head}".encode()
    ).hexdigest()
    assert token == expected
    assert token == "17033eeb7cd7186276776aa46a2cb9b97bea036e6310e11ca3283399bc9109f6"


def test_container_hash_without_an_audit_head_encodes_it_empty() -> None:
    assert (
        tokens.container_hash(COLLECTION_ID, 0, None)
        == "17f46c3db7f0eff0fa3be924242f9633f7c5048bfe57fc6d0ac70d4fa4214658"
    )


def test_item_version_is_the_domain_separated_row_digest() -> None:
    payload = _payload_cases()["ledger_empty_body"][3]
    token = tokens.item_version(COLLECTION_ID, ITEM_KEY, 3, payload)
    expected = hashlib.sha256(
        f"exomem-collection-row:v1\0{COLLECTION_ID}\0{ITEM_KEY}\0003\0{payload}".encode()
    ).hexdigest()
    assert token == expected
    assert token == "e2db320cd2880acd72e9fa814174e568261553ca4b42d28b278094a91b75b4d4"


def test_store_head_hash_vector() -> None:
    assert (
        tokens.store_head_hash(None, 1, "e" * 64)
        == "22e2c03e38b5a9e7c7846c4a1da2b32f49093a624e51079bd3e83428fac551a5"
    )


def test_manifest_hash_is_the_hash_of_the_manifest_bytes(
    manifest: collections.CollectionManifest,
) -> None:
    assert tokens.manifest_hash(LEDGER_MANIFEST_TEXT) == manifest.manifest_version.hash
    bom_crlf = "﻿" + LEDGER_MANIFEST_TEXT.replace("\n", "\r\n")
    assert tokens.manifest_hash(bom_crlf) == hashlib.sha256(bom_crlf.encode("utf-8")).hexdigest()


def test_tokens_are_64_lowercase_hex_and_every_component_counts() -> None:
    payload = "a" * 64
    base = tokens.item_version(COLLECTION_ID, "k", 1, payload)
    variants = {
        tokens.item_version(COLLECTION_ID, "k", 2, payload),
        tokens.item_version(COLLECTION_ID, "k2", 1, payload),
        tokens.item_version(COLLECTION_ID, "k", 1, "b" * 64),
        tokens.item_version("6e9d1e2f-8f0a-4b6c-9d21-6f0f4a2b7c31", "k", 1, payload),
    }
    assert base not in variants and len(variants) == 4
    generations = {tokens.container_hash(COLLECTION_ID, g, "1" * 64) for g in range(5)}
    assert len(generations) == 5
    for token in (base, *variants, *generations):
        assert HEX64.fullmatch(token)


def test_domains_separate_token_kinds() -> None:
    # The same component strings never produce the same token across kinds.
    assert tokens.container_hash("c", 1, "a" * 64) != tokens.item_version("c", "1", 1, "a" * 64)
    assert tokens.container_hash("c", 1, None) != hashlib.sha256(b"c\x001\x00").hexdigest()


@pytest.mark.parametrize(
    "call",
    [
        lambda: tokens.container_hash("c\0x", 1, None),
        lambda: tokens.container_hash("c", -1, None),
        lambda: tokens.container_hash("c", True, None),
        lambda: tokens.container_hash("c", 1, "a\0b"),
        lambda: tokens.item_version("c", "k", 0, "a" * 64),
        lambda: tokens.item_version("c", "k\0", 1, "a" * 64),
        lambda: tokens.item_version("c", "k", 1, "A" * 64),
        lambda: tokens.store_head_hash(None, 0, "a" * 64),
    ],
)
def test_malformed_components_are_refused(call) -> None:  # noqa: ANN001
    with pytest.raises((TypeError, ValueError)):
        call()
