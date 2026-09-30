"""An exact semantic-unit read is decided by the page release plane first.

`read_memory(path, unit_ref=...)` returns one unit, its parent citation and up
to 2,400 characters of the surrounding Markdown. Those are the page's own
contents, so the page's release decision governs them: a page withheld from
the caller answers exactly as an absent page does, whatever the unit reference
is, and a unit is served only from a page released to the caller in full.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import semantic_index, server
from exomem.governance import egress, membership, policy
from exomem.governance import principal as principal_module

SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
REST_KEY = "synthetic-rest-key-0123456789"

UNIT_PAGE = "Knowledge Base/Notes/Patterns/unit-read-probe.md"
UNIT_PAGE_TEXT = (
    "---\n"
    "type: insight\n"
    "exomem_id: 12345678-1234-5678-1234-567812345679\n"
    "title: Unit read probe\n"
    "status: active\n"
    "created: 2026-07-16\n"
    "updated: 2026-07-16\n"
    "sources: []\n"
    "tags: [probe]\n"
    "---\n\n"
    "- [config] The unit sentinel is SENTINEL-UNIT-7 ^sentinel\n\n"
    "Surrounding text SENTINEL-CONTEXT-9.\n"
)
SENTINELS = (b"SENTINEL-UNIT-7", b"SENTINEL-CONTEXT-9")

OAUTH_ISSUER = "https://issuer.example"
OAUTH_SUBJECT = "synthetic-oauth-subject"
ALICE = principal_module.normalize_audience(subject=OAUTH_SUBJECT, issuer=OAUTH_ISSUER)

CF_ISSUER = "https://team.cloudflareaccess.example"
CF_SUBJECT = "synthetic-cf-subject"
CF_AUDIENCE = principal_module.normalize_audience(subject=CF_SUBJECT, issuer=CF_ISSUER)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    for leaky in ("EXOMEM_UPLOAD_TOKEN", "EXOMEM_BASE_URL"):
        monkeypatch.delenv(leaky, raising=False)
    _reset_caches()
    yield
    _reset_caches()


def _reset_caches() -> None:
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _govern(vault: Path, *, audience: str, ceiling: int = egress.LEVEL_NONE) -> None:
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


def _unit_refs(vault: Path) -> tuple[str, str, str]:
    """(real ref, bogus anchor on the same page, bogus ref), computed owner-side."""
    target = vault / UNIT_PAGE
    target.write_text(UNIT_PAGE_TEXT, encoding="utf-8")
    state = semantic_index.current_parent_index_state(vault, UNIT_PAGE)
    real = next(unit.unit_ref for unit in state.document.units if unit.unit_ref)
    return real, f"{state.document.parent_ref}#no-such-anchor", "unit:nope"


def _rest_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("EXOMEM_REST_API_KEY", REST_KEY)
    monkeypatch.setenv("EXOMEM_CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.example")
    monkeypatch.setenv("EXOMEM_CF_ACCESS_AUD", "synthetic-aud")
    monkeypatch.setattr("exomem.cf_access.make_jwks_client", lambda _team: object())
    monkeypatch.setattr(
        "exomem.cf_access.verified_claims",
        lambda token, **_kw: {"iss": CF_ISSUER, "sub": CF_SUBJECT} if token == "cf-jwt" else None,
    )
    return TestClient(server.build_server(require_auth=False).http_app())


def _rest_read(client: TestClient, ref: str, headers: dict[str, str]):
    response = client.post(
        "/api/read_memory", json={"path": UNIT_PAGE, "unit_ref": ref}, headers=headers
    )
    return response.status_code, response.content


CF_HEADERS = {"Cf-Access-Jwt-Assertion": "cf-jwt"}
OWNER_HEADERS = {"Authorization": f"Bearer {REST_KEY}"}


def _oauth_principal() -> principal_module.RequestPrincipal:
    return principal_module.RequestPrincipal(
        audience_id=ALICE,
        surface="mcp",
        resolved=True,
        issuer_family="mcp-oauth:" + hashlib.sha256(OAUTH_ISSUER.encode()).hexdigest(),
    )


def _mcp_read(mcp, ref: str, who: principal_module.RequestPrincipal) -> tuple[str, str]:
    with principal_module.request_scope(who):
        try:
            result = asyncio.run(
                mcp.call_tool(
                    "read_memory", {"path": UNIT_PAGE, "unit_ref": ref}, run_middleware=False
                )
            )
        except Exception as exc:  # noqa: BLE001 - the refusal IS the observation
            return type(exc).__name__, str(exc)
    return "result", json.dumps(result.structured_content, sort_keys=True)


# ---------------------------------------------------------------------------
# Withheld page: every unit reference answers exactly as an absent page
# ---------------------------------------------------------------------------


def test_rest_unit_read_of_a_withheld_page_answers_like_an_absent_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = _unit_refs(vault)
    _govern(vault, audience=CF_AUDIENCE)
    client = _rest_client(monkeypatch)

    withheld = [_rest_read(client, ref, CF_HEADERS) for ref in refs]
    (vault / UNIT_PAGE).unlink()
    _reset_caches()
    absent = [_rest_read(client, ref, CF_HEADERS) for ref in refs]

    assert absent[0][0] == 404
    assert len(set(withheld + absent)) == 1, withheld + absent
    for _, body in withheld:
        assert not any(sentinel in body for sentinel in SENTINELS)


def test_mcp_unit_read_of_a_withheld_page_answers_like_an_absent_page(vault: Path) -> None:
    refs = _unit_refs(vault)
    _govern(vault, audience=ALICE)
    mcp = server.build_server(require_auth=False)

    withheld = [_mcp_read(mcp, ref, _oauth_principal()) for ref in refs]
    (vault / UNIT_PAGE).unlink()
    _reset_caches()
    absent = [_mcp_read(mcp, ref, _oauth_principal()) for ref in refs]

    assert absent[0][0] != "result"
    assert "NOT_FOUND" in absent[0][1]
    assert len(set(withheld + absent)) == 1, withheld + absent
    for _, text in withheld:
        assert "SENTINEL" not in text


# ---------------------------------------------------------------------------
# Partial release: a unit is served only from a page released in full
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ceiling",
    [egress.LEVEL_NOTICE, egress.LEVEL_ABSTRACT, egress.LEVEL_EXCERPT],
    ids=["notice", "abstract", "excerpt"],
)
def test_a_page_released_below_full_serves_no_unit(
    vault: Path, monkeypatch: pytest.MonkeyPatch, ceiling: int
) -> None:
    refs = _unit_refs(vault)
    _govern(vault, audience=CF_AUDIENCE, ceiling=ceiling)
    client = _rest_client(monkeypatch)

    partial = [_rest_read(client, ref, CF_HEADERS) for ref in refs]
    (vault / UNIT_PAGE).unlink()
    _reset_caches()
    absent = _rest_read(client, refs[0], CF_HEADERS)

    assert absent[0] == 404
    assert set(partial) == {absent}


def test_a_page_released_in_full_serves_the_unit(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real, _, _ = _unit_refs(vault)
    _govern(vault, audience=CF_AUDIENCE, ceiling=egress.LEVEL_FULL)
    client = _rest_client(monkeypatch)

    status, body = _rest_read(client, real, CF_HEADERS)

    assert status == 200, body
    data = json.loads(body)["data"]
    assert data["status"] == "found"
    assert "SENTINEL-UNIT-7" in data["unit"]["content"]
    assert "SENTINEL-CONTEXT-9" in data["parent_context"]["markdown"]


# ---------------------------------------------------------------------------
# The owner keeps exact unit reads
# ---------------------------------------------------------------------------


def test_rest_owner_still_reads_the_unit(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real, _, bogus = _unit_refs(vault)
    _govern(vault, audience=CF_AUDIENCE)
    client = _rest_client(monkeypatch)

    status, body = _rest_read(client, real, OWNER_HEADERS)
    missing_status, missing_body = _rest_read(client, bogus, OWNER_HEADERS)

    assert status == 200, body
    data = json.loads(body)["data"]
    assert data["status"] == "found"
    assert "SENTINEL-UNIT-7" in data["unit"]["content"]
    assert missing_status == 200, missing_body
    assert json.loads(missing_body)["data"]["status"] == "missing"


def test_mcp_owner_still_reads_the_unit(vault: Path) -> None:
    real, _, _ = _unit_refs(vault)
    _govern(vault, audience=ALICE)
    mcp = server.build_server(require_auth=False)

    kind, text = _mcp_read(mcp, real, principal_module.owner_principal(surface="mcp"))

    assert kind == "result", text
    payload = json.loads(text)
    assert payload.get("result", payload)["status"] == "found"
    assert "SENTINEL-UNIT-7" in text
