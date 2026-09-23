"""A graph-context seed is decided by the page release plane first.

`connect_memory(operation="context")` and the `graph_context` leaf accept a
`unit_ref` or a page `path` as the seed. Whether a unit reference resolves,
the drift the graph reports while resolving it, and whether a page path
assembles a context are all facts about the page, so a page withheld from the
caller must answer exactly as an absent page does.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_semantic_unit_graph import (
    _PAGE_ID,
    _SOURCE,
    _rebuild_with_live_checkpoint,
    _unit_context_fixture,
)

from exomem import commands, server
from exomem.governance import egress, membership, policy
from exomem.governance import principal as principal_module

SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
REST_KEY = "synthetic-rest-key-0123456789"
CF_ISSUER = "https://team.cloudflareaccess.example"
CF_SUBJECT = "synthetic-cf-subject"
CF_AUDIENCE = principal_module.normalize_audience(subject=CF_SUBJECT, issuer=CF_ISSUER)
CF_HEADERS = {"Cf-Access-Jwt-Assertion": "cf-jwt"}
OWNER_HEADERS = {"Authorization": f"Bearer {REST_KEY}"}
BOGUS_ANCHOR = f"exomem://memory/{_PAGE_ID}#no-such-anchor"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _reset_caches()
    yield
    _reset_caches()


def _reset_caches() -> None:
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _withhold_insights(root: Path, *, ceiling: int = egress.LEVEL_NONE) -> None:
    governance = root / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "insights.yaml").write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Insights\n"
        'paths: ["Notes/Insights/**"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "insights.yaml").write_text(
        f'governance_version: 1\nid: {RULE_ID}\nscope_ids: ["{SCOPE_ID}"]\n'
        f'audience: "{CF_AUDIENCE}"\nceiling: {ceiling}\n',
        encoding="utf-8",
    )
    _reset_caches()


def _remove_page(root: Path) -> None:
    (root / _SOURCE).unlink()
    _rebuild_with_live_checkpoint(root)
    _reset_caches()


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


def _context(client: TestClient, body: dict, headers: dict[str, str]):
    response = client.post(
        "/api/connect_memory", json={"operation": "context", **body}, headers=headers
    )
    return response.status_code, response.content


def _cf_principal() -> principal_module.RequestPrincipal:
    digest = hashlib.sha256(f"{CF_ISSUER}\0{CF_SUBJECT}".encode()).hexdigest()
    return principal_module.resolve_rest_principal(f"cf-access:{digest}")


# ---------------------------------------------------------------------------
# connect_memory(operation="context") over REST
# ---------------------------------------------------------------------------


def test_context_unit_seed_of_a_withheld_page_answers_like_an_absent_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, compact, _rich = _unit_context_fixture(vault)
    _withhold_insights(vault)
    client = _rest_client(monkeypatch)
    seeds = [{"unit_ref": compact.unit_ref}, {"unit_ref": BOGUS_ANCHOR}]

    withheld = [_context(client, seed, CF_HEADERS) for seed in seeds]
    _remove_page(vault)
    absent = [_context(client, seed, CF_HEADERS) for seed in seeds]

    assert withheld == absent
    assert all(status == 200 for status, _ in absent)


def test_context_page_seed_of_a_withheld_page_answers_like_an_absent_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, compact, _rich = _unit_context_fixture(vault)
    _withhold_insights(vault)
    client = _rest_client(monkeypatch)
    seeds = [{"path": _SOURCE}, {"path": _SOURCE, "unit_ref": compact.unit_ref}]

    withheld = [_context(client, seed, CF_HEADERS) for seed in seeds]
    _remove_page(vault)
    absent = [_context(client, seed, CF_HEADERS) for seed in seeds]

    assert absent[0][0] == 404
    assert withheld == absent


def test_context_owner_still_seeds_from_the_unit(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, compact, _rich = _unit_context_fixture(vault)
    _withhold_insights(vault)
    client = _rest_client(monkeypatch)

    status, body = _context(client, {"unit_ref": compact.unit_ref}, OWNER_HEADERS)

    assert status == 200, body
    assert b'"unit_status": "found"' in body


# ---------------------------------------------------------------------------
# The graph_context leaf
# ---------------------------------------------------------------------------


def test_graph_context_unit_seed_of_a_withheld_page_answers_like_an_absent_page(
    vault: Path,
) -> None:
    _source, compact, _rich = _unit_context_fixture(vault)
    _withhold_insights(vault)

    def read(ref: str) -> dict:
        with principal_module.request_scope(_cf_principal()):
            return commands.op_graph_context(vault, unit_ref=ref)

    withheld = [read(ref) for ref in (compact.unit_ref, BOGUS_ANCHOR)]
    _remove_page(vault)
    absent = [read(ref) for ref in (compact.unit_ref, BOGUS_ANCHOR)]

    assert withheld == absent
    assert absent[0]["unit_status"] == "missing"


def test_graph_context_unit_seed_below_the_release_floor_is_absent(vault: Path) -> None:
    _source, compact, _rich = _unit_context_fixture(vault)
    _withhold_insights(vault, ceiling=egress.LEVEL_NOTICE)

    with principal_module.request_scope(_cf_principal()):
        notice = commands.op_graph_context(vault, unit_ref=compact.unit_ref)
    _remove_page(vault)
    with principal_module.request_scope(_cf_principal()):
        absent = commands.op_graph_context(vault, unit_ref=compact.unit_ref)

    assert notice == absent


def test_graph_context_owner_still_seeds_from_the_unit(vault: Path) -> None:
    _source, compact, _rich = _unit_context_fixture(vault)
    _withhold_insights(vault)

    with principal_module.request_scope(principal_module.owner_principal(surface="cli")):
        context = commands.op_graph_context(vault, unit_ref=compact.unit_ref)

    assert context["unit_status"] == "found"
    assert context["seeds"]
