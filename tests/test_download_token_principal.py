"""A minted download token carries the principal that minted it.

`transfer_artifact(operation="download")` hands its caller a short-lived bearer
for `/download`. The route decides every requested path through the release
plane under the principal it resolves from that bearer, so the bearer has to
name who minted it: resolving every minted token to the owner would give any
caller that can mint one the owner's full disclosure of every file.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from exomem import commands, server, server_transfer, upload_tokens
from exomem.governance import egress, membership, policy, scrubber
from exomem.governance import principal as principal_module

SECRET = "synthetic-upload-secret-0123456789abcdef"
PRIVATE_SIGNING_ROOT = "synthetic-private-signing-root-0123456789abcdef"
SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
WITHHELD = "Knowledge Base/Notes/Patterns/kill-switch-for-risky-releases.md"
RELEASED = "Knowledge Base/index.md"

OAUTH_ISSUER = "https://issuer.example"
OAUTH_SUBJECT = "synthetic-oauth-subject"
ALICE = principal_module.normalize_audience(subject=OAUTH_SUBJECT, issuer=OAUTH_ISSUER)

CF_ISSUER = "https://team.cloudflareaccess.example"
CF_SUBJECT = "synthetic-cf-subject"
CF_AUDIENCE = principal_module.normalize_audience(subject=CF_SUBJECT, issuer=CF_ISSUER)


@pytest.fixture(autouse=True)
def _clear_governance_caches():
    _reset_caches()
    yield
    _reset_caches()


@pytest.fixture(autouse=True)
def _transfer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", SECRET)
    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", PRIVATE_SIGNING_ROOT)
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://memory.example")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")


def _reset_caches() -> None:
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _withhold(vault: Path, *, audience: str, ceiling: int = egress.LEVEL_NONE) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "patterns.yaml").write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Patterns\n"
        'paths: ["Notes/Patterns/**"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "patterns.yaml").write_text(
        f'governance_version: 1\nid: {RULE_ID}\nscope_ids: ["{SCOPE_ID}"]\n'
        f'audience: "{audience}"\nceiling: {ceiling}\n',
        encoding="utf-8",
    )
    _reset_caches()


def _client() -> TestClient:
    return TestClient(server.build_server(require_auth=False).http_app())


def _mint_as(vault: Path, who: principal_module.RequestPrincipal) -> str:
    """Mint through the real command leaf, under the principal a surface bound."""
    with principal_module.request_scope(who):
        handoff = commands.op_transfer_artifact(vault, operation="download")
    assert handoff["download_url"] == "https://memory.example/download"
    return handoff["token"]


def _download(client: TestClient, path: str, token: str):
    return client.get(
        "/download", params={"path": path}, headers={"Authorization": f"Bearer {token}"}
    )


def _transfer_config() -> server_transfer.TransferConfig:
    return server_transfer.TransferConfig(
        upload_token=SECRET,
        upload_max_bytes=1024,
        large_upload_base=None,
        cf_team=None,
        cf_aud=None,
        cf_jwks=None,
        principal_signing_root=PRIVATE_SIGNING_ROOT,
    )


def _bearer_request(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/download",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


def _oauth_principal() -> principal_module.RequestPrincipal:
    return principal_module.RequestPrincipal(
        audience_id=ALICE,
        surface="mcp",
        resolved=True,
        issuer_family="mcp-oauth:" + hashlib.sha256(OAUTH_ISSUER.encode()).hexdigest(),
    )


def _cf_access_principal() -> principal_module.RequestPrincipal:
    # The REST boundary's own spelling of a Cloudflare Access identity.
    digest = hashlib.sha256(f"{CF_ISSUER}\0{CF_SUBJECT}".encode()).hexdigest()
    return principal_module.resolve_rest_principal(f"cf-access:{digest}")


# ---------------------------------------------------------------------------
# The minting principal decides the download
# ---------------------------------------------------------------------------


def test_non_owner_oauth_mint_cannot_download_a_page_withheld_from_it(vault: Path) -> None:
    _withhold(vault, audience=ALICE)
    client = _client()
    token = _mint_as(vault, _oauth_principal())

    withheld = _download(client, WITHHELD, token)
    released = _download(client, RELEASED, token)

    assert withheld.status_code == 404, withheld.text
    assert (vault / WITHHELD).read_bytes() not in withheld.content
    # The token is not merely dead: what the minter may read, it downloads.
    assert released.status_code == 200, released.text
    assert released.content == (vault / RELEASED).read_bytes()


def test_cf_access_mint_cannot_download_a_page_withheld_from_it(vault: Path) -> None:
    who = _cf_access_principal()
    assert who.audience_id == CF_AUDIENCE
    _withhold(vault, audience=CF_AUDIENCE)
    client = _client()
    token = _mint_as(vault, who)

    withheld = _download(client, WITHHELD, token)
    released = _download(client, RELEASED, token)

    assert withheld.status_code == 404, withheld.text
    assert released.status_code == 200, released.text


def test_minted_token_resolves_to_the_minting_audience_not_the_owner(vault: Path) -> None:
    for who, audience in (
        (_oauth_principal(), ALICE),
        (_cf_access_principal(), CF_AUDIENCE),
    ):
        token = _mint_as(vault, who)
        resolved = server_transfer.download_principal(_bearer_request(token), _transfer_config())
        assert resolved.audience_id == audience
        assert resolved.audience_id != principal_module.OWNER_AUDIENCE
        assert resolved.resolved is True


def test_owner_mint_still_downloads(vault: Path) -> None:
    # The page is withheld from another audience, not from the owner.
    _withhold(vault, audience=ALICE)
    client = _client()
    token = _mint_as(vault, principal_module.owner_principal(surface="mcp"))

    response = _download(client, WITHHELD, token)

    assert response.status_code == 200, response.text
    assert response.content == (vault / WITHHELD).read_bytes()
    resolved = server_transfer.download_principal(_bearer_request(token), _transfer_config())
    assert resolved.audience_id == principal_module.OWNER_AUDIENCE


def test_marked_raw_orphan_is_downloadable_only_by_the_owner_without_policy(vault: Path) -> None:
    """A copied raw artifact must not become public when its companion is lost.
    The owner's transfer bearer and owner-audience capabilities still serve it;
    a guest's capability, or one forged with the public bearer, reads as absent."""
    path = "Knowledge Base/Evidence/Test/export/__exomem_raw_v1__sample.csv"
    artifact = vault / path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"timestamp,latitude\n2026-10-01,12.345\n")
    client = _client()
    token = _mint_as(vault, _oauth_principal())
    response = _download(client, path, token)
    shared_key = _download(client, path, SECRET)
    legacy_owner = _download(client, path, upload_tokens.mint_bound(SECRET, audience="owner"))
    forged_token = upload_tokens.mint_principal(
        SECRET, principal_module.owner_principal(surface="library"),
    )
    forged_local = _download(client, path, forged_token)
    artifact.unlink()
    absent = _download(client, path, token)
    forged_absent = _download(client, path, forged_token)

    assert response.status_code == absent.status_code == 404
    assert response.content == absent.content
    assert shared_key.status_code == legacy_owner.status_code == 200
    assert shared_key.content == legacy_owner.content == b"timestamp,latitude\n2026-10-01,12.345\n"
    assert forged_local.status_code == forged_absent.status_code
    assert forged_local.content == forged_absent.content


