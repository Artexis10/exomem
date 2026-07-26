"""Single-use, content-bound withhold-tokens (design decision D6).

A withheld or abstracted item MAY carry an escalation token bound to the
audience, the item's CONTENT FINGERPRINTS, a maximum level, and an expiry.
Wire form is `wh1.<jti>.<exp>.<hmac>`; the HMAC covers
`{jti, fingerprints, audience, exp}` under a per-machine key kept in the
`.governance.sqlite` sidecar meta — SystemRandom, never an env secret, never
synced. Redemption is consume-once under `BEGIN IMMEDIATE`.

The binding is to content, not to paths, so approval-by-substitution is
impossible: swap the file after minting and redemption fails closed, offering
a fresh escalation rather than disclosing the changed content.

Minting is a privilege, not a fallback: an `empty` policy has no governance to
escalate against and a `blocked` policy cannot be trusted to say what the
ceiling is, so both refuse to mint.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from exomem.governance import egress, tokens
from exomem.governance.principal import RequestPrincipal
from exomem.governance.store import sidecar_path

SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
RESTRICTED_PATH = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
OTHER_PATH = "Knowledge Base/Notes/Patterns/retry-with-full-jitter-backoff.md"
EXTERNAL = "external"


def _gov(vault: Path) -> Path:
    return vault / "Knowledge Base" / "_Governance"


def govern(vault: Path, *, ceiling: int = 0, audience: str = EXTERNAL) -> None:
    scope = _gov(vault) / "scopes" / "patterns.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Patterns\n"
        'paths: ["Notes/Patterns/**"]\n',
        encoding="utf-8",
    )
    rule = _gov(vault) / "rules" / "patterns-external.yaml"
    rule.parent.mkdir(parents=True, exist_ok=True)
    rule.write_text(
        f"governance_version: 1\nid: {RULE_ID}\nscope_ids: [\"{SCOPE_ID}\"]\n"
        f"audience: {audience}\nceiling: {ceiling}\n",
        encoding="utf-8",
    )


def govern_broken(vault: Path) -> None:
    """Cold-start compile refusal -> `policy.load()` returns the BLOCKED policy."""
    govern(vault)
    rule = _gov(vault) / "rules" / "patterns-external.yaml"
    rule.write_text(
        f"governance_version: 1\nid: {RULE_ID}\nscope_ids: [\"{SCOPE_ID}\"]\n"
        f"audience: {EXTERNAL}\nceiling: 9\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _clear_caches():
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    yield
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _reset_policy_caches() -> None:
    """Drop every governance memo so a freshly written policy takes effect
    inside an already-built server process."""
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _mint(vault: Path, **kw) -> str:
    kw.setdefault("paths", [RESTRICTED_PATH])
    kw.setdefault("audience", EXTERNAL)
    kw.setdefault("max_level", egress.LEVEL_EXCERPT)
    return tokens.mint(vault, **kw)


# --------------------------------------------------------------------------
# Wire form and mint/verify
# --------------------------------------------------------------------------


def test_mint_produces_the_wh1_wire_form(vault: Path) -> None:
    govern(vault)
    token = _mint(vault)
    parts = token.split(".")
    assert len(parts) == 4
    assert parts[0] == "wh1"
    assert parts[1] and parts[2].isdigit() and parts[3]
    # The wire form carries no path, no content, no level.
    assert RESTRICTED_PATH not in token


def test_verify_round_trips_the_bound_claims(vault: Path) -> None:
    govern(vault)
    token = _mint(vault, max_level=egress.LEVEL_EXCERPT)
    claim = tokens.verify(vault, token, audience=EXTERNAL)
    assert claim.audience == EXTERNAL
    assert claim.max_level == egress.LEVEL_EXCERPT
    assert claim.fingerprints == (tokens.content_fingerprint(vault, RESTRICTED_PATH),)
    assert claim.expires_at > int(time.time())


def test_token_never_authorizes_above_its_bound_maximum(vault: Path) -> None:
    govern(vault)
    claim = tokens.verify(vault, _mint(vault, max_level=egress.LEVEL_ABSTRACT),
                          audience=EXTERNAL)
    assert claim.max_level == egress.LEVEL_ABSTRACT
    assert claim.max_level < egress.LEVEL_FULL


def test_multi_item_token_binds_every_fingerprint(vault: Path) -> None:
    govern(vault)
    token = _mint(vault, paths=[RESTRICTED_PATH, OTHER_PATH])
    claim = tokens.verify(vault, token, audience=EXTERNAL)
    assert set(claim.fingerprints) == {
        tokens.content_fingerprint(vault, RESTRICTED_PATH),
        tokens.content_fingerprint(vault, OTHER_PATH),
    }


# --------------------------------------------------------------------------
# Tamper / expiry / audience
# --------------------------------------------------------------------------


@pytest.mark.parametrize("index", [1, 2, 3])
def test_tampering_with_any_segment_refuses(vault: Path, index: int) -> None:
    govern(vault)
    parts = _mint(vault).split(".")
    parts[index] = "9" * len(parts[index]) if parts[index].isdigit() else "x" * len(parts[index])
    with pytest.raises(tokens.WithholdTokenError) as err:
        tokens.verify(vault, ".".join(parts), audience=EXTERNAL)
    assert err.value.code in ("TOKEN_INVALID", "TOKEN_UNKNOWN")


def test_malformed_token_refuses(vault: Path) -> None:
    govern(vault)
    for bad in ("", "wh1", "wh1.a.b", "wh2.a.1.b", "not-a-token", "wh1.a.notanint.b"):
        with pytest.raises(tokens.WithholdTokenError):
            tokens.verify(vault, bad, audience=EXTERNAL)


def test_expired_token_refuses(vault: Path) -> None:
    govern(vault)
    token = _mint(vault, ttl_seconds=1)
    claim = tokens.verify(vault, token, audience=EXTERNAL)
    with pytest.raises(tokens.WithholdTokenError) as err:
        tokens.verify(vault, token, audience=EXTERNAL, now=claim.expires_at + 1)
    assert err.value.code == "TOKEN_EXPIRED"


def test_token_is_bound_to_its_audience(vault: Path) -> None:
    """Not replayable across clients: the audience is inside the HMAC."""
    govern(vault)
    token = _mint(vault, audience=EXTERNAL)
    with pytest.raises(tokens.WithholdTokenError) as err:
        tokens.verify(vault, token, audience="someone-else")
    assert err.value.code == "TOKEN_INVALID"


# --------------------------------------------------------------------------
# Consume-once
# --------------------------------------------------------------------------


def test_redeem_is_single_use(vault: Path) -> None:
    govern(vault)
    token = _mint(vault)
    claim = tokens.redeem(vault, token, audience=EXTERNAL)
    assert claim.max_level == egress.LEVEL_EXCERPT
    with pytest.raises(tokens.WithholdTokenError) as err:
        tokens.redeem(vault, token, audience=EXTERNAL)
    assert err.value.code == "TOKEN_CONSUMED"


def test_verify_does_not_consume(vault: Path) -> None:
    govern(vault)
    token = _mint(vault)
    tokens.verify(vault, token, audience=EXTERNAL)
    tokens.verify(vault, token, audience=EXTERNAL)
    tokens.redeem(vault, token, audience=EXTERNAL)


def test_consume_once_uses_an_immediate_transaction() -> None:
    """`BEGIN IMMEDIATE` is what makes redemption single-use under concurrency:
    it takes the write lock before the read, so two racing redemptions cannot
    both observe `consumed_at IS NULL`."""
    import inspect

    source = inspect.getsource(tokens)
    assert "BEGIN IMMEDIATE" in source


def test_redeeming_an_unknown_jti_refuses(vault: Path) -> None:
    govern(vault)
    token = _mint(vault)
    with sqlite3.connect(sidecar_path(vault)) as conn:
        conn.execute("DELETE FROM withhold_tokens")
        conn.commit()
    with pytest.raises(tokens.WithholdTokenError) as err:
        tokens.redeem(vault, token, audience=EXTERNAL)
    assert err.value.code == "TOKEN_UNKNOWN"


# --------------------------------------------------------------------------
# Content-fingerprint binding
# --------------------------------------------------------------------------


def test_content_drift_refuses_and_offers_a_fresh_escalation(vault: Path) -> None:
    """Approval-by-substitution is impossible: the token is bound to the bytes
    that were approved, not to the path that held them."""
    govern(vault)
    token = _mint(vault)
    (vault / RESTRICTED_PATH).write_text(
        "---\ntype: pattern\n---\n\n# Swapped after the token was minted\n",
        encoding="utf-8",
    )
    with pytest.raises(tokens.WithholdTokenError) as err:
        tokens.redeem(vault, token, audience=EXTERNAL)
    assert err.value.code == "TOKEN_CONTENT_DRIFT"
    # One step, not a re-confirm treadmill: the refusal names how to proceed.
    assert err.value.remediation
    assert "fresh" in err.value.remediation.lower()


def test_content_drift_does_not_consume_the_token(vault: Path) -> None:
    """A refused redemption must not burn the token — otherwise a drifting
    file would silently destroy a still-valid escalation."""
    govern(vault)
    token = _mint(vault)
    original = (vault / RESTRICTED_PATH).read_text(encoding="utf-8")
    (vault / RESTRICTED_PATH).write_text("changed", encoding="utf-8")
    with pytest.raises(tokens.WithholdTokenError):
        tokens.redeem(vault, token, audience=EXTERNAL)
    (vault / RESTRICTED_PATH).write_text(original, encoding="utf-8")
    assert tokens.redeem(vault, token, audience=EXTERNAL).audience == EXTERNAL


def test_content_fingerprint_tracks_bytes_not_mtime(vault: Path) -> None:
    govern(vault)
    before = tokens.content_fingerprint(vault, RESTRICTED_PATH)
    path = vault / RESTRICTED_PATH
    path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")  # touch
    assert tokens.content_fingerprint(vault, RESTRICTED_PATH) == before
    path.write_text("different", encoding="utf-8")
    assert tokens.content_fingerprint(vault, RESTRICTED_PATH) != before


def test_missing_item_fingerprint_refuses_to_mint(vault: Path) -> None:
    govern(vault)
    with pytest.raises(tokens.WithholdTokenError) as err:
        _mint(vault, paths=["Knowledge Base/Notes/Patterns/no-such-page.md"])
    assert err.value.code == "TOKEN_UNMINTABLE"


# --------------------------------------------------------------------------
# Mint refuses on an empty or blocked policy
# --------------------------------------------------------------------------


def test_empty_policy_refuses_to_mint(vault: Path) -> None:
    """No `_Governance/` means nothing was withheld — there is nothing to
    escalate against, and a token would be a capability over an open vault."""
    with pytest.raises(tokens.WithholdTokenError) as err:
        _mint(vault)
    assert err.value.code == "TOKEN_UNMINTABLE"
    assert not sidecar_path(vault).exists()


def test_blocked_policy_refuses_to_mint(vault: Path) -> None:
    """A refused cold-start compile cannot be trusted to say what the ceiling
    is, so it cannot authorize an escalation above it."""
    govern_broken(vault)
    from exomem.governance import policy as policy_module

    assert policy_module.load(vault).blocked is True
    with pytest.raises(tokens.WithholdTokenError) as err:
        _mint(vault)
    assert err.value.code == "TOKEN_UNMINTABLE"
    assert not sidecar_path(vault).exists()


# --------------------------------------------------------------------------
# Per-machine HMAC key
# --------------------------------------------------------------------------


def test_hmac_key_is_per_machine_and_persistent(vault: Path) -> None:
    govern(vault)
    _mint(vault)
    first = tokens._hmac_key(vault)
    assert isinstance(first, bytes) and len(first) >= 32
    assert tokens._hmac_key(vault) == first


def test_hmac_key_never_comes_from_the_environment(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is a per-machine sidecar value (the `sidecar_store` precedent),
    never an env secret and never synced — so a vault copied to another box
    cannot carry redeemable tokens with it."""
    govern(vault)
    _mint(vault)
    key = tokens._hmac_key(vault)
    for name in ("EXOMEM_WITHHOLD_KEY", "EXOMEM_HMAC_KEY", "EXOMEM_SECRET"):
        monkeypatch.setenv(name, "attacker-controlled-value")
    tokens.clear_key_cache()
    assert tokens._hmac_key(vault) == key


