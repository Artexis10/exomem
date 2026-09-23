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
    out = upload_tokens.mint_for_endpoint(
        SECRET, "https://kb.example.io", scope="download", audience=AUDIENCE
    )
    assert out["download_url"] == "https://kb.example.io/download"
    assert out["ttl_seconds"] == upload_tokens.DEFAULT_TTL
    assert upload_tokens.bound_audience(out["token"], SECRET) == AUDIENCE
    with pytest.raises(ValueError, match="DOWNLOAD_DISABLED"):
        upload_tokens.mint_for_endpoint(None, "https://kb.example.io", scope="download")


# ---------------- download capabilities carry their minting audience ----------------

AUDIENCE = "principal:" + "ab" * 32
FLOOR = "\x00unresolved"


def test_mint_for_endpoint_download_without_an_audience_binds_the_floor() -> None:
    out = upload_tokens.mint_for_endpoint(SECRET, "https://kb.example.io", scope="download")
    assert upload_tokens.bound_audience(out["token"], SECRET) == FLOOR


def test_mint_for_endpoint_upload_ignores_the_audience() -> None:
    out = upload_tokens.mint_for_endpoint(SECRET, "https://kb.example.io", audience=AUDIENCE)
    assert upload_tokens.verify(out["token"], SECRET) is True
    assert upload_tokens.bound_audience(out["token"], SECRET, scope="upload") is None


def test_bound_roundtrip_and_expiry() -> None:
    t = upload_tokens.mint_bound(SECRET, audience=AUDIENCE, ttl=900, now=1000)
    assert upload_tokens.bound_audience(t, SECRET, now=1000) == AUDIENCE
    assert upload_tokens.bound_audience(t, SECRET, now=1900) == AUDIENCE
    assert upload_tokens.bound_audience(t, SECRET, now=1901) is None


@pytest.mark.parametrize("audience", ["owner", AUDIENCE, FLOOR, "a.b:c/d"])
def test_bound_audience_survives_any_spelling(audience: str) -> None:
    t = upload_tokens.mint_bound(SECRET, audience=audience, now=1000)
    assert upload_tokens.bound_audience(t, SECRET, now=1000) == audience


def test_bound_audience_cannot_be_swapped() -> None:
    t = upload_tokens.mint_bound(SECRET, audience=AUDIENCE, now=1000)
    prefix, exp, _claim, sig = t.split(".")
    owner_claim = upload_tokens.mint_bound(SECRET, audience="owner", now=1000).split(".")[2]
    assert upload_tokens.bound_audience(f"{prefix}.{exp}.{owner_claim}.{sig}", SECRET, now=1000) is None


def test_bound_expiry_cannot_be_extended() -> None:
    t = upload_tokens.mint_bound(SECRET, audience=AUDIENCE, ttl=900, now=1000)
    prefix, exp, claim, sig = t.split(".")
    forged = f"{prefix}.{int(exp) + 100000}.{claim}.{sig}"
    assert upload_tokens.bound_audience(forged, SECRET, now=1000) is None


def test_bound_wrong_secret_or_scope_fails() -> None:
    t = upload_tokens.mint_bound(SECRET, audience=AUDIENCE, now=1000)
    assert upload_tokens.bound_audience(t, "other-secret", now=1000) is None
    assert upload_tokens.bound_audience(t, SECRET, scope="upload", now=1000) is None


def test_bound_token_is_not_an_upload_or_legacy_token() -> None:
    t = upload_tokens.mint_bound(SECRET, audience=AUDIENCE, now=1000)
    assert upload_tokens.verify(t, SECRET, scope="upload", now=1000) is False
    assert upload_tokens.verify(t, SECRET, scope="download", now=1000) is False
    assert upload_tokens.lane_for_token(t, SECRET, now=1000) is None


def test_legacy_download_token_names_no_audience() -> None:
    legacy = upload_tokens.mint(SECRET, scope="download", now=1000)
    assert upload_tokens.bound_audience(legacy, SECRET, now=1000) is None


def test_bound_claim_has_one_spelling() -> None:
    t = upload_tokens.mint_bound(SECRET, audience="owner", now=1000)
    prefix, exp, claim, sig = t.split(".")
    assert upload_tokens.bound_audience(f"{prefix}.{exp}.{claim}=.{sig}", SECRET, now=1000) is None


def test_bound_malformed_fails() -> None:
    good = upload_tokens.mint_bound(SECRET, audience=AUDIENCE, now=1000)
    _, exp, claim, sig = good.split(".")
    for bad in (
        None,
        "",
        "v2",
        f"v2.{exp}.{claim}",
        f"v2.{exp}.{claim}.{sig}.extra",
        f"v2.notanumber.{claim}.{sig}",
        f"v2.{exp}.!!!.{sig}",
        f"v2.{exp}..{sig}",
        f"v2.{exp}.{claim}.{sig[:-1]}é",
        f"v1.{exp}.{sig}",
    ):
        assert upload_tokens.bound_audience(bad, SECRET, now=1000) is None, bad


def test_mint_bound_requires_an_audience() -> None:
    with pytest.raises(ValueError):
        upload_tokens.mint_bound(SECRET, audience="")