def test_raw_preservation_release_revocation_and_held_download(vault: Path, monkeypatch) -> None:
    """Release covers one live recipient and the exact bytes sent, never a swapped snapshot."""
    from exomem.writer_lease import invoke_command

    govern = next(command for command in commands.PRODUCT_COMMANDS if command.name == "govern_memory")

    raw = "timestamp,latitude\n2026-10-01,12.345\n"
    local = principal_module.owner_principal(surface="library")
    recipient = _oauth_principal()
    other = _cf_access_principal()
    client = _client()
    with principal_module.request_scope(local):
        saved = commands.op_preserve_evidence(
            vault, scope="Test", category="raw", filename="sample.csv", content=raw,
            raw_protection=True,
        )
    path = saved["path"]
    assert (vault / path).read_bytes() == raw.encode()
    local_token = _mint_as(vault, local)
    token = _mint_as(vault, recipient)
    assert _download(client, path, local_token).content == raw.encode()
    assert _download(client, path, token).status_code == 404
    with principal_module.request_scope(local):
        grant = invoke_command(
            govern, vault, operation="grant", scope="standing", grant_id=RULE_ID,
            path=path, audience=recipient.audience_id,
            raw_release={"version": 1, "surface": recipient.surface,
                         "issuer_family": recipient.issuer_family,
                         "purpose": None, "includes_location": True},
        )
    assert grant["ok"], grant
    assert _download(client, path, token).content == raw.encode()
    assert _download(client, saved["sidecar_path"], token).content == (vault / saved["sidecar_path"]).read_bytes()
    with principal_module.request_scope(recipient):
        page = egress.postfilter("read_memory", commands.op_read_memory(
            vault, path=saved["sidecar_path"], include_raw=True,
        ), vault)
    assert page["content"] == (vault / saved["sidecar_path"]).read_text()
    assert _download(client, path, _mint_as(vault, other)).status_code == 404
    assert not egress.release_allows_frames(vault, path, principal=recipient)
    assert egress.release_allows_frames(vault, path, principal=local)

    from dataclasses import replace

    from exomem import reserved_paths

    read = reserved_paths.read_generic_bytes

    def swapped(root, relative, **kwargs):
        snapshot = read(root, relative, **kwargs)
        return replace(snapshot, data=b"different bytes") if relative == path else snapshot

    with monkeypatch.context() as patch:
        patch.setattr(reserved_paths, "read_generic_bytes", swapped)
        assert _download(client, path, token).status_code == 404
    with principal_module.request_scope(local):
        invoke_command(govern, vault, operation="revoke", scope="standing", grant_id=RULE_ID)
    assert _download(client, path, token).status_code == 404