def test_a_different_vault_gets_a_different_key(vault: Path, tmp_path: Path) -> None:
    govern(vault)
    _mint(vault)
    other = tmp_path / "other-vault"
    (other / "Knowledge Base" / "Notes" / "Patterns").mkdir(parents=True)
    target = other / RESTRICTED_PATH
    target.write_text("---\ntype: pattern\n---\n\n# Other\n", encoding="utf-8")
    govern(other)
    tokens.mint(other, paths=[RESTRICTED_PATH], audience=EXTERNAL, max_level=5)
    assert tokens._hmac_key(other) != tokens._hmac_key(vault)


def test_token_from_one_vault_is_not_redeemable_in_another(
    vault: Path, tmp_path: Path
) -> None:
    govern(vault)
    token = _mint(vault)
    other = tmp_path / "other-vault"
    (other / "Knowledge Base" / "Notes" / "Patterns").mkdir(parents=True)
    (other / RESTRICTED_PATH).write_text(
        (vault / RESTRICTED_PATH).read_text(encoding="utf-8"), encoding="utf-8"
    )
    govern(other)
    tokens.mint(other, paths=[RESTRICTED_PATH], audience=EXTERNAL, max_level=5)
    with pytest.raises(tokens.WithholdTokenError):
        tokens.redeem(other, token, audience=EXTERNAL)


