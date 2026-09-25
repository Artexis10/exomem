"""Tests for D7/C4 bearer derivation and envelope encryption (task 3.5)."""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from cellctl.secrets import (
    bearer_matches,
    derive_cell_bearer,
    generate_backup_data_key,
    unwrap_secret,
    wrap_secret,
)


def test_bearer_is_43_characters_and_unpadded() -> None:
    bearer = derive_cell_bearer(b"x" * 32, "aaaaaaaaaaaaaaaa")
    assert len(bearer) == 43
    assert "=" not in bearer


def test_bearer_derivation_is_deterministic_for_same_key_and_cell() -> None:
    key = os.urandom(32)
    assert derive_cell_bearer(key, "aaaaaaaaaaaaaaaa") == derive_cell_bearer(
        key, "aaaaaaaaaaaaaaaa"
    )


def test_bearer_differs_per_cell_id_under_the_same_key() -> None:
    key = os.urandom(32)
    assert derive_cell_bearer(key, "aaaaaaaaaaaaaaaa") != derive_cell_bearer(
        key, "bbbbbbbbbbbbbbbb"
    )


def test_bearer_matches_accepts_the_correct_bearer() -> None:
    key = os.urandom(32)
    bearer = derive_cell_bearer(key, "aaaaaaaaaaaaaaaa")
    assert bearer_matches(bearer, key=key, cell_id="aaaaaaaaaaaaaaaa")


def test_bearer_matches_rejects_a_bearer_for_a_different_cell() -> None:
    key = os.urandom(32)
    bearer = derive_cell_bearer(key, "aaaaaaaaaaaaaaaa")
    assert not bearer_matches(bearer, key=key, cell_id="bbbbbbbbbbbbbbbb")


def test_bearer_matches_rejects_a_bearer_under_a_different_key() -> None:
    bearer = derive_cell_bearer(os.urandom(32), "aaaaaaaaaaaaaaaa")
    assert not bearer_matches(bearer, key=os.urandom(32), cell_id="aaaaaaaaaaaaaaaa")


def test_backup_data_key_is_32_random_bytes_each_time() -> None:
    a = generate_backup_data_key()
    b = generate_backup_data_key()
    assert len(a) == 32
    assert len(b) == 32
    assert a != b


_AAD = dict(cell_id="aaaaaaaaaaaaaaaa", column="backup_key_wrapped", key_version=1)


def test_wrap_and_unwrap_round_trips_the_plaintext() -> None:
    master_key = os.urandom(32)
    plaintext = generate_backup_data_key()
    wrapped = wrap_secret(master_key, plaintext, **_AAD)
    assert wrapped != plaintext
    assert unwrap_secret(master_key, wrapped, **_AAD) == plaintext


def test_wrap_produces_a_different_blob_each_time_same_plaintext() -> None:
    master_key = os.urandom(32)
    plaintext = b"x" * 32
    assert wrap_secret(master_key, plaintext, **_AAD) != wrap_secret(master_key, plaintext, **_AAD)


def test_unwrap_fails_under_the_wrong_master_key() -> None:
    plaintext = generate_backup_data_key()
    wrapped = wrap_secret(os.urandom(32), plaintext, **_AAD)
    with pytest.raises(InvalidTag):
        unwrap_secret(os.urandom(32), wrapped, **_AAD)


# SR-L5: a wrapped blob must be bound to its cell, column and key version, so
# it cannot be moved to another cell/column/version and still decrypt.


def test_unwrap_fails_under_a_different_cell_id() -> None:
    master_key = os.urandom(32)
    plaintext = generate_backup_data_key()
    wrapped = wrap_secret(master_key, plaintext, **_AAD)
    with pytest.raises(InvalidTag):
        unwrap_secret(master_key, wrapped, **{**_AAD, "cell_id": "bbbbbbbbbbbbbbbb"})


def test_unwrap_fails_under_a_different_column() -> None:
    master_key = os.urandom(32)
    plaintext = generate_backup_data_key()
    wrapped = wrap_secret(master_key, plaintext, **_AAD)
    with pytest.raises(InvalidTag):
        unwrap_secret(master_key, wrapped, **{**_AAD, "column": "b2_key_wrapped"})


def test_unwrap_fails_under_a_different_key_version() -> None:
    master_key = os.urandom(32)
    plaintext = generate_backup_data_key()
    wrapped = wrap_secret(master_key, plaintext, **_AAD)
    with pytest.raises(InvalidTag):
        unwrap_secret(master_key, wrapped, **{**_AAD, "key_version": 2})