@pytest.mark.parametrize("signing_root", [None, SECRET], ids=["missing-private-root", "root-is-public-bearer"])
def test_non_private_signer_keeps_legacy_transfer_for_the_owner(vault, monkeypatch, signing_root):
    """Static-bearer deployments serve the owner's protected original through the
    owner-audience capability, and cannot forge a full principal."""
    if signing_root is None:
        monkeypatch.delenv("EXOMEM_JWT_SIGNING_KEY")
    else:
        monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", signing_root)
    local = principal_module.owner_principal(surface="library")
    with principal_module.request_scope(local):
        saved = commands.op_preserve_evidence(
            vault, scope="Test", category="raw", filename="local.txt",
            content="exact local original\n", raw_protection=True,
        )
        handoff = commands.op_transfer_artifact(vault, operation="download")
        page = commands.op_read_memory(vault, path=saved["path"], include_raw=True)
    assert page["content"] == "exact local original\n"
    assert handoff["preserves_ingress_principal"] is False
    assert "raw_transfer_unavailable_reason" not in handoff
    assert upload_tokens.bound_audience(handoff["token"], SECRET) == "owner"
    client = _client()
    assert _download(client, RELEASED, handoff["token"]).content == (vault / RELEASED).read_bytes()
    assert _download(client, saved["path"], handoff["token"]).content == b"exact local original\n"
    forged = upload_tokens.mint_principal(SECRET, local)
    assert _download(client, saved["path"], forged).status_code == 401