# --------------------------------------------------------------------------
# TTL sweep
# --------------------------------------------------------------------------


def test_sweep_removes_only_expired_rows(vault: Path) -> None:
    govern(vault)
    short = _mint(vault, ttl_seconds=1)
    long = _mint(vault, ttl_seconds=3600)
    short_claim = tokens.verify(vault, short, audience=EXTERNAL)
    removed = tokens.sweep(vault, now=short_claim.expires_at + 1)
    assert removed == 1
    assert tokens.verify(vault, long, audience=EXTERNAL).max_level == egress.LEVEL_EXCERPT
    with pytest.raises(tokens.WithholdTokenError):
        tokens.verify(vault, short, audience=EXTERNAL)


def test_sweep_is_safe_on_a_vault_with_no_sidecar(vault: Path) -> None:
    assert tokens.sweep(vault) == 0
    assert not sidecar_path(vault).exists()


def test_minting_sweeps_opportunistically(vault: Path) -> None:
    govern(vault)
    stale = _mint(vault, ttl_seconds=1)
    stale_claim = tokens.verify(vault, stale, audience=EXTERNAL)
    _mint(vault, now=stale_claim.expires_at + 1)
    with sqlite3.connect(sidecar_path(vault)) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM withhold_tokens").fetchone()[0]
    assert rows == 1


