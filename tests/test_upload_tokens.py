"""upload_tokens.mint / verify — short-lived HMAC upload tokens."""

from __future__ import annotations

import pytest

from exomem import upload_tokens

SECRET = "s3cret-long-lived"


def test_mint_verify_roundtrip() -> None:
    t = upload_tokens.mint(SECRET, ttl=900, now=1000)  # exp = 1900
    assert upload_tokens.verify(t, SECRET, now=1000) is True
    assert upload_tokens.verify(t, SECRET, now=1899) is True  # still inside ttl


def test_expired_fails() -> None:
    t = upload_tokens.mint(SECRET, ttl=900, now=1000)  # exp = 1900
    assert upload_tokens.verify(t, SECRET, now=1901) is False


def test_wrong_secret_fails() -> None:
    t = upload_tokens.mint(SECRET, ttl=900, now=1000)
    assert upload_tokens.verify(t, "other-secret", now=1000) is False


def test_tampered_exp_fails() -> None:
    # extend the expiry but keep the old signature → must not verify
    t = upload_tokens.mint(SECRET, ttl=900, now=1000)
    _, exp, sig = t.split(".")
    forged = f"v1.{int(exp) + 100000}.{sig}"
    assert upload_tokens.verify(forged, SECRET, now=1000) is False


def test_malformed_fails() -> None:
    for bad in (None, "", "nope", "v1.notanumber.deadbeef", "v1.1900", "1900.deadbeef", "v2.1900.x"):
        assert upload_tokens.verify(bad, SECRET, now=1000) is False


def test_long_lived_token_is_not_a_minted_token() -> None:
    # a raw 64-hex long-lived token has no `v1.` prefix → not accepted here
    assert upload_tokens.verify("a" * 64, SECRET, now=1000) is False


# ---------------- mint_for_endpoint (the mint_upload_token tool body) ----------------


def test_mint_for_endpoint_returns_verifiable_token() -> None:
    out = upload_tokens.mint_for_endpoint(SECRET, "https://kb.example.io")
    assert out["ttl_seconds"] == upload_tokens.DEFAULT_TTL
    assert out["upload_url"] == "https://kb.example.io/upload"
    assert upload_tokens.verify(out["token"], SECRET) is True


def test_mint_for_endpoint_disabled_without_secret() -> None:
    with pytest.raises(ValueError, match="UPLOAD_DISABLED"):
        upload_tokens.mint_for_endpoint(None, "https://kb.example.io")


def test_mint_for_endpoint_no_large_url_by_default() -> None:
    out = upload_tokens.mint_for_endpoint(SECRET, "https://kb.example.io")
    assert "large_upload_url" not in out


def test_mint_for_endpoint_large_url_when_configured() -> None:
    out = upload_tokens.mint_for_endpoint(
        SECRET, "https://kb.example.io", large_base_url="https://big.example.ts.net"
    )
    assert out["upload_url"] == "https://kb.example.io/upload"
    assert out["large_upload_url"] == "https://big.example.ts.net/upload"
    assert upload_tokens.verify(out["token"], SECRET) is True


# ---------------- scope isolation (upload vs download) ----------------


def test_scope_isolation() -> None:
    up = upload_tokens.mint(SECRET, scope="upload", now=1000)
    dn = upload_tokens.mint(SECRET, scope="download", now=1000)
    assert upload_tokens.verify(up, SECRET, scope="upload", now=1000) is True
    assert upload_tokens.verify(up, SECRET, scope="download", now=1000) is False
    assert upload_tokens.verify(dn, SECRET, scope="download", now=1000) is True
    assert upload_tokens.verify(dn, SECRET, scope="upload", now=1000) is False


def test_mint_for_endpoint_download() -> None:
    out = upload_tokens.mint_for_endpoint(SECRET, "https://kb.example.io", scope="download")
    assert out["download_url"] == "https://kb.example.io/download"
    assert out["ttl_seconds"] == upload_tokens.DEFAULT_TTL
    assert upload_tokens.verify(out["token"], SECRET, scope="download") is True
    with pytest.raises(ValueError, match="DOWNLOAD_DISABLED"):
        upload_tokens.mint_for_endpoint(None, "https://kb.example.io", scope="download")
