from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet


def test_cache_persists_only_digest_key_and_encrypted_payload(tmp_path: Path) -> None:
    from exomem.session_validation_cache import SessionValidationCache

    path = tmp_path / "session-validations.sqlite"
    cache = SessionValidationCache(path, encryption_key=Fernet.generate_key())
    token = "exo_s1.secret-session.raw-bearer-material"
    claims = {
        "client_id": "codex-client",
        "scopes": ["exomem:read"],
        "github_login": "person",
    }

    cache.upsert(token, claims, validated_at=1_800_000_000.0)

    entry = cache.get(token)
    assert entry is not None
    assert entry.claims == claims
    assert entry.validated_at == 1_800_000_000.0

    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT token_digest, encrypted_value FROM session_validations"
        ).fetchone()
    assert row is not None
    assert row[0] == digest
    assert token.encode("utf-8") not in path.read_bytes()
    assert b"codex-client" not in path.read_bytes()
    assert b"github_login" not in row[1]


def test_cache_delete_and_wrong_key_fail_closed(tmp_path: Path) -> None:
    from exomem.session_validation_cache import SessionValidationCache

    path = tmp_path / "session-validations.sqlite"
    token = "session-token"
    cache = SessionValidationCache(path, encryption_key=Fernet.generate_key())
    cache.upsert(token, {"client_id": "codex"}, validated_at=1_800_000_000.0)

    wrong_key_cache = SessionValidationCache(path, encryption_key=Fernet.generate_key())
    assert wrong_key_cache.get(token) is None

    cache.delete(token)
    assert cache.get(token) is None


def test_cache_delete_family_clears_only_matching_access_sessions(tmp_path: Path) -> None:
    from exomem.session_validation_cache import SessionValidationCache

    cache = SessionValidationCache(
        tmp_path / "session-validations.sqlite",
        encryption_key=Fernet.generate_key(),
    )
    cache.upsert("family-a-token", {"family_id": "family-a"}, validated_at=1_800_000_000)
    cache.upsert("family-b-token", {"family_id": "family-b"}, validated_at=1_800_000_000)

    cache.delete_family("family-a")

    assert cache.get("family-a-token") is None
    assert cache.get("family-b-token") is not None


def test_cache_rejects_invalid_validation_timestamp(tmp_path: Path) -> None:
    from exomem.session_validation_cache import SessionValidationCache

    cache = SessionValidationCache(
        tmp_path / "session-validations.sqlite",
        encryption_key=Fernet.generate_key(),
    )

    for invalid in (0.0, -1.0, float("inf"), float("nan")):
        try:
            cache.upsert("session-token", {"client_id": "codex"}, validated_at=invalid)
        except ValueError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"accepted invalid validation timestamp {invalid!r}")


def test_delete_of_unknown_bearer_does_not_grow_block_list(tmp_path: Path) -> None:
    # Every failed validation calls delete(), including forged bearers from
    # unauthenticated callers; a digest may only stay blocked when a cached row
    # actually existed, or the in-memory set grows without bound.
    from exomem.session_validation_cache import SessionValidationCache

    cache = SessionValidationCache(
        tmp_path / "session-validations.sqlite", encryption_key=Fernet.generate_key()
    )
    for index in range(50):
        cache.delete(f"garbage-bearer-{index}")
    assert cache._blocked_digests == set()

    cache.upsert("real-token", {"client_id": "codex"}, validated_at=1_800_000_000.0)
    cache.delete("real-token")
    digest = hashlib.sha256(b"real-token").hexdigest()
    assert digest in cache._blocked_digests
    assert cache.get("real-token") is None