# --------------------------------------------------------------------------
# Wiring: withheld notices embed a token
# --------------------------------------------------------------------------


def test_withheld_notice_embeds_a_redeemable_token(vault: Path) -> None:
    govern(vault, ceiling=egress.LEVEL_NOTICE)
    from exomem.find_types import Hit

    def _hit(path: str) -> Hit:
        return Hit(path=path, type="pattern", scope=None, title="t",
                   updated="2026-01-01", excerpt="e", bm25_rank=1)

    result = egress.annotate_hits(
        vault,
        [_hit(RESTRICTED_PATH), _hit(OTHER_PATH)],
        principal=RequestPrincipal(audience_id=EXTERNAL, surface="mcp"),
        limit=2,
    )
    assert len(result.notices) >= 1
    notice = result.notices[0]
    assert notice["escalation_token"].startswith("wh1.")
    claim = tokens.verify(vault, notice["escalation_token"], audience=EXTERNAL)
    # Bound to the item's OWN ceiling, not to the release floor — a token is
    # not an approval step, so it must not carry more than the decision did.
    assert claim.max_level == egress.LEVEL_NOTICE
    # The notice still leaks nothing about the item itself.
    assert "path" not in notice and "excerpt" not in notice
    assert RESTRICTED_PATH not in str(notice)


def test_l0_withholding_mints_no_token(vault: Path) -> None:
    """L0 is silent: there is no notice to carry a token, and minting one
    would create a capability nobody was told about."""
    govern(vault, ceiling=egress.LEVEL_NONE)
    from exomem.find_types import Hit

    result = egress.annotate_hits(
        vault,
        [Hit(path=RESTRICTED_PATH, type="pattern", scope=None, title="t",
             updated="2026-01-01", excerpt="e")],
        principal=RequestPrincipal(audience_id=EXTERNAL, surface="mcp"),
        limit=2,
    )
    assert result.notices == []
    assert not sidecar_path(vault).exists()


# --------------------------------------------------------------------------
# Group 7 — transfer / media gating (tasks.md 7.1)
# --------------------------------------------------------------------------


def test_transfer_download_denies_below_l6(vault: Path) -> None:
    """A download hands over the item's COMPLETE bytes, so it is an L6 act.

    Nothing below full disclosure can authorize one: an excerpt-level ceiling
    permits a bounded excerpt, not the file that excerpt came from.
    """
    for ceiling in (
        egress.LEVEL_NONE,
        egress.LEVEL_NOTICE,
        egress.LEVEL_CONSTRAINT,
        egress.LEVEL_ABSTRACT,
        egress.LEVEL_EXCERPT_REDACTED,
        egress.LEVEL_EXCERPT,
    ):
        govern(vault, ceiling=ceiling)
        from exomem.governance import membership, policy

        policy._CACHE.clear()
        membership.clear_memo()
        egress.clear_decision_memo()
        assert not egress.release_allows_download(
            vault, RESTRICTED_PATH,
            principal=RequestPrincipal(audience_id=EXTERNAL, surface="rest"),
        ), f"ceiling {ceiling} wrongly authorized a full-bytes download"


def test_transfer_download_allows_at_l6(vault: Path) -> None:
    govern(vault, ceiling=egress.LEVEL_FULL)
    assert egress.release_allows_download(
        vault, RESTRICTED_PATH,
        principal=RequestPrincipal(audience_id=EXTERNAL, surface="rest"),
    )


