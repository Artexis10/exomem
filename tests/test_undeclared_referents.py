"""The write-time `undeclared_referents` block, read from the MCP response a hookless client sees.

Every case drives the in-process MCP server over an initialized vault, so the block is read
after the terminal projection and egress, and a promotion route is followed with the arguments
the block names. Fixtures are invented.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from granted_writer import CLIENT, ISSUER, grant_writer
from test_recent_promotions import _as, _log_entry
from test_vocabulary_registries import _served

from exomem import capture_sweep, due_state, envelope, episode_workflow, epistemic_graph, graph_sync, prominence
from exomem import vault as vault_module
from exomem.governance import egress, membership, policy
from exomem.governance import principal as principal_module
from exomem.governance.principal import OriginSessionBinding, RequestPrincipal
from exomem.vocabulary.registry import PUBLIC_INSTANCE

FAMILY_REF = "exomem://review/family/undeclared_referents"
NO_RELATION = "No honest relation exists for this isolated fixture."
EXTERNAL = RequestPrincipal(audience_id="external", surface="mcp", issuer_family="example-issuer")


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "config.json"))
    for name in ("EXOMEM_PROMINENCE", "EXOMEM_SURFACE", "EXOMEM_HOSTED_CELL"):
        monkeypatch.delenv(name, raising=False)
    due_state.reset_emission_state()
    yield
    due_state.reset_emission_state()


def _served_with_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The served vault with its graph index built, so `entity_candidate` can read link dependencies."""
    vault, call = _served(tmp_path, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return vault, call


def _body(title: str, *names: str) -> str:
    links = ", ".join(f"[[{name}]]" for name in names)
    return f"## Observations\n\n- [finding] The review of {title} leaned on {links}.\n"


def _committed(call, tool: str, arguments: dict, review_hash: str) -> dict:
    """Commit once; when the write needs a relation review, commit again with `reviewed_none`."""
    written = call(tool, arguments)
    if written.get("error", {}).get("code") == "SEMANTIC_CONTRACT_BLOCKED":
        written = call(tool, {
            **arguments, "relation_disposition": "reviewed_none",
            "relation_review_hash": review_hash, "relation_review_reason": NO_RELATION,
        })
    assert written.get("status") == "committed", written
    return written


def _note(call, title: str, *names: str) -> dict:
    """Commit one note that links these names, through the validate-then-commit door."""
    arguments = {"title": title, "note_type": "insight", "content": _body(title, *names)}
    draft = call("remember", {**arguments, "validate_only": True})
    return _committed(call, "remember", {
        **arguments, "draft_id": draft["draft_id"], "draft_hash": draft["draft_hash"],
        "draft_token": draft["draft_token"],
    }, draft["draft_hash"])


def _edit(call, path: str, old: str, new: str) -> dict:
    operation = {"kind": "replace_string", "old_string": old, "new_string": new}
    arguments = {"path": path, "why": "Add a source the review leaned on."}
    semantic = call("edit_note", {**arguments, "operation": {**operation, "validate_only": True}})["semantic"]
    operation["transition_token"] = semantic["transition_token"]
    edited = call("edit_note", {**arguments, "operation": operation})
    if edited.get("error", {}).get("code") == "SEMANTIC_CONTRACT_BLOCKED":
        edited = call("edit_note", {**arguments, "operation": {
            **operation, "relation_disposition": "reviewed_none",
            "relation_review_hash": semantic["transition_hash"], "relation_review_reason": NO_RELATION,
        }})
    assert edited.get("status") == "committed", edited
    return edited


def _listed(response: dict) -> list[str]:
    return [item["name"] for item in response.get("undeclared_referents", {}).get("names", [])]


def _promote(call, item: dict, summary: str = "A synthetic concept.") -> dict:
    """Run the `create-entity` route a block names, choosing the type and summary as an agent does."""
    route = item["route"]
    operation = {**route["args"]["operation"], "entity_type": "concept", "summary": summary}
    return call(route["tool"], {"operation": operation})


def test_a_write_lists_up_to_three_names_it_newly_links_that_name_no_page_and_no_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    made = call("connect_memory", {
        "operation": "create-entity", "entity_type": "concept", "name": "Tidal Ledger",
        "summary": "A synthetic concept named Tidal Ledger.",
    })
    assert made["status"] == "committed", made

    written = _note(call, "Harbour review", "Kestrel", "Tidal Ledger", "Salt Ledger", "Quiet Ferry", "Amber Gate")

    assert _listed(written) == ["Kestrel", "Salt Ledger", "Quiet Ferry"], (
        "an entity's name is not listed, and four unpaged names are cut to three"
    )
    for item in written["undeclared_referents"]["names"]:
        assert item["route"] == {
            "tool": "change_connections",
            "args": {"operation": {"kind": "create-entity", "name": item["name"], "ref": written["path"]}},
        }
        assert item["review"].startswith("exomem://review/write-advisory/")
        assert re.fullmatch(r"[0-9a-f]{24}", item["fingerprint"])


def test_an_edit_lists_only_the_name_it_adds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    first = _note(call, "Harbour review", "Kestrel")
    assert _listed(first) == ["Kestrel"]

    edited = _edit(call, first["path"], "[[Kestrel]]", "[[Kestrel]] and [[Salt Ledger]]")

    assert _listed(edited) == ["Salt Ledger"], "Kestrel was linked before this write"


def test_a_name_that_entity_candidate_raises_in_the_same_response_is_not_listed_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served_with_graph(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    _note(call, "Harbour review", "Kestrel")

    second = _note(call, "Pier review", "Kestrel", "Salt Ledger")

    assert [item["name"] for item in second["entity_candidate"]["identities"]] == ["Kestrel"]
    assert _listed(second) == ["Salt Ledger"]


def test_a_dismissed_name_is_not_listed_again_and_raises_no_entity_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served_with_graph(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    first = _note(call, "Harbour review", "Kestrel", "Salt Ledger")
    kestrel = next(item for item in first["undeclared_referents"]["names"] if item["name"] == "Kestrel")
    dismissed = call("triage_memory", {
        "ref": kestrel["review"], "action": "dismiss", "expected_fingerprint": kestrel["fingerprint"],
        "why": "intentional: Kestrel is a one-off name in this review.",
    })
    assert dismissed.get("state") == "dismissed", dismissed

    second = _note(call, "Pier review", "Kestrel", "Salt Ledger")

    assert [item["name"] for item in second["entity_candidate"]["identities"]] == ["Salt Ledger"]
    assert "undeclared_referents" not in second, "Salt Ledger belongs to entity_candidate now; Kestrel is dismissed"
    third = _note(call, "Dock review", "Kestrel")
    assert "undeclared_referents" not in third and "entity_candidate" not in third


def _switch_structural_suggestions_off(call) -> None:
    envelope.set_disposition("structural_suggestions", "off")


def _switch_to_light_prominence(call) -> None:
    prominence.write_prominence("light")


def _quiet_the_family(call) -> None:
    quieted = call("triage_memory", {
        "ref": FAMILY_REF, "action": "quiet", "why": "too_frequent: names are linked on purpose here.",
    })
    assert quieted.get("success") is not False, quieted


@pytest.mark.parametrize(
    "silence",
    [_switch_structural_suggestions_off, _switch_to_light_prominence, _quiet_the_family],
    ids=["structural_suggestions-off", "light-prominence", "family-quiet"],
)
def test_the_block_is_absent_where_the_owner_has_asked_for_less(
    silence, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    assert _listed(_note(call, "Harbour review", "Kestrel")) == ["Kestrel"], "the block is live before the switch"

    silence(call)

    assert "undeclared_referents" not in _note(call, "Pier review", "Salt Ledger")


def test_a_name_that_two_pages_of_one_batch_link_is_listed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One `episode_memory` resume commits two notes; the batch carries one block for both.

    The notes share a link with a page that already exists, so each is proposed with its relation
    review. A proposal refuses a review the page does not yet need, and a page needs one only once
    another page that shares its link exists.
    """
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-seed")
    _note(call, "Earlier harbour note", "Kestrel")
    _as(monkeypatch, "session-a")
    key = "ep-" + "c0" * 16

    def episode(**arguments) -> dict:
        return call("episode_memory", {"episode": key, **arguments})

    def prepare(candidate: str, slug: str, names: tuple[str, ...]) -> str:
        """Prepare one note leaf with the relation review that its shared link requires."""
        review = {"relation_disposition": "reviewed_none", "relation_review_reason": NO_RELATION}
        leaf = {"leaf_key": "write", "effect_revision": 1, "kind": "create-note", "args": {
            "title": slug.replace("-", " ").title(), "slug": slug, "content": _body(slug, *names), **review,
        }}
        prepared = episode(action="prepare", candidate=candidate, proposal={
            "route": "focused_note", "alternatives": [], "evidence": "complete",
            "reason": "The agent chose this home.", "leaves": [leaf], "title": slug,
        })
        candidate_row = next(row for row in prepared["candidates"] if row["candidate_key"] == candidate)
        episode(action="disposition", candidate=candidate, disposition="routed", reason="Routed for the test.")
        return candidate_row["leaves"][0]["leaf_id"]

    episode(
        action="record", subject="Harbour review", summary="Reviewed two harbour notes.",
        worked_on=["Reviewed the harbour"], decided=["The Kestrel numbers hold", "The pier numbers hold"], open=[],
    )
    first = prepare("first", "first-episode-note", ("Kestrel", "Salt Ledger"))
    second = prepare("second", "second-episode-note", ("Kestrel", "Quiet Ferry"))
    state = episode(action="candidates")

    resumed = episode(
        action="resume", input_revision=state["input_revision"], journal_digest=state["journal_digest"],
        order=[first, second],
    )

    assert [row["path"].rsplit("/", 1)[-1] for row in resumed["executed"]] == [
        "first-episode-note.md", "second-episode-note.md",
    ]
    assert _listed(resumed) == ["Kestrel", "Salt Ledger", "Quiet Ferry"]
    kestrel = resumed["undeclared_referents"]["names"][0]
    assert kestrel["route"]["args"]["operation"]["ref"].endswith("first-episode-note.md"), "the first page to link it"


VOLATILE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{64}|\d{4}-\d{2}-\d{2}T[0-9:.]+Z"
)
SCOPE_ID, RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FB0"


def _withhold_from_external(vault: Path, glob: str) -> None:
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "withheld.yaml").write_text(
        f'governance_version: 1\nid: {SCOPE_ID}\nname: Withheld\npaths: ["{glob}"]\n', encoding="utf-8",
    )
    (governance / "rules" / "withheld-external.yaml").write_text(
        f'governance_version: 1\nid: {RULE_ID}\nscope_ids: ["{SCOPE_ID}"]\naudience: external\nceiling: 0\n',
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()


def _restricted_writer(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, inbox: str) -> RequestPrincipal:
    """Bind host write authority and a live origin to the existing restricted audience."""
    from exomem import auth_sessions, registry_history, server_auth, state_migration
    from exomem.vocabulary import registry_specs

    granted = grant_writer(vault, tmp_path, monkeypatch, writable_paths=[f"{inbox}/**", "log.md"],
                           capture_paths=[f"Knowledge Base/{inbox}"], protected=[SCOPE_ID])(EXTERNAL.audience_id)
    config = tmp_path / "host-boundary.json"
    data = json.loads(config.read_text(encoding="utf-8"))
    public = {"namespace": "Knowledge Base/_Schema/public", "history": "public", "overrides": {}}
    for spec in registry_specs().values():
        override = {}
        if spec.overlay(vault).exists():
            override["overlay"] = spec.overlay(vault).relative_to(vault).as_posix()
        if registry_history.history_dir(vault, spec.stem).exists():
            override["history"] = spec.stem
        if override:
            public["overrides"][spec.stem] = override
    data["vocabulary"] = {"public": public, "private": {},
                          "destinations": {path: None for path in data["capture_paths"]}, "selections": {}}
    config.write_text(json.dumps(data), encoding="utf-8")
    stop = state_migration.assert_offline_migration_authority(source="undeclared referent fixture")
    state_migration.arm_connector_boundary_offline(vault, authority=stop)
    with principal_module.library_scope():
        assert graph_sync.drain_active_rebuilds(timeout=60)
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
        assert graph_sync.drain_active_rebuilds(timeout=60)
    authority = auth_sessions.SessionAuthority.local(
        directory=tmp_path / "origin-sessions", signing_root="temporary-referent-signing-root",
        issuer=ISSUER, audience=f"{ISSUER}/mcp",
    )
    _, origin = asyncio.run(authority.issue(
        client_id=CLIENT, scopes=["exomem:read", "exomem:write"],
        identity=auth_sessions.SessionIdentity(github_user_id=42, github_login="referent-reader"),
    ))
    monkeypatch.setenv("EXOMEM_BASE_URL", ISSUER)
    monkeypatch.setattr(server_auth, "build_session_authority", lambda **kwargs: authority)
    return replace(EXTERNAL, client_binding=granted.client_binding,
                   origin_session=OriginSessionBinding(origin.session_id, origin.generation, origin.audience))


def _same_bytes_but_for_ids(value: object) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "served_context_id"}
        task = value.get("task_context")
        # The 32-hex task generation derives from source versions normalized below, which differ across fresh vaults.
        if isinstance(task, dict) and re.fullmatch(r"[0-9a-f]{32}", task.get("generation", "")):
            value["task_context"] = {**task, "generation": "<id>"}
    return VOLATILE.sub("<id>", json.dumps(value, sort_keys=True))


@pytest.mark.timeout(300)  # three served vaults in one comparison; the default 60 s covers one
def test_a_restricted_caller_gets_the_same_receipt_and_activation_whether_a_same_name_entity_is_withheld_or_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The twin of a withheld entity at another path, at the entity's own default path, and none."""
    seen: dict[str, dict] = {}
    # Compare governance at the same graph readiness, including the twin with no entity write.
    for variant, entity_type in (("other-path", "organization"), ("same-path", "concept"), ("absent", None)):
        vault, call = _served_with_graph(tmp_path / variant, monkeypatch)
        _as(monkeypatch, "owner-session")
        if entity_type is not None:
            made = call("connect_memory", {
                "operation": "create-entity", "entity_type": entity_type, "name": "Kestrel",
                "summary": "A synthetic entity the restricted caller may not see.",
            })
            assert made["status"] == "committed", made
        _withhold_from_external(vault, "Entities/**")
        with monkeypatch.context() as restricted:
            _as(restricted, "restricted-session")  # a later session: it would see a promotion notice that leaked
            capture_sweep.reset_state()  # the sweep's quiet interval is per process and vault, not per session
            writer = _restricted_writer(vault, tmp_path / variant, restricted, inbox="Notes/Inbox/restricted")
            restricted.setattr(principal_module, "resolve_mcp_principal", lambda: writer)
            if entity_type is not None:
                hidden = call("read_memory", {"path": made["path"]})
                assert hidden.get("success") is False, "the entity is withheld from the restricted caller"
            seen[variant] = {
                "write": _note(call, "Harbour review", "Kestrel", "Salt Ledger"),
                "notices": call("bootstrap", {}),
                "activation": call("recall_context", {"turn": "What did the harbour review conclude about Kestrel?"}),
            }

    identifiers = [row["activation"]["served_context_id"] for row in seen.values()]
    assert len(set(identifiers)) == 3, "composition identifiers are fresh for every response"
    absent = seen.pop("absent")
    assert _listed(absent["write"]) == ["Kestrel", "Salt Ledger"], "the absent twin lists the name"
    for variant, observed in seen.items():
        assert _same_bytes_but_for_ids(observed["write"]) == _same_bytes_but_for_ids(absent["write"]), variant
        assert _same_bytes_but_for_ids(observed["notices"]) == _same_bytes_but_for_ids(absent["notices"]), variant
        assert _same_bytes_but_for_ids(observed["activation"]) == _same_bytes_but_for_ids(absent["activation"]), variant


def _logged_operation_id(vault: Path, path: str) -> str:
    """Read the page's write identity through the maintained operation-log parser."""
    ids = [
        entry["provenance"]["operation_id"]
        for entry in vault_module.read_operation_log(vault)["entries"]
        if entry.get("provenance", {}).get("path") == path
    ]
    assert len(ids) == 1, ids
    return ids[0]


def test_an_entity_made_through_the_route_names_the_originating_write_path_and_operation_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    written = _note(call, "Harbour review", "Kestrel")

    created = _promote(call, written["undeclared_referents"]["names"][0])

    entry = _log_entry(vault, created["path"])
    note_operation = _logged_operation_id(vault, written["path"])
    assert f"Origin: {written['path']}. Operation: {note_operation}." in entry
    assert created["request_id"] not in entry, "the id is the note's write, not this creation's request"


def test_a_withheld_origin_and_a_missing_one_give_a_restricted_caller_the_same_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route's `ref` is the caller's to supply, so a probe can aim it at a page it may not read."""
    vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    withheld = _note(call, "Harbour review", "Kestrel")["path"]
    missing = "Knowledge Base/Notes/Insights/a-page-that-was-never-written.md"
    _withhold_from_external(vault, "Notes/Insights/**")
    answers: dict[str, str] = {}
    with monkeypatch.context() as restricted:
        writer = _restricted_writer(vault, tmp_path, restricted, inbox="Entities/Concepts")
        restricted.setattr(principal_module, "resolve_mcp_principal", lambda: writer)
        for name, ref in (("Alpha Ledger", withheld), ("Beta Ledger", missing)):
            created = call("change_connections", {"operation": {
                "kind": "create-entity", "name": name, "ref": ref, "entity_type": "concept",
                "registry_scope": PUBLIC_INSTANCE,
                "summary": "A synthetic concept used to compare origins.",
            }})
            assert created["status"] == "committed", created
            assert "Origin: unknown." in _log_entry(vault, created["path"])
            answers[name] = _same_bytes_but_for_ids(created).replace(name, "<name>").replace(
                name.lower().replace(" ", "-"), "<slug>",
            )

    assert answers["Alpha Ledger"] == answers["Beta Ledger"]
    assert withheld not in answers["Alpha Ledger"]
