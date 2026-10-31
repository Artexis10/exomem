"""Relation endpoints, end to end (`promote-referents-without-nudges` task 2.10).

A relation declares which pages it joins. The owner narrows a core relation's
endpoints with an extension, a wider declaration is refused, and an authored
edge outside the declared families is written with a non-blocking finding.
Every case goes through the public MCP tools over an initialized vault.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from exomem import entity_types, graph_sync, relation_endpoints, relation_registry

_CLUB = {
    "label": "Club",
    "guidance": "A membership body with its own meetings and dues.",
    "parent": "organization",
    "attributes": {"folder": "Clubs"},
}
_OWNS_ENDPOINTS = {"subject": ["entity:person", "entity:organization"], "object": ["entity:*"]}


def _served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An initialized vault and an in-process MCP server over it."""
    from conftest import initialize_vault_state_offline

    from exomem import server as server_module
    from exomem.init import init_vault

    vault = tmp_path / "vault"
    init_vault(vault)
    initialize_vault_state_offline(vault, source="relation endpoints")
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    mcp = server_module.build_server(require_auth=False)

    def call(tool: str, arguments: dict) -> dict:
        content = asyncio.run(
            mcp.call_tool(tool, arguments, run_middleware=True)
        ).structured_content
        return content["result"] if set(content) == {"result"} else content

    call.mcp = mcp
    return vault, call


def _relations(call, operation: str, **arguments) -> dict:
    return call(
        "schema_memory", {"subject": "relations", "operation": operation, **arguments}
    )


def _save_extension(call, key: str, definition: dict, subject: str = "relations") -> dict:
    current = call("schema_memory", {"subject": subject, "operation": "inspect"})
    return call(
        "schema_memory",
        {
            "subject": subject,
            "operation": "save",
            "proposal": {"upsert": {key: definition}},
            "expected_hash": current["content_hash"],
            "why": "record who and what the relation joins",
        },
    )


def _extension(parent: str, endpoints: dict | None = None) -> dict:
    attributes: dict = {"direction": "directed"}
    if endpoints is not None:
        attributes["endpoints"] = endpoints
    return {
        "parent": parent,
        "description": "A reviewed refinement of its parent relation.",
        "attributes": attributes,
    }


def _entry(call, key: str) -> dict:
    listed = _relations(call, "inspect", limit=200)
    return next(entry for entry in listed["entries"] if entry["key"] == key)


def test_a_narrowing_extension_saves_and_its_endpoints_survive_a_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _save_extension(call, "club", _CLUB, subject="entity-types")

    # `affiliated_with` joins a person to an organization; a club is an organization.
    narrowed = {"subject": ["entity:person"], "object": ["entity:club"]}
    saved = _save_extension(call, "vault.club_member", _extension("affiliated_with", narrowed))
    assert saved["state"] == "committed", saved
    assert _entry(call, "vault.club_member")["attributes"]["endpoints"] == narrowed

    # A later save, then its revert: the older bytes carry the endpoints and revalidate.
    _save_extension(call, "vault.club_patron", _extension("owns", _OWNS_ENDPOINTS))
    newest = call("schema_memory", {"subject": "relations", "operation": "history"})["versions"][0]
    current = call("schema_memory", {"subject": "relations", "operation": "inspect"})
    restored = call(
        "schema_memory",
        {
            "subject": "relations",
            "operation": "restore",
            "version": newest["version"],
            "expected_hash": current["content_hash"],
            "why": "the patron relation was premature",
        },
    )
    assert restored["state"] == "committed", restored
    keys = {entry["key"] for entry in _relations(call, "inspect", limit=200)["entries"]}
    assert "vault.club_patron" not in keys
    assert _entry(call, "vault.club_member")["attributes"]["endpoints"] == narrowed
    registry = relation_registry.load_registry(vault)
    assert registry.endpoints_of("vault.club_member").object == ("entity:club",)
    assert graph_sync.drain_active_rebuilds(timeout=60)


def test_an_extension_wider_than_its_parent_is_refused_and_nothing_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    before = _relations(call, "inspect")["content_hash"]

    # `affiliated_with` ends at an organization; `entity:*` takes in every entity.
    wider = {"subject": ["entity:person"], "object": ["entity:*"]}
    refused = _save_extension(call, "vault.sits_with", _extension("affiliated_with", wider))

    assert refused["valid"] is False
    assert [item["code"] for item in refused["findings"]] == ["endpoint_outside_parent"]
    assert refused["saved"] is None
    assert _relations(call, "inspect")["content_hash"] == before
    assert relation_registry.load_registry(vault).definition("vault.sits_with") is None


@pytest.mark.parametrize(
    ("token", "code"),
    [
        ("entity:ghost", "unknown_endpoint_type"),
        ("role:ghost", "invalid_endpoints"),
        ("person", "invalid_endpoints"),
    ],
)
def test_endpoints_name_only_registered_entity_types_and_note_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str, code: str
) -> None:
    _vault, call = _served(tmp_path, monkeypatch)
    proposal = {"upsert": {"vault.kept_by": _extension("owns", {"subject": [token], "object": ["entity:*"]})}}

    verdict = _relations(call, "propose", proposal=proposal)

    assert verdict["valid"] is False
    assert [item["code"] for item in verdict["findings"]] == [code]