def test_transfer_download_is_open_on_an_ungoverned_vault(vault: Path) -> None:
    assert egress.release_allows_download(
        vault, RESTRICTED_PATH,
        principal=RequestPrincipal(audience_id=EXTERNAL, surface="rest"),
    )


def test_transfer_download_blocked_policy_denies(vault: Path) -> None:
    """The `.blocked` fail-closed check at the transfer consumer."""
    govern_broken(vault)
    assert not egress.release_allows_download(
        vault, RESTRICTED_PATH,
        principal=RequestPrincipal(audience_id=EXTERNAL, surface="rest"),
    )


def test_transfer_download_unresolved_principal_denies(vault: Path) -> None:
    from exomem.governance.principal import most_restrictive_principal

    govern(vault, ceiling=egress.LEVEL_FULL)
    assert not egress.release_allows_download(
        vault, RESTRICTED_PATH, principal=most_restrictive_principal(surface="hosted")
    )


def test_hosted_download_route_consults_the_release_decision() -> None:
    """The hosted `/private/exomem/v1/download` handler is where a download
    target is selected, so the gate must sit there — before the file is
    opened, not after the bytes are already streaming."""
    import inspect

    from exomem import server_hosted

    source = inspect.getsource(server_hosted)
    assert "release_allows_download" in source


def test_read_media_frames_gated(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Video frames are the item's content in image form: `op_get_video_frames`
    must consult the release decision BEFORE returning frames, and refuse
    indistinguishably from a missing path below the floor."""
    from exomem import commands

    media_rel = "Knowledge Base/Sources/Media/demo-recording.mp4"
    target = vault / media_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x00\x00\x18ftypmp42fake-video-bytes")

    scope = _gov(vault) / "scopes" / "media.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC1\nname: Media\n"
        'paths: ["Sources/Media/**"]\n',
        encoding="utf-8",
    )
    rule = _gov(vault) / "rules" / "media-external.yaml"
    rule.parent.mkdir(parents=True, exist_ok=True)
    rule.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC2\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FC1"]\n'
        f"audience: {EXTERNAL}\nceiling: {egress.LEVEL_ABSTRACT}\n",
        encoding="utf-8",
    )

    called: list[str] = []

    def _boom(*a, **kw):  # pragma: no cover - must never run
        called.append("extracted")
        raise AssertionError("frames were extracted before the release decision")

    monkeypatch.setattr(commands, "_video_frames_module", lambda: _boom)

    from exomem.governance.principal import request_scope

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_get_video_frames(vault, path=media_rel)
    assert called == []


def test_read_media_frames_blocked_policy_denies(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands
    from exomem.governance.principal import request_scope

    media_rel = "Knowledge Base/Sources/Media/demo-recording.mp4"
    target = vault / media_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x00\x00\x18ftypmp42fake-video-bytes")
    govern_broken(vault)

    monkeypatch.setattr(
        commands,
        "_video_frames_module",
        lambda: (_ for _ in ()).throw(AssertionError("extraction reached")),
    )
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            commands.op_get_video_frames(vault, path=media_rel)


def test_local_download_route_consults_the_release_decision(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local `/download` route is the other place a download target is
    selected, and it bypasses `invoke_command` exactly as design D1 says the
    transfer routes do. Without a gate here, any holder of a valid
    download-scope credential could fetch a withheld file's complete bytes and
    escape every ceiling by asking for the artifact instead of the text.

    Below L6 the refusal must be byte-identical to the response the route
    already produces for a genuinely missing path — same status, same body —
    so a withheld artifact is indistinguishable from one that never existed.
    """
    from starlette.testclient import TestClient

    from exomem import server, upload_tokens

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "sek")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    client = TestClient(server.build_server(require_auth=False).http_app())
    token = upload_tokens.mint("sek", scope="download")
    headers = {"Authorization": f"Bearer {token}"}

    def _get(path: str):
        return client.get("/download", params={"path": path}, headers=headers)

    # Ungoverned vault: unchanged: the file streams.
    baseline = _get(RESTRICTED_PATH)
    assert baseline.status_code == 200, baseline.text
    assert baseline.content == (vault / RESTRICTED_PATH).read_bytes()

    # Governed below L6 -> denied.
    for ceiling in (
        egress.LEVEL_NONE,
        egress.LEVEL_NOTICE,
        egress.LEVEL_ABSTRACT,
        egress.LEVEL_EXCERPT,
    ):
        # The bearer credential on this route is `EXOMEM_UPLOAD_TOKEN` — the
        # OWNER's own key — so an owner-audience ceiling (e.g. an org cap) is
        # what a policy would use to bound it.
        govern(vault, ceiling=ceiling, audience="owner")
        _reset_policy_caches()
        denied = _get(RESTRICTED_PATH)
        assert denied.status_code == 404, (
            f"ceiling {ceiling} served the complete bytes of a withheld file"
        )

    # …and that refusal is byte-identical to a genuinely missing path.
    govern(vault, ceiling=egress.LEVEL_NONE, audience="owner")
    _reset_policy_caches()
    withheld = _get(RESTRICTED_PATH)
    (vault / RESTRICTED_PATH).unlink()
    _reset_policy_caches()
    missing = _get(RESTRICTED_PATH)
    assert withheld.status_code == missing.status_code
    assert withheld.content == missing.content