def test_raw_transfer_rechecks_verified_session_purpose_and_closure(vault: Path, tmp_path, monkeypatch) -> None:
    """A signed transfer cannot retain a superseded purpose or closed session."""
    import sqlite3

    from test_authorization_session_lifecycle import NOW, _custody, _file_connection

    from exomem.governance import authorization_session_authority as authority
    from exomem.governance import authorization_session_lifecycle as lifecycle
    from exomem.governance import raw_protection
    from exomem.writer_lease import invoke_command

    database = tmp_path / "sessions.sqlite"
    connection, migration = _file_connection(database)
    custody = _custody(migration.activation_state_digest)
    recipient = _oauth_principal()
    issued = lifecycle.open_session(
        connection, custody=custody, principal_id=recipient.audience_id,
        issuer_family=recipient.issuer_family, now=NOW, ttl_seconds=600,
    )
    context = lifecycle.resume_session(
        connection, custody=custody, bearer=issued.bearer,
        principal_id=recipient.audience_id, issuer_family=recipient.issuer_family, now=NOW,
    )
    session_recipient = recipient.with_verified_authorization_session(
        context, issuer_family=recipient.issuer_family,
    )
    client = _client()
    local = principal_module.owner_principal(surface="library")
    govern = next(command for command in commands.PRODUCT_COMMANDS if command.name == "govern_memory")
    with principal_module.request_scope(local):
        saved = commands.op_preserve_evidence(
            vault, scope="Test", category="raw", filename="purpose.csv",
            content="timestamp,latitude\n2026-10-01,12.345\n", raw_protection=True,
        )
        grant = invoke_command(
            govern, vault, operation="grant", scope="standing", grant_id=RULE_ID,
            path=saved["path"], audience=recipient.audience_id,
            raw_release={"version": 1, "surface": recipient.surface,
                         "issuer_family": recipient.issuer_family,
                         "purpose": "route-audit", "includes_location": True},
        )
    assert grant["ok"], grant
    monkeypatch.setattr(raw_protection.time, "time", lambda: NOW)
    monkeypatch.setattr(raw_protection.store, "open_authorization_session_connection", lambda _root: sqlite3.connect(database))
    monkeypatch.setattr(raw_protection.authorization_custody, "load_authorization_custody", lambda _root, **_kw: custody)
    token = _mint_as(vault, session_recipient)
    # A request argument is not the verified session's purpose.
    assert _download(client, saved["path"], _mint_as(vault, recipient.with_purpose("route-audit"))).status_code == 404
    assert _download(client, saved["path"], token).status_code == 404
    authority.declare_purpose(
        connection, context=context, audience=recipient.audience_id,
        purpose="route-audit", now=NOW, expires_at=NOW + 300,
    )
    released = _download(client, saved["path"], token)
    assert released.status_code == 200, released.text
    assert released.content == (vault / saved["path"]).read_bytes()
    authority.declare_purpose(
        connection, context=context, audience=recipient.audience_id,
        purpose="unrelated", now=NOW, expires_at=NOW + 300,
    )
    assert _download(client, saved["path"], token).status_code == 404
    authority.declare_purpose(
        connection, context=context, audience=recipient.audience_id,
        purpose="route-audit", now=NOW, expires_at=NOW + 300,
    )
    lifecycle.close_verified_session(connection, custody=custody, context=context, now=NOW)
    assert _download(client, saved["path"], token).status_code == 404
    connection.close()


def test_raw_upload_secret_is_still_the_owner(vault: Path) -> None:
    _withhold(vault, audience=ALICE)
    client = _client()

    resolved = server_transfer.download_principal(_bearer_request(SECRET), _transfer_config())
    response = _download(client, WITHHELD, SECRET)

    assert resolved.audience_id == principal_module.OWNER_AUDIENCE
    assert resolved.resolved is True
    assert response.status_code == 200, response.text


def test_unresolved_minter_binds_the_fail_closed_floor(vault: Path) -> None:
    token = _mint_as(vault, principal_module.most_restrictive_principal(surface="mcp"))
    resolved = server_transfer.download_principal(_bearer_request(token), _transfer_config())

    assert resolved.audience_id == principal_module.MOST_RESTRICTIVE_AUDIENCE
    assert resolved.resolved is False


def test_unbound_mint_is_not_the_owner(vault: Path) -> None:
    """No surface bound a principal: minting must not default to the owner."""
    token = commands.op_transfer_artifact(vault, operation="download")["token"]
    resolved = server_transfer.download_principal(_bearer_request(token), _transfer_config())

    assert resolved.audience_id != principal_module.OWNER_AUDIENCE
    assert resolved.resolved is False


