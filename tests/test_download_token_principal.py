"""A minted download token carries the principal that minted it.

`transfer_artifact(operation="download")` hands its caller a short-lived bearer
for `/download`. The route decides every requested path through the release
plane under the principal it resolves from that bearer, so the bearer has to
name who minted it: resolving every minted token to the owner would give any
caller that can mint one the owner's full disclosure of every file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from exomem import commands, server, server_transfer, upload_tokens
from exomem.governance import egress, membership, policy
from exomem.governance import principal as principal_module

SECRET = "synthetic-upload-secret-0123456789abcdef"
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