def test_local_download_route_allows_at_l6(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.testclient import TestClient

    from exomem import server, upload_tokens

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "sek")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    govern(vault, ceiling=egress.LEVEL_FULL, audience="owner")
    _reset_policy_caches()
    client = TestClient(server.build_server(require_auth=False).http_app())
    token = upload_tokens.mint("sek", scope="download")
    response = client.get(
        "/download",
        params={"path": RESTRICTED_PATH},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.content == (vault / RESTRICTED_PATH).read_bytes()


def test_local_download_route_blocked_policy_denies(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `.blocked` fail-closed check at the local transfer consumer."""
    from starlette.testclient import TestClient

    from exomem import server, upload_tokens

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "sek")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    govern_broken(vault)
    _reset_policy_caches()
    from exomem.governance import policy as policy_module

    assert policy_module.load(vault).blocked is True
    client = TestClient(server.build_server(require_auth=False).http_app())
    token = upload_tokens.mint("sek", scope="download")
    response = client.get(
        "/download",
        params={"path": RESTRICTED_PATH},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404, response.text


def test_local_download_route_unresolved_principal_denies(vault: Path) -> None:
    """The route resolves no per-request identity of its own — the credential
    is the vault owner's. An unresolved-but-expected principal bound by an
    outer surface must still fail closed here."""
    from exomem.governance.principal import most_restrictive_principal, request_scope

    govern(vault, ceiling=egress.LEVEL_FULL)
    _reset_policy_caches()
    with request_scope(most_restrictive_principal(surface="rest")):
        assert not egress.release_allows_download(vault, RESTRICTED_PATH)


def test_local_download_principal_separates_owner_from_cf_access(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two credentials reach `/download` and they are NOT the same human.

    `EXOMEM_UPLOAD_TOKEN` is the vault owner's key; a Cloudflare Access
    assertion carries a third party. Resolving both to `owner` would let a
    CF-Access downloader inherit the owner's ceiling — the exact fail-open the
    canonical-audience work exists to prevent. The CF identity folds into the
    same id space MCP and REST use, so a grant authored there applies here.
    """
    from starlette.requests import Request

    from exomem import cf_access, server_transfer, upload_tokens
    from exomem.governance.principal import (
        MOST_RESTRICTIVE_AUDIENCE,
        OWNER_AUDIENCE,
        normalize_audience,
    )

    def _request(**headers: str) -> Request:
        raw = [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
        return Request(
            {"type": "http", "method": "GET", "path": "/download", "headers": raw}
        )

    owner_config = server_transfer.TransferConfig(
        upload_token="sek",
        upload_max_bytes=1024,
        large_upload_base=None,
        cf_team=None,
        cf_aud=None,
        cf_jwks=None,
    )
    # The shared token, and a token minted from it, are both the owner.
    minted = upload_tokens.mint("sek", scope="download")
    for credential in ("sek", minted):
        who = server_transfer.download_principal(
            _request(authorization=f"Bearer {credential}"), owner_config
        )
        assert who.audience_id == OWNER_AUDIENCE, credential
        assert who.resolved is True

    cf_config = server_transfer.TransferConfig(
        upload_token=None,
        upload_max_bytes=1024,
        large_upload_base=None,
        cf_team="team.cloudflareaccess.com",
        cf_aud="aud-123",
        cf_jwks=object(),
    )
    monkeypatch.setattr(
        cf_access,
        "verified_claims",
        lambda *a, **k: {"sub": "auth0|999", "iss": "https://issuer.example"},
    )
    who = server_transfer.download_principal(
        _request(cf_access_jwt_assertion="jwt"), cf_config
    )
    assert who.audience_id == normalize_audience(
        subject="auth0|999", issuer="https://issuer.example"
    )
    assert who.audience_id != OWNER_AUDIENCE

    # Authorized by the route but unresolvable here -> deny, never owner.
    monkeypatch.setattr(cf_access, "verified_claims", lambda *a, **k: None)
    who = server_transfer.download_principal(
        _request(cf_access_jwt_assertion="jwt"), cf_config
    )
    assert who.audience_id == MOST_RESTRICTIVE_AUDIENCE
    assert who.resolved is False


def test_local_download_wrong_scope_token_is_not_the_owner(vault: Path) -> None:
    """An upload-scope token must not resolve as the owner on the download
    route — `upload_tokens` binds scope precisely so one cannot be replayed
    as the other."""
    from starlette.requests import Request

    from exomem import server_transfer, upload_tokens
    from exomem.governance.principal import OWNER_AUDIENCE

    config = server_transfer.TransferConfig(
        upload_token="sek",
        upload_max_bytes=1024,
        large_upload_base=None,
        cf_team=None,
        cf_aud=None,
        cf_jwks=None,
    )
    upload_scoped = upload_tokens.mint("sek", scope="upload")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/download",
            "headers": [(b"authorization", f"Bearer {upload_scoped}".encode())],
        }
    )
    assert server_transfer.download_principal(request, config).audience_id != OWNER_AUDIENCE


# --------------------------------------------------------------------------
# Hosted transfer v2: the public download route is the OTHER binary egress
# --------------------------------------------------------------------------


def _hosted_transfer_app(vault: Path):
    """The real public transfer-v2 surface, pointed at a governed vault.

    Mirrors `tests/test_hosted_transfer_v2._app` — the point is to drive the
    ACTUAL registered route, signed grant and all, not a stand-in.
    """
    import base64
    import contextlib
    import hashlib
    import hmac
    from typing import Any

    from fastmcp import FastMCP

    from exomem import hosted_transfer
    from exomem.hosted_runtime import (
        HostedCellConfig,
        HostedCellLifecycle,
        HostedResourceLimits,
    )
    from exomem.schema import load_source_schema
    from exomem.server_hosted import register_hosted_routes

    origin = "https://substratesystems.io"
    transfer_host = "transfer.substratesystems.io"
    kid = "credential-7"
    credential = "transfer-signing-credential-with-at-least-thirty-two-bytes"
    principal_scope = (
        base64.urlsafe_b64encode(hashlib.sha256(b"principal").digest()).rstrip(b"=").decode()
    )

    class _Replay(RuntimeError):
        code = "HOSTED_JTI_REPLAY"

    class _Security:
        def __init__(self) -> None:
            self.consumed: set[str] = set()

        def verify_transfer_signature(
            self, kid_value: str, ascii_payload: bytes, signature: bytes
        ) -> bool:
            if kid_value != kid:
                return False
            expected = hmac.new(
                credential.encode("utf-8"), ascii_payload, hashlib.sha256
            ).digest()
            return hmac.compare_digest(signature, expected)

        def consume_transfer_jti(self, **values: Any) -> None:
            jti = str(values["jti"])
            if jti in self.consumed:
                raise _Replay
            self.consumed.add(jti)

    state_root = vault.parent / "hosted-state"
    log_root = vault.parent / "hosted-logs"
    config = HostedCellConfig(
        cell_id="cell-alpha",
        vault_root=vault,
        state_root=state_root,
        log_root=log_root,
        service_credential="private-service-credential-with-thirty-two-bytes",
        enforce_transfer_v1_compatibility=False,
        transfer_browser_origin=origin,
        transfer_host=transfer_host,
        resource_limits=HostedResourceLimits(
            storage_bytes=1024 * 1024, upload_bytes=1024 * 1024, worker_count=0
        ),
    )
    state_root.mkdir(parents=True, exist_ok=True)
    log_root.mkdir(parents=True, exist_ok=True)
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True, mutation_authority_ready=True, service_auth_ready=True
    )
    security = _Security()
    app = FastMCP("transfer-v2-governance")
    register_hosted_routes(
        app,
        config=config,
        lifecycle=lifecycle,
        source_schema=load_source_schema(vault),
        transfer_security_authority=security,
        preserve_stream_func=None,
        mutation_guard_factory=lambda _vault: contextlib.nullcontext(),
        runtime_temp_authority=None,
    )

    def mint(path: str, *, jti: str) -> str:
        now = int(time.time())
        return hosted_transfer.mint_transfer_grant_v2(
            signing_credential=credential,
            kid=kid,
            origin=origin,
            operation="download",
            cell_id="cell-alpha",
            principal_scope=principal_scope,
            jti=jti,
            max_bytes=1024 * 1024,
            target={"kind": "download-v1", "path": path},
            issued_at=now,
            not_before=now,
            expires_at=now + 300,
        )

    def get(path: str, *, jti: str):
        import asyncio

        import httpx

        grant = mint(path, jti=jti)

        async def _go():
            transport = httpx.ASGITransport(app=app.http_app())
            async with httpx.AsyncClient(
                transport=transport, base_url=f"https://{transfer_host}"
            ) as client:
                return await client.get(
                    hosted_transfer.TRANSFER_DOWNLOAD_PATH,
                    headers={
                        "Origin": origin,
                        hosted_transfer.TRANSFER_GRANT_HEADER: grant,
                    },
                )

        return asyncio.run(_go())

    return get, security, principal_scope


