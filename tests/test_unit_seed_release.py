"""A graph-context seed is decided by the page release plane first.

`connect_memory(operation="context")` and the `graph_context` leaf accept a
`unit_ref` or a page `path` as the seed. Whether a unit reference resolves,
the drift the graph reports while resolving it, and whether a page path
assembles a context are all facts about the page, so a page withheld from the
caller must answer exactly as an absent page does.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient
from test_semantic_unit_graph import (
    _PAGE_ID,
    _SOURCE,
    _rebuild_with_live_checkpoint,
    _unit_context_fixture,
)

from exomem import commands, semantic_index, server
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


# ---------------------------------------------------------------------------
# Refs that name their page by path: exomem://vault/ and exomem://source/
# ---------------------------------------------------------------------------

NOID = "Knowledge Base/Notes/Insights/noid-page.md"
NOID_TEXT = (
    "---\ntype: insight\ntitle: No id\n---\n# No id\n\n"
    "## Observations\n- [configuration] Idless sentinel IDLESS-3 ^q-1\n"
)


def _idless_seeds(vault: Path) -> list[str]:
    _unit_context_fixture(vault)
    (vault / NOID).write_text(NOID_TEXT, encoding="utf-8")
    _rebuild_with_live_checkpoint(vault)
    _reset_caches()
    state = semantic_index.current_parent_index_state(vault, NOID)
    real = [unit.unit_ref for unit in state.document.units if unit.unit_ref]
    assert real and real[0].startswith("exomem://vault/"), real
    return [
        *real,
        f"exomem://vault/{quote(NOID)}#nope",
        f"exomem://source/{quote(NOID[:-3])}#q-1",
        f"exomem://vault/{quote(_SOURCE)}#compact-1",
    ]


def _remove_idless(vault: Path) -> None:
    (vault / NOID).unlink()
    _rebuild_with_live_checkpoint(vault)
    _reset_caches()


def test_rest_path_named_unit_seeds_of_a_withheld_page_answer_like_an_absent_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeds = _idless_seeds(vault)
    _withhold_insights(vault)
    client = _rest_client(monkeypatch)
    requests = [
        {"operation": operation, "unit_ref": seed}
        for seed in seeds
        for operation in ("context", "graph-context")
    ]

    def run() -> list:
        return [
            client.post("/api/connect_memory", json=body, headers=CF_HEADERS).content
            for body in requests
        ]

    withheld = run()
    _remove_idless(vault)
    (vault / _SOURCE).unlink()
    _rebuild_with_live_checkpoint(vault)
    _reset_caches()
    absent = run()

    assert withheld == absent
    assert not any(b"IDLESS" in body for body in withheld)


def test_mcp_path_named_unit_seeds_of_a_withheld_page_answer_like_an_absent_page(
    vault: Path,
) -> None:
    seeds = _idless_seeds(vault)
    _withhold_insights(vault)
    mcp = server.build_server(require_auth=False)
    who = principal_module.RequestPrincipal(audience_id=CF_AUDIENCE, surface="mcp")

    def run() -> list[str]:
        out = []
        for seed in seeds:
            with principal_module.request_scope(who):
                result = asyncio.run(
                    mcp.call_tool(
                        "connect_memory",
                        {"operation": "context", "unit_ref": seed},
                        run_middleware=False,
                    )
                )
            out.append(json.dumps(result.structured_content, sort_keys=True))
        return out

    withheld = run()
    _remove_idless(vault)
    (vault / _SOURCE).unlink()
    _rebuild_with_live_checkpoint(vault)
    _reset_caches()
    absent = run()

    assert withheld == absent
    assert not any("IDLESS" in text for text in withheld)