def test_an_extension_that_declares_nothing_shows_the_endpoints_it_inherits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    proposal = {"upsert": {"vault.keeps": _extension("owns")}}

    verdict = _relations(call, "propose", proposal=proposal)
    [row] = verdict["entries"]
    assert row["inherited"]["endpoints"] == _OWNS_ENDPOINTS

    _save_extension(call, "vault.keeps", _extension("owns"))
    inherited = relation_registry.load_registry(vault).endpoints_of("vault.keeps")
    assert (inherited.subject, inherited.object) == (
        ("entity:person", "entity:organization"),
        ("entity:*",),
    )


def _capture_and_note(call, title: str, relation_row: str | None = None) -> dict:
    captured = call(
        "capture_source",
        {"title": f"{title} source", "source_kind": "session", "content": f"{title} was discussed."},
    )
    return call(
        "create_note",
        {
            "title": title,
            "note_type": "insight",
            "content": (
                f"## Observations\n\n- [fact] {title} was settled.\n"
                + (f"\n## Relations\n\n- {relation_row}\n" if relation_row else "")
            ),
            "sources": [f"{captured['path']}#Capture"],
        },
    )


def test_an_owns_edge_from_a_note_is_written_and_the_receipt_flags_the_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    target = _capture_and_note(call, "Dockside crane")

    # `owns` names an entity as owner; an insight note is not one.
    written = _capture_and_note(
        call, "Crane ownership", f"owns [[{target['path'].removesuffix('.md')}]]"
    )
    assert written["state"] == "committed", written
    assert "owns [[" in (vault / written["path"]).read_text(encoding="utf-8")
    flagged = {item["code"] for item in written["semantic_advisory"]["findings"]}
    assert "endpoint_mismatch" in flagged

    # A relation that declares no endpoints is never flagged for them.
    undeclared = _capture_and_note(
        call, "Crane support", f"supports [[{target['path'].removesuffix('.md')}]]"
    )
    assert undeclared["state"] == "committed", undeclared
    assert "endpoint_mismatch" not in {
        item["code"] for item in (undeclared.get("semantic_advisory") or {}).get("findings", [])
    }


def _entity(call, entity_type: str, name: str, **extra) -> dict:
    return call(
        "connect_memory",
        {
            "operation": "create-entity",
            "entity_type": entity_type,
            "name": name,
            "summary": f"{name} is an invented entity for the endpoint check.",
            **extra,
        },
    )


def test_an_edge_is_judged_against_the_declared_type_and_its_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call = _served(tmp_path, monkeypatch)
    _save_extension(call, "club", _CLUB, subject="entity-types")
    club = _entity(call, "club", "Harbour Club")
    concept = _entity(call, "concept", "Tidal Ledger")

    def affiliation(name: str, target: dict) -> dict:
        relation = {"kind": "affiliated_with", "target": target["path"].removesuffix(".md")}
        return _entity(call, "person", name, relations=[relation])

    # A club is an organization, so a person affiliated with one is within `affiliated_with`.
    within = affiliation("Ben Quay", club)
    assert within["state"] == "committed"
    assert "semantic_advisory" not in within

    # A concept is not an organization: the edge is written and the receipt flags it.
    outside = affiliation("Cy Dock", concept)
    assert outside["state"] == "committed"
    assert [item["code"] for item in outside["semantic_advisory"]["findings"]] == [
        "endpoint_mismatch"
    ]
    assert graph_sync.drain_active_rebuilds(timeout=60)


def test_every_shipped_core_endpoint_names_a_core_entity_type_or_a_note_role() -> None:
    types = entity_types.core_registry()
    core = relation_registry.core_registry()
    declared = {key: core.endpoints_of(key) for key in core.keys}
    assert any(declared.values())
    for key, endpoints in declared.items():
        if endpoints is None:
            continue
        for token in (*endpoints.subject, *endpoints.object):
            namespace, name = relation_endpoints.split(token)
            if namespace == relation_endpoints.ENTITY and name != relation_endpoints.ANY:
                assert types.resolve(name) is not None, f"{key}: {token} is not a core entity type"


def test_propose_relation_carries_endpoints_and_flags_a_wider_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call = _served(tmp_path, monkeypatch)

    def propose(endpoints: dict) -> dict:
        return _relations(
            call,
            "propose-relation",
            proposal={
                "requested_label": "sits_with",
                "parent": "affiliated_with",
                "description": "A person sits with an organization.",
                "direction": "directed",
                "endpoints": endpoints,
            },
        )

    narrower = propose({"subject": ["entity:person"], "object": ["entity:organization"]})
    assert narrower["valid"] is True, narrower
    assert narrower["delta"]["upsert"]["vault.sits_with"]["endpoints"] == {
        "subject": ["entity:person"],
        "object": ["entity:organization"],
    }

    wider = propose({"subject": ["entity:person"], "object": ["entity:*"]})
    assert wider["valid"] is False
    assert [item["code"] for item in wider["findings"]] == ["endpoint_outside_parent"]