def _hosted_audience(principal_scope: str) -> str:
    """The audience id the route's own principal resolution produces."""
    from exomem.governance import principal as principal_module

    return principal_module.resolve_hosted_principal(principal_scope).audience_id


def test_hosted_transfer_download_consults_the_release_decision(vault: Path) -> None:
    """The public transfer-v2 download route hands over a file's COMPLETE
    bytes on nothing but a signed grant.

    Its sibling `server_hosted:/private/exomem/v1/download` consults
    `release_allows_download` before opening the file; this one did not. A
    grant is minted by the hosted control plane, so it proves who is asking —
    not what they may see. Without the release consult, any holder of a valid
    download grant escapes every ceiling by asking for the artifact instead of
    the text.
    """
    get, _security, principal_scope = _hosted_transfer_app(vault)
    audience = _hosted_audience(principal_scope)
    body = (vault / RESTRICTED_PATH).read_bytes()

    # Ungoverned: unchanged, the file streams.
    baseline = get(RESTRICTED_PATH, jti="11111111-1111-4111-8111-111111111101")
    assert baseline.status_code == 200, baseline.text
    assert baseline.content == body

    for index, ceiling in enumerate(
        (
            egress.LEVEL_NONE,
            egress.LEVEL_NOTICE,
            egress.LEVEL_ABSTRACT,
            egress.LEVEL_EXCERPT,
        )
    ):
        govern(vault, ceiling=ceiling, audience=audience)
        _reset_policy_caches()
        denied = get(RESTRICTED_PATH, jti=f"11111111-1111-4111-8111-11111111120{index}")
        assert denied.status_code == 404, (
            f"ceiling {ceiling} streamed the complete bytes of a withheld file"
        )
        assert body not in denied.content