@pytest.mark.parametrize(
    "audience",
    [principal_module.OWNER_AUDIENCE, principal_module.MOST_RESTRICTIVE_AUDIENCE, ALICE],
)
def test_bound_token_survives_the_terminal_scrubber(audience: str) -> None:
    token = upload_tokens.mint_bound(SECRET, audience=audience)
    handoff = {"token": token, "ttl_seconds": 900, "download_url": "https://memory.example/download"}

    scrubbed, changed = scrubber.scrub_value(handoff)

    assert changed is False
    assert scrubbed["token"] == token


def test_cf_access_rest_round_trip_through_the_dispatcher(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mint over REST as a Cloudflare Access principal, then download.

    The handoff crosses the shared dispatcher, whose terminal credential
    scrubber rewrites high-entropy runs in unlabelled fields. A bound token
    must survive it intact, or a non-owner receives a dead capability."""
    _withhold(vault, audience=CF_AUDIENCE)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "synthetic-rest-key-0123456789")
    monkeypatch.setenv("EXOMEM_CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.example")
    monkeypatch.setenv("EXOMEM_CF_ACCESS_AUD", "synthetic-aud")
    monkeypatch.setattr("exomem.cf_access.make_jwks_client", lambda _team: object())
    monkeypatch.setattr(
        "exomem.cf_access.verified_claims",
        lambda token, **_kw: {"iss": CF_ISSUER, "sub": CF_SUBJECT} if token == "cf-jwt" else None,
    )
    client = _client()

    minted = client.post(
        "/api/transfer_artifact",
        json={"operation": "download"},
        headers={"Cf-Access-Jwt-Assertion": "cf-jwt"},
    )
    assert minted.status_code == 200, minted.text
    token = minted.json()["data"]["token"]
    assert upload_tokens.bound_principal(token, PRIVATE_SIGNING_ROOT).audience_id == CF_AUDIENCE

    withheld = _download(client, WITHHELD, token)
    released = _download(client, RELEASED, token)

    assert withheld.status_code == 404, withheld.text
    assert released.status_code == 200, released.text
    assert released.content == (vault / RELEASED).read_bytes()


# ---------------------------------------------------------------------------
# Tokens minted before the binding existed
# ---------------------------------------------------------------------------


def test_pre_binding_download_token_is_refused(vault: Path) -> None:
    client = _client()
    legacy = upload_tokens.mint(SECRET, scope="download")

    response = _download(client, RELEASED, legacy)
    resolved = server_transfer.download_principal(_bearer_request(legacy), _transfer_config())

    assert response.status_code == 401, response.text
    assert resolved.audience_id != principal_module.OWNER_AUDIENCE
    assert resolved.resolved is False


def test_review_probe_minted_download_token_is_not_owner() -> None:
    """Regression for the review probe: mint through the endpoint helper with
    no principal in hand, present the token to `/download`'s resolver."""
    handoff = upload_tokens.mint_for_endpoint(SECRET, "https://memory.example", scope="download")
    token = handoff["token"]

    resolved = server_transfer.download_principal(_bearer_request(token), _transfer_config())

    assert resolved.audience_id != principal_module.OWNER_AUDIENCE
    assert resolved.resolved is False


# ---------------------------------------------------------------------------
# One refusal: withheld and missing are indistinguishable
# ---------------------------------------------------------------------------


def test_withheld_and_missing_refuse_identically(vault: Path) -> None:
    _withhold(vault, audience=ALICE)
    client = _client()
    token = _mint_as(vault, _oauth_principal())

    withheld = _download(client, WITHHELD, token)
    (vault / WITHHELD).unlink()
    _reset_caches()
    missing = _download(client, WITHHELD, token)

    assert missing.status_code == 404
    assert withheld.status_code == missing.status_code
    assert withheld.headers.get("content-type") == missing.headers.get("content-type")
    assert withheld.content == missing.content


def test_withheld_refusal_does_not_reveal_the_on_disk_spelling(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a case-insensitive filesystem (NTFS, APFS) the resolver re-spells an
    EXISTING path to its on-disk casing but leaves a missing one as typed. A
    refusal that echoed the resolver's spelling would answer "does this exist,
    and what is it really called?" for a file the caller may not see."""
    real = server_transfer.resolve_under_vault

    def case_folding(vault_root: Path, path: str, **kwargs):
        wanted = str(path).strip().replace("\\", "/").lstrip("/").casefold()
        for candidate in Path(vault_root).rglob("*"):
            spelled = candidate.relative_to(vault_root).as_posix()
            if spelled.casefold() == wanted:
                return real(vault_root, spelled, **kwargs)
        return real(vault_root, path, **kwargs)

    monkeypatch.setattr(server_transfer, "resolve_under_vault", case_folding)
    _withhold(vault, audience=principal_module.OWNER_AUDIENCE)
    client = _client()
    typed = WITHHELD.lower()

    withheld = _download(client, typed, SECRET)
    (vault / WITHHELD).unlink()
    _reset_caches()
    missing = _download(client, typed, SECRET)

    assert missing.status_code == 404
    assert withheld.status_code == missing.status_code
    assert withheld.content == missing.content
    assert WITHHELD.encode() not in withheld.content


# ---------------------------------------------------------------------------
# Malformed bearers are refused, never a server error
# ---------------------------------------------------------------------------


def _unraising_client() -> TestClient:
    return TestClient(server.build_server(require_auth=False).http_app(), raise_server_exceptions=False)


@pytest.mark.parametrize(
    "token",
    [
        "v2." + "9" * 5000 + ".00." + "0" * 64,
        "v1." + "9" * 5000 + "." + "0" * 64,
    ],
    ids=["bound-5000-digits", "v1-5000-digits"],
)
def test_unparseable_expiry_is_refused_on_both_transfer_routes(vault: Path, token: str) -> None:
    client = _unraising_client()
    # Raw header bytes: a non-ASCII bearer arrives latin-1 decoded, as on the wire.
    auth = [(b"authorization", f"Bearer {token}".encode("latin-1"))]

    download = client.get("/download", params={"path": RELEASED}, headers=auth)
    upload = client.post(
        "/upload",
        files={"file": ("a.bin", b"x", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers=auth,
    )

    assert download.status_code == 401, download.text
    assert upload.status_code == 401, upload.text


@pytest.mark.parametrize("spelling", ["Knowledge Base/Notes/Patterns", "Knowledge Base/Notes/Patterns/"])
def test_a_folder_refuses_exactly_like_a_missing_path(vault: Path, spelling: str) -> None:
    """A folder is never a download, and saying so would confirm that a folder
    inside a withheld scope exists."""
    _withhold(vault, audience=ALICE)
    client = _client()
    token = _mint_as(vault, _oauth_principal())

    folder = _download(client, spelling, token)
    shutil.rmtree(vault / "Knowledge Base" / "Notes" / "Patterns")
    _reset_caches()
    missing = _download(client, spelling, token)

    assert missing.status_code == 404
    assert folder.status_code == missing.status_code
    assert folder.content == missing.content


INVALID_PATH_BODY = {"code": "INVALID_PATH", "reason": "path is not a vault-relative file path"}


@pytest.mark.parametrize(
    "requested",
    [
        "../../etc/hostname",
        "Knowledge Base/Notes/escape-link/hostname",
        "Knowledge Base/Notes/escape-link/no-such-file",
    ],
    ids=["dotdot", "symlink-existing", "symlink-missing"],
)
def test_an_escaping_path_is_refused_without_echoing_server_paths(
    vault: Path, tmp_path: Path, requested: str
) -> None:
    outside = tmp_path / "outside-the-vault"
    outside.mkdir()
    (outside / "hostname").write_text("outside\n", encoding="utf-8")
    try:
        (vault / "Knowledge Base" / "Notes" / "escape-link").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    client = _client()

    response = _download(client, requested, SECRET)

    assert response.status_code == 400
    assert response.json() == INVALID_PATH_BODY
    for server_path in (str(vault), str(outside), str(tmp_path)):
        assert server_path not in response.text


NON_ASCII_BEARERS = [b"Bearer \xe9abc", b"Bearer v1.\xb2." + b"0" * 64]
NON_ASCII_IDS = ["latin-1-letter", "v1-superscript-expiry"]


@pytest.mark.parametrize("header", NON_ASCII_BEARERS, ids=NON_ASCII_IDS)
def test_non_ascii_bearer_is_refused_on_download_and_upload(vault: Path, header: bytes) -> None:
    client = _unraising_client()
    auth = [(b"authorization", header)]

    download = client.get("/download", params={"path": RELEASED}, headers=auth)
    upload = client.post(
        "/upload",
        files={"file": ("a.bin", b"x", "application/octet-stream")},
        data={"scope": "S", "category": "C"},
        headers=auth,
    )

    assert download.status_code == 401, download.text
    assert upload.status_code == 401, upload.text


@pytest.mark.parametrize("header", NON_ASCII_BEARERS, ids=NON_ASCII_IDS)
def test_non_ascii_bearer_resolves_to_no_principal(header: bytes) -> None:
    request = Request(
        {"type": "http", "method": "GET", "path": "/download", "headers": [(b"authorization", header)]}
    )

    resolved = server_transfer.download_principal(request, _transfer_config())

    assert resolved.audience_id == principal_module.MOST_RESTRICTIVE_AUDIENCE
    assert resolved.resolved is False


@pytest.mark.parametrize("header", NON_ASCII_BEARERS, ids=NON_ASCII_IDS)
def test_non_ascii_bearer_is_refused_on_rest(vault: Path, monkeypatch: pytest.MonkeyPatch, header: bytes) -> None:
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "synthetic-rest-key-0123456789")
    client = _unraising_client()

    response = client.post(
        "/api/read_memory",
        json={"path": RELEASED},
        headers=[(b"authorization", header), (b"content-type", b"application/json")],
    )

    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# Direct page reads decide release before they decode
# ---------------------------------------------------------------------------

UNDECODABLE = "Knowledge Base/Notes/Patterns/undecodable-note.md"
PAGE_BYTES = b"---\ntype: pattern\n---\nwithheld body\n"
NON_UTF8_BYTES = b"AB\xfeCD"


def _cf_rest_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "synthetic-rest-key-0123456789")
    monkeypatch.setenv("EXOMEM_CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.example")
    monkeypatch.setenv("EXOMEM_CF_ACCESS_AUD", "synthetic-aud")
    monkeypatch.setattr("exomem.cf_access.make_jwks_client", lambda _team: object())
    monkeypatch.setattr(
        "exomem.cf_access.verified_claims",
        lambda token, **_kw: {"iss": CF_ISSUER, "sub": CF_SUBJECT} if token == "cf-jwt" else None,
    )
    return _client()


def _three_states(vault: Path, read) -> list:
    """The same requested path, withheld as a page, withheld as undecodable
    bytes, and absent."""
    target = vault / UNDECODABLE
    answers = []
    for data in (PAGE_BYTES, NON_UTF8_BYTES, None):
        if data is None:
            target.unlink()
        else:
            target.write_bytes(data)
        _reset_caches()
        answers.append(read())
    return answers


def test_rest_read_memory_refuses_a_withheld_undecodable_file_like_a_missing_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _withhold(vault, audience=CF_AUDIENCE)
    client = _cf_rest_client(monkeypatch)

    def read():
        response = client.post(
            "/api/read_memory",
            json={"path": UNDECODABLE},
            headers={"Cf-Access-Jwt-Assertion": "cf-jwt"},
        )
        return response.status_code, response.content

    page, undecodable, missing = _three_states(vault, read)

    assert missing[0] == 404
    assert page == undecodable == missing
    assert b"0xfe" not in undecodable[1]


def _refusal(call) -> str:
    try:
        call()
    except ValueError as exc:
        return str(exc)
    raise AssertionError("the read returned instead of refusing")


@pytest.mark.parametrize("door", ["read_memory", "get", "fetch"])
def test_every_direct_read_door_refuses_withheld_undecodable_like_missing(
    vault: Path, door: str
) -> None:
    _withhold(vault, audience=CF_AUDIENCE)
    leaf = {
        "read_memory": lambda: commands.op_read_memory(vault, path=UNDECODABLE),
        "get": lambda: commands.op_get(vault, path=UNDECODABLE),
        "fetch": lambda: commands.op_fetch(vault, id=UNDECODABLE),
    }[door]

    def read():
        with principal_module.request_scope(_cf_access_principal()):
            return _refusal(leaf)

    page, undecodable, missing = _three_states(vault, read)

    assert missing.startswith("NOT_FOUND:")
    assert page == undecodable == missing


def test_exact_unit_read_refuses_withheld_undecodable_like_missing(vault: Path) -> None:
    _withhold(vault, audience=CF_AUDIENCE)
    target = vault / UNDECODABLE

    def read():
        with principal_module.request_scope(_cf_access_principal()):
            return _refusal(
                lambda: commands.op_read_memory(vault, path=UNDECODABLE, unit_ref="unit:any")
            )

    target.write_bytes(NON_UTF8_BYTES)
    undecodable = read()
    target.unlink()
    missing = read()

    assert undecodable == missing


@pytest.mark.parametrize("helper", ["get_page", "get_frontmatter"])
def test_page_helpers_refuse_withheld_undecodable_like_missing(vault: Path, helper: str) -> None:
    from exomem import get_frontmatter, get_page

    _withhold(vault, audience=CF_AUDIENCE)
    call = {
        "get_page": lambda: get_page.get_page(vault, path=UNDECODABLE),
        "get_frontmatter": lambda: get_frontmatter.get_frontmatter(vault, path=UNDECODABLE),
    }[helper]
    target = vault / UNDECODABLE

    def read():
        with principal_module.request_scope(_cf_access_principal()):
            try:
                call()
            except (get_page.GetError, get_frontmatter.GetFrontmatterError) as exc:
                return exc.code, exc.reason
        raise AssertionError("the helper returned instead of refusing")

    target.write_bytes(NON_UTF8_BYTES)
    undecodable = read()
    target.unlink()
    _reset_caches()
    missing = read()

    assert missing[0] == "NOT_FOUND"
    assert undecodable == missing


def test_a_released_undecodable_file_is_unreadable_without_echoing_its_bytes(vault: Path) -> None:
    """Released to the caller, the file is still reported unreadable — but the
    reason names no byte value or offset."""
    target = vault / UNDECODABLE
    target.write_bytes(NON_UTF8_BYTES)

    with principal_module.request_scope(principal_module.owner_principal(surface="rest")):
        refusal = _refusal(lambda: commands.op_read_memory(vault, path=UNDECODABLE))

    assert refusal.startswith("UNREADABLE:")
    assert "0xfe" not in refusal
    assert "position" not in refusal


def _withheld_outcomes(vault: Path) -> list[tuple[str, str]]:
    events = vault / "Knowledge Base" / "_Governance" / "events"
    records = (
        [
            json.loads(line)
            for path in sorted(events.rglob("*.jsonl"))
            if path.is_file()
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        if events.exists()
        else []
    )
    outcomes = []
    for record in records:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
        for outcome in payload.get("outcomes") or ():
            outcomes.append((str(outcome.get("decision")), str(outcome.get("audience"))))
    return outcomes


def test_a_withheld_undecodable_read_leaves_the_same_receipt_as_a_decodable_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _withhold(vault, audience=CF_AUDIENCE)
    client = _cf_rest_client(monkeypatch)
    target = vault / UNDECODABLE

    def read_and_collect(data: bytes) -> list[tuple[str, str]]:
        target.write_bytes(data)
        _reset_caches()
        before = len(_withheld_outcomes(vault))
        response = client.post(
            "/api/read_memory",
            json={"path": UNDECODABLE},
            headers={"Cf-Access-Jwt-Assertion": "cf-jwt"},
        )
        assert response.status_code == 404, response.text
        return _withheld_outcomes(vault)[before:]

    decodable = read_and_collect(PAGE_BYTES)
    undecodable = read_and_collect(NON_UTF8_BYTES)

    assert ("withheld", CF_AUDIENCE) in decodable
    assert undecodable == decodable