def test_hosted_transfer_download_denies_under_a_blocked_policy(vault: Path) -> None:
    """A cold-start compile refusal must not fall through to the open path on
    the binary surface either."""
    get, _security, _scope = _hosted_transfer_app(vault)
    govern_broken(vault)
    _reset_policy_caches()
    from exomem.governance import policy as policy_module

    assert policy_module.load(vault).blocked is True
    denied = get(RESTRICTED_PATH, jti="11111111-1111-4111-8111-111111111301")
    assert denied.status_code == 404
    assert (vault / RESTRICTED_PATH).read_bytes() not in denied.content


def test_hosted_transfer_download_allows_at_l6(vault: Path) -> None:
    """The gate is a release decision, not a blanket denial."""
    get, _security, principal_scope = _hosted_transfer_app(vault)
    govern(vault, ceiling=egress.LEVEL_FULL, audience=_hosted_audience(principal_scope))
    _reset_policy_caches()
    allowed = get(RESTRICTED_PATH, jti="11111111-1111-4111-8111-111111111401")
    assert allowed.status_code == 200, allowed.text
    assert allowed.content == (vault / RESTRICTED_PATH).read_bytes()


def test_hosted_transfer_withheld_refusal_is_identical_to_a_missing_path(
    vault: Path,
) -> None:
    """Existence neutrality, including the JTI.

    The refusal body/status must match a genuinely missing path — and so must
    the grant's consumed state. The route already consumes the JTI BEFORE the
    target is resolved, so a missing path burns the grant; if a withheld path
    left it intact, replaying the same grant would answer "withheld or
    missing?" and the 404 equivalence would be cosmetic.
    """
    get, security, principal_scope = _hosted_transfer_app(vault)
    govern(vault, ceiling=egress.LEVEL_NONE, audience=_hosted_audience(principal_scope))
    _reset_policy_caches()

    withheld_jti = "11111111-1111-4111-8111-111111111501"
    withheld = get(RESTRICTED_PATH, jti=withheld_jti)
    assert withheld_jti in security.consumed, (
        "a release refusal must consume the JTI exactly as a missing path does, "
        "or the grant's replay state becomes the oracle the 404 was hiding"
    )

    (vault / RESTRICTED_PATH).unlink()
    _reset_policy_caches()
    missing_jti = "11111111-1111-4111-8111-111111111502"
    missing = get(RESTRICTED_PATH, jti=missing_jti)

    assert withheld.status_code == missing.status_code == 404
    assert withheld.json() == missing.json()
    assert missing_jti in security.consumed
