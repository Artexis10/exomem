"""`episode_memory` candidates: typed operations over existing curation leaves.

Contract: openspec/changes/close-memory-loop tasks 3.3, 3.5, 3.6 and 3.7, with
the live-enablement gate of task 5.5. A candidate names a typed destination for
an existing writer (a closed curation step its route owns), never a free-form
effect. `resume` is the episode's executor and refuses with
`episode_workflow_disabled` unless the service sets `EXOMEM_EPISODE_WORKFLOW`,
a feature switch rather than an authority boundary.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands, curation, episode_store, episode_workflow, server
from exomem import episode_model as model
from exomem import schema as schema_module
from exomem.__main__ import main as cli_main
from exomem.episode_store import EpisodeStore
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

KEY = "ep-" + "5e" * 16
ENTITY = "Knowledge Base/Entities/Organizations/Marsh Dyeworks.md"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _record(vault: Path, **overrides: object) -> dict:
    args: dict[str, object] = {
        "action": "record",
        "episode": KEY,
        "subject": "Kestrel Loom dye trial",
        "summary": "Settled the indigo vat plan; the kiln booking stays open.",
        "worked_on": ["Compared two dye suppliers for the loom trial"],
        "decided": ["Source indigo from Marsh Dyeworks"],
        "open": ["Maybe book the kiln next month"],
    }
    args.update(overrides)
    return commands.op_episode_memory(vault, schema_module.load_source_schema(vault), **args)


def _workflow(vault: Path, **kwargs: object) -> dict:
    return commands.op_episode_memory(
        vault, schema_module.load_source_schema(vault), episode=KEY, **kwargs
    )


def _note_leaf(slug: str, sentence: str, *, key: str = "write", revision: int = 1, **extra) -> dict:
    return {
        "leaf_key": key,
        "effect_revision": revision,
        "kind": "create-note",
        "args": {
            "title": slug.replace("-", " ").title(),
            "slug": slug,
            "content": f"## Observations\n\n- [finding] {sentence} ^{slug}\n",
            "relation_disposition": "reviewed_none",
            "relation_review_reason": "No supported relation in this synthetic episode.",
            **extra,
        },
    }


def _proposal(route: str, leaves: list[dict], *, title: str | None = None, target=None) -> dict:
    proposal: dict[str, object] = {
        "route": route,
        "alternatives": [],
        "evidence": "complete",
        "reason": "The active agent chose this home for a distinct future question.",
        "leaves": leaves,
    }
    if title is not None:
        proposal["title"] = title
    if target is not None:
        proposal["target"] = target
    return proposal


def _canonical_files(vault: Path) -> dict[str, bytes]:
    """Canonical Markdown only: operational stores and indexes are not knowledge."""
    kb = vault / "Knowledge Base"
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in kb.rglob("*.md")
        if "_Governance" not in path.relative_to(kb).parts
        and not path.relative_to(kb).parts[0].startswith(".")
    }


def _journal(vault: Path) -> bytes:
    store = EpisodeStore(vault, owner_audience_id=owner_principal(surface="mcp").audience_id)
    return store.path(model.episode_id(KEY)).read_bytes()


@pytest.fixture
def owner():
    with request_scope(owner_principal(surface="mcp")):
        yield


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")


@pytest.fixture
def disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(episode_workflow.ENABLE_ENV, raising=False)


def _reviewed(vault: Path) -> str:
    """The journal digest a caller holds after re-reading its candidates."""
    return _workflow(vault, action="candidates")["journal_digest"]


def _candidate(result: dict, key: str) -> dict:
    return next(item for item in result["candidates"] if item["candidate_key"] == key)


def _leaf_ids(inspected: dict, key: str) -> list[str]:
    return [leaf["leaf_id"] for leaf in _candidate(inspected, key)["leaves"]]


# --------------------------------------------------------------------------- #
# 3.3 — the typed operations on the canonical surface
# --------------------------------------------------------------------------- #


def test_the_operations_are_actions_of_the_generated_episode_command() -> None:
    product = {command.name: command for command in commands.PRODUCT_COMMANDS}
    command = product["episode_memory"]

    assert command.surfaces >= {"mcp", "rest", "cli"}
    assert command.leaf is commands.op_episode_memory
    for action in ("inspect", "candidates"):
        assert commands.invocation_is_read_only(command, {"action": action}) is True
    for action in ("record", "prepare", "disposition", "resume"):
        assert commands.invocation_is_read_only(command, {"action": action}) is False
    params = {param.name for param in command.params}
    assert {"candidate", "proposal", "disposition", "reason", "input_revision"} <= params
    # Hosted exposes none of it.
    assert "episode_memory" in commands.HOSTED_SURFACE_EXCLUSIONS
    for profile in commands.PRODUCT_SURFACE_PROFILES.values():
        assert "episode_memory" not in profile.command_names


def test_execution_is_off_unless_the_service_enables_it(monkeypatch) -> None:
    monkeypatch.delenv(episode_workflow.ENABLE_ENV, raising=False)
    assert episode_workflow.enabled() is False
    # Only an explicit affirmative switches it on; anything else is off.
    for value in ("", "0", "false", "off", "no", "disabled", "garbage", "2", "enable"):
        monkeypatch.setenv(episode_workflow.ENABLE_ENV, value)
        assert episode_workflow.enabled() is False, value
    for value in ("1", "true", "TRUE", "Yes", "on", " On "):
        monkeypatch.setenv(episode_workflow.ENABLE_ENV, value)
        assert episode_workflow.enabled() is True, value


def test_the_advertised_leaf_kinds_exclude_destructive_steps() -> None:
    from typing import get_args

    schema = get_args(commands._EpisodeProposalArgument)[1].json_schema  # noqa: SLF001
    kinds = schema["anyOf"][0]["properties"]["leaves"]["items"]["properties"]["kind"]["enum"]
    # The Records leaf joined by ruling (close-memory-loop 3.5); it appends and
    # its route owns nothing else.
    assert set(kinds) == {
        "create-note",
        "create-entity",
        "accept-relation",
        "edit",
        "supersede",
        "append-record",
    }


def test_prepare_disposition_and_inspect_share_the_curation_leaf(
    vault: Path, owner, disabled
) -> None:
    _record(vault)
    prepared = _workflow(
        vault,
        action="prepare",
        candidate="indigo-thesis",
        proposal=_proposal(
            "focused_note",
            [_note_leaf("indigo-vat-thesis", "Indigo vats need a stable warm bath.")],
            title="Indigo vat thesis",
        ),
    )
    leaf = _candidate(prepared, "indigo-thesis")["leaves"][0]
    # The leaf is bound to a sealed single-step curation plan: the same leaf
    # `maintain_memory mode=curation` would execute.
    run = curation.CurationStore(vault).load_plan(leaf["run_id"])
    assert [step["kind"] for step in run["steps"]] == ["create-note"]
    assert leaf["operation_id"] == curation.operation_id(
        curation.CurationStore(vault).identities(leaf["run_id"])[0], 0, run["steps"][0]["step_id"]
    )
    assert leaf["outcome"] == "pending" and leaf["bound"] is True

    disposed = _workflow(
        vault,
        action="disposition",
        candidate="indigo-thesis",
        disposition="routed",
        reason="The named thesis is independently useful.",
    )
    inspected = _workflow(vault, action="candidates")

    assert _candidate(disposed, "indigo-thesis")["disposition"] == "routed"
    assert _candidate(inspected, "indigo-thesis") == _candidate(disposed, "indigo-thesis")
    assert inspected["execution"] == "disabled"
    # The projection carries identities and outcomes, never leaf payloads.
    assert "stable warm bath" not in json.dumps(inspected)
    assert not (vault / "Knowledge Base/Notes/Insights/indigo-vat-thesis.md").exists()


@pytest.mark.parametrize(
    "leaf",
    [
        {"leaf_key": "x", "effect_revision": 1, "kind": "shell", "args": {"command": "rm -rf /"}},
        {"leaf_key": "x", "effect_revision": 1, "kind": "create-note", "args": {"effects": []}},
        {
            "leaf_key": "x",
            "effect_revision": 1,
            "kind": "create-note",
            "args": {"title": "t", "content": "c", "python": "print(1)"},
        },
        {**_note_leaf("free-form", "A."), "execute": True},
    ],
)
def test_arbitrary_execution_payloads_are_refused(vault: Path, owner, leaf: dict) -> None:
    _record(vault)
    before = _journal(vault)
    with pytest.raises(ValueError, match="EPISODE_PROPOSAL_INVALID"):
        _workflow(
            vault,
            action="prepare",
            candidate="smuggled",
            proposal=_proposal("focused_note", [leaf], title="Smuggled"),
        )
    assert _journal(vault) == before


def test_a_proposal_with_unknown_fields_is_refused(vault: Path, owner) -> None:
    _record(vault)
    proposal = _proposal("focused_note", [_note_leaf("a-note", "A.")], title="A")
    proposal["effects"] = [{"write": "anywhere"}]
    with pytest.raises(ValueError, match="EPISODE_PROPOSAL_INVALID"):
        _workflow(vault, action="prepare", candidate="a", proposal=proposal)


def test_resume_is_refused_while_disabled_and_writes_nothing(vault: Path, owner, disabled) -> None:
    _record(vault)
    _workflow(
        vault,
        action="prepare",
        candidate="indigo-thesis",
        proposal=_proposal(
            "focused_note", [_note_leaf("indigo-vat-thesis", "Warm bath.")], title="Indigo"
        ),
    )
    _workflow(
        vault, action="disposition", candidate="indigo-thesis", disposition="routed", reason="r"
    )
    files, journal = _canonical_files(vault), _journal(vault)

    refused = _workflow(vault, action="resume", input_revision=1, journal_digest=_reviewed(vault))

    assert refused["status"] == "refused"
    assert refused["code"] == "episode_workflow_disabled"
    assert refused["executed"] == []
    assert _canonical_files(vault) == files
    assert _journal(vault) == journal


def test_workflow_needs_a_recorded_episode_of_this_audience(vault: Path, enabled) -> None:
    with request_scope(RequestPrincipal(audience_id="client-a", surface="mcp")):
        _record(vault)
    with request_scope(RequestPrincipal(audience_id="client-b", surface="mcp")):
        for action, extra in (
            ("candidates", {}),
            ("resume", {"input_revision": 1, "journal_digest": "0" * 64}),
            ("disposition", {"candidate": "c", "disposition": "no_capture", "reason": "r"}),
        ):
            with pytest.raises(ValueError, match="EPISODE_NOT_FOUND"):
                _workflow(vault, action=action, **extra)


def test_an_unresolved_principal_fails_before_anything_is_written(vault: Path, enabled) -> None:
    with request_scope(owner_principal(surface="mcp")):
        _record(vault)
    journal = _journal(vault)
    with request_scope(RequestPrincipal(audience_id=None, surface="mcp")):
        with pytest.raises(ValueError, match="EPISODE_OWNER_UNRESOLVED"):
            _workflow(vault, action="resume", input_revision=1, journal_digest="0" * 64)
    assert _journal(vault) == journal


def test_arguments_of_another_action_are_refused(vault: Path, owner) -> None:
    _record(vault)
    with pytest.raises(ValueError, match="EPISODE_INVALID"):
        _workflow(vault, action="candidates", candidate="smuggled")
    with pytest.raises(ValueError, match="EPISODE_INVALID"):
        _workflow(vault, action="disposition", candidate="c", disposition="routed")
    with pytest.raises(ValueError, match="EPISODE_INVALID"):
        _workflow(vault, action="resume")
    # Resume names the episode state its caller last reviewed.
    with pytest.raises(ValueError, match="EPISODE_INVALID"):
        _workflow(vault, action="resume", input_revision=1)
    with pytest.raises(ValueError, match="EPISODE_INVALID"):
        _workflow(vault, action="record", subject="s", summary="s", proposal={"route": "x"})
    with pytest.raises(ValueError, match="EPISODE_INVALID"):
        _workflow(vault, action="inspect", candidate="smuggled")


# --------------------------------------------------------------------------- #
# Doors
# --------------------------------------------------------------------------- #


def _rest_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in ("EXOMEM_UPLOAD_TOKEN", "EXOMEM_CF_ACCESS_TEAM_DOMAIN", "EXOMEM_CF_ACCESS_AUD"):
        monkeypatch.delenv(leaky, raising=False)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    return TestClient(server.build_server(require_auth=False).http_app())


def test_three_doors_reach_one_leaf_and_refuse_untyped_fields(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], disabled
) -> None:
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    with request_scope(owner_principal(surface="mcp")):
        _record(vault)
    client = _rest_client(monkeypatch)
    rest = client.post(
        "/api/episode_memory",
        json={"action": "resume", "episode": KEY, "input_revision": 1, "journal_digest": "0" * 64},
        headers={"Authorization": "Bearer sekret"},
    )
    assert rest.status_code == 200, rest.text
    assert rest.json()["data"]["code"] == "episode_workflow_disabled"
    for field in ("effects", "payload", "command"):
        response = client.post(
            "/api/episode_memory",
            json={"action": "candidates", "episode": KEY, field: ["anything"]},
            headers={"Authorization": "Bearer sekret"},
        )
        assert response.status_code >= 400, response.text
        assert "UNKNOWN_PARAM" in response.text

    mcp = server.build_server(require_auth=False)
    with request_scope(owner_principal(surface="mcp")):
        called = asyncio.run(
            mcp.call_tool(
                "episode_memory", {"action": "candidates", "episode": KEY}, run_middleware=False
            )
        )
        with pytest.raises(Exception):  # noqa: B017 - the MCP schema refuses the field
            asyncio.run(
                mcp.call_tool(
                    "episode_memory",
                    {"action": "candidates", "episode": KEY, "effects": ["x"]},
                    run_middleware=False,
                )
            )
    mcp_result = (
        called.structured_content
        if isinstance(called.structured_content, dict)
        else json.loads(called.content[0].text)
    )
    assert mcp_result["episode"] == KEY

    try:
        code = cli_main(["episode_memory", "--action", "candidates", "--episode", KEY, "--json"])
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    out = capsys.readouterr().out
    assert code == 0, out
    assert json.loads(out)["data"]["episode"] == KEY


def test_an_enabled_resume_reports_its_receipts_through_the_rest_door(
    vault: Path, monkeypatch: pytest.MonkeyPatch, enabled
) -> None:
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    with request_scope(owner_principal(surface="mcp")):
        _record(vault)
        _three_notes(vault)
        digest = _reviewed(vault)
    client = _rest_client(monkeypatch)
    response = client.post(
        "/api/episode_memory",
        json={
            "action": "resume",
            "episode": KEY,
            "input_revision": 1,
            "journal_digest": digest,
            "max_leaves": 1,
        },
        headers={"Authorization": "Bearer sekret"},
    )
    assert response.status_code == 200, response.text
    body = json.dumps(response.json())
    assert '"executed"' in body and '"deferred": 2' in body, body[:2000]


# --------------------------------------------------------------------------- #
# 3.5 — a synthetic slice through the existing writers
# --------------------------------------------------------------------------- #


def _seed_entity(vault: Path) -> str:
    """An existing entity the episode should hydrate rather than duplicate."""
    proposed = curation.propose(
        vault,
        {
            "version": 1,
            "title": "Seed an entity",
            "steps": [
                {
                    "step_id": "seed",
                    "kind": "create-entity",
                    "args": {
                        "entity_type": "organization",
                        "name": "Marsh Dyeworks",
                        "summary": "Dye supplier for the Kestrel Loom trial.",
                    },
                }
            ],
        },
    )
    curation.apply(
        vault,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Seed the synthetic fixture.",
    )
    return ENTITY


def _prepare_slice(vault: Path, recap_ref: str) -> None:
    # A focused claim that cites the recap Source as its provenance.
    _workflow(
        vault,
        action="prepare",
        candidate="indigo-thesis",
        proposal=_proposal(
            "focused_note",
            [
                {
                    "leaf_key": "write",
                    "effect_revision": 1,
                    "kind": "create-note",
                    "args": {
                        "title": "Indigo vat thesis",
                        "slug": "indigo-vat-thesis",
                        "sources": [recap_ref],
                        "content": (
                            "## Observations\n\n- [finding] The loom trial keeps indigo"
                            " vats in a stable warm bath. ^indigo-vat-thesis\n\n"
                            f"## Relations\n\nSee [[{ENTITY.removesuffix('.md')}]].\n"
                        ),
                    },
                }
            ],
            title="Indigo vat thesis",
        ),
    )
    # A new entity the episode introduced.
    _workflow(
        vault,
        action="prepare",
        candidate="kiln-cooperative",
        proposal=_proposal(
            "entity",
            [
                {
                    "leaf_key": "create",
                    "effect_revision": 1,
                    "kind": "create-entity",
                    "args": {
                        "entity_type": "organization",
                        "name": "Ember Kiln Cooperative",
                        "summary": "Shared kiln considered for the dye trial.",
                    },
                }
            ],
            title="Ember Kiln Cooperative",
        ),
    )
    # A Records event and a mentioned possibility have no integrated leaf.
    _workflow(
        vault,
        action="prepare",
        candidate="vat-temperature-log",
        proposal=_proposal("records", [], title="Vat temperature reading"),
    )
    _workflow(
        vault,
        action="prepare",
        candidate="kiln-booking",
        proposal=_proposal("planning", [], title="Book the kiln"),
    )
    for key, value, reason in (
        ("indigo-thesis", "routed", "A distinct future question about vat practice."),
        ("kiln-cooperative", "routed", "A stable organization the trial depends on."),
        ("vat-temperature-log", "deferred", "Records execution is not integrated yet."),
        ("kiln-booking", "no_capture", "Only a possibility was mentioned, not intent."),
    ):
        _workflow(vault, action="disposition", candidate=key, disposition=value, reason=reason)


def _link_supplier(vault: Path) -> dict:
    """Hydrate the existing supplier by an accepted relation, not a second page."""
    from exomem import deferred_index, epistemic_graph, find, index_sync, semantic_contract

    note = "Knowledge Base/Notes/Insights/indigo-vat-thesis.md"
    # Publish the writers' derived state the way the service does: the graph,
    # its drained work queue and the reference-identity snapshot that pages
    # carrying an `exomem_id` are resolved through.
    find.clear_cache()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    for _ in range(12):
        if not deferred_index.list_graph_paths(vault):
            break
        index_sync.drain_graph_work(vault, limit=64)
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
        semantic_contract.build_corpus_context(vault)
        review = commands.op_review_memory(vault, mode="relation-queue")
    item = next(
        item
        for group in review["groups"]
        for item in group["items"]
        if item["from"] == note and item["to"] == ENTITY
    )
    expected_hash = next(g["content_hash"] for g in review["groups"] if g["path"] == note)
    _workflow(
        vault,
        action="prepare",
        candidate="supplier-link",
        proposal=_proposal(
            "relation_only",
            [
                {
                    "leaf_key": "accept",
                    "effect_revision": 1,
                    "kind": "accept-relation",
                    "args": {
                        "ref": item["ref"],
                        "expected_hash": expected_hash,
                        "why": "The thesis names the supplier it depends on.",
                        "expected_fingerprint": item["fingerprint"],
                    },
                }
            ],
            target=ENTITY,
        ),
    )
    disposed = _workflow(
        vault,
        action="disposition",
        candidate="supplier-link",
        disposition="routed",
        reason="A truthful typed relation to the existing supplier.",
    )
    return _workflow(
        vault, action="resume", input_revision=1, journal_digest=disposed["journal_digest"]
    )


def test_synthetic_slice_runs_through_existing_writers_with_nothing_unexpressed(
    vault: Path, owner, enabled
) -> None:
    _seed_entity(vault)
    recap = _record(vault)
    recap_ref = recap["source"]["ref"]

    # Hydration before duplication: a second entity of the same name is
    # refused when its leaf is prepared, before any effect exists.
    before_duplicate = _journal(vault)
    with pytest.raises(ValueError, match="ENTITY_EXISTS"):
        _workflow(
            vault,
            action="prepare",
            candidate="duplicate-supplier",
            proposal=_proposal(
                "entity",
                [
                    {
                        "leaf_key": "create",
                        "effect_revision": 1,
                        "kind": "create-entity",
                        "args": {
                            "entity_type": "organization",
                            "name": "Marsh Dyeworks",
                            "summary": "A duplicate of an existing supplier.",
                        },
                    }
                ],
                title="Marsh Dyeworks",
            ),
        )
    assert _journal(vault) == before_duplicate

    _prepare_slice(vault, recap_ref)
    before = _canonical_files(vault)

    resumed = _workflow(vault, action="resume", input_revision=1, journal_digest=_reviewed(vault))

    assert resumed["status"] == "ok", resumed["blocked"]
    assert len(resumed["executed"]) == 2 and resumed["blocked"] == []
    assert resumed["publication"] == "pending"
    after = _canonical_files(vault)
    note = "Knowledge Base/Notes/Insights/indigo-vat-thesis.md"
    kiln = "Knowledge Base/Entities/Organizations/Ember Kiln Cooperative.md"
    # Exactly the expressed effects: one created note and one created entity.
    # No Planning or Records page appears and nothing else changes.
    assert set(after) - set(before) == {note, kiln}
    # Besides them, only the writers' own system-managed bookkeeping moves:
    # the navigation index and log, and the cited recap's `ingested_into`
    # back-reference. The recap's body stays byte-identical.
    recap_path = recap["source"]["path"]
    changed = {path for path in before if before[path] != after.get(path)}
    assert changed == {recap_path, "Knowledge Base/index.md", "Knowledge Base/log.md"}
    recap_before = before[recap_path].decode().partition("\n---\n")
    recap_after = after[recap_path].decode().partition("\n---\n")
    assert recap_after[2] == recap_before[2]
    assert recap_before[0].replace("ingested_into: []", "") == recap_after[0].replace(
        f'ingested_into: ["[[{note.removesuffix(".md")}]]"]', ""
    )

    inspected = _workflow(vault, action="candidates")
    assert _candidate(inspected, "vat-temperature-log")["disposition"] == "deferred"
    assert _candidate(inspected, "kiln-booking")["disposition"] == "no_capture"
    assert inspected["complete"] is False
    assert {c["candidate_key"] for c in inspected["candidates"] if c["pending"]} == {
        "vat-temperature-log"
    }

    # The final pass: an active-agent postcommit attestation that reverifies
    # every committed proof, while the deferred candidate stays pending.
    attested = _workflow(
        vault,
        action="resume",
        input_revision=1,
        postcommit=True,
        journal_digest=resumed["journal_digest"],
    )
    assert attested["executed"] == []
    assert attested["reviewed_through_input_revision"] == 1
    assert attested["complete"] is False

    # The second pass hydrates the existing supplier through the relation
    # writer: the note gains a typed edge, and no second supplier page exists.
    note_before = (vault / note).read_bytes()
    linked = _link_supplier(vault)
    assert [item["path"] for item in linked["executed"]] == [note]
    relinked = _canonical_files(vault)
    assert {path for path in after if after[path] != relinked.get(path)} <= {
        note,
        "Knowledge Base/log.md",
        "Knowledge Base/index.md",
    }
    assert set(relinked) == set(after)
    assert "## Relations" in (vault / note).read_text(encoding="utf-8")
    assert (vault / note).read_bytes() != note_before
    assert len(list((vault / ENTITY).parent.glob("Marsh Dyeworks*.md"))) == 1

    # Canonical readback through the ordinary read path.
    read = commands.op_read_memory(vault, path=note)
    assert "stable warm bath" in json.dumps(read, default=str)

    # Source/claim: the recap stays a raw episode Source; the claim cites it.
    recap_page = (vault / recap["source"]["path"]).read_text(encoding="utf-8")
    assert "type: source" in recap_page and "source_type: episode" in recap_page
    claim = (vault / note).read_text(encoding="utf-8")
    assert "type: insight" in claim
    assert recap["source"]["path"].removesuffix(".md") in claim


# --------------------------------------------------------------------------- #
# 3.6 — derived state reaches a fresh session, honestly
# --------------------------------------------------------------------------- #


def _fresh_session_index(vault: Path) -> None:
    from exomem import find, lexstore, working_set_index, working_set_runtime

    find.clear_cache()
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()


def test_a_fresh_session_activates_bounded_provenance_bearing_context(vault: Path, enabled) -> None:
    with request_scope(owner_principal(surface="mcp")):
        _seed_entity(vault)
        recap = _record(vault)
        _prepare_slice(vault, recap["source"]["ref"])
        _workflow(vault, action="resume", input_revision=1, journal_digest=_reviewed(vault))
    _fresh_session_index(vault)

    with request_scope(owner_principal(surface="mcp")):
        packet = commands.op_activate_context(
            vault, turn="What did we settle about Marsh Dyeworks for the indigo vats?"
        )

    encoded = json.dumps(packet, default=str)
    assert len(encoded.encode()) < 64 * 1024
    assert packet["abstained"] is False
    assert "Marsh Dyeworks" in encoded
    # Every offered entry names where it came from.
    recent = packet["recent_context"]
    entries = recent["entries"] if isinstance(recent, dict) else recent
    assert entries and all(entry.get("ref") or entry.get("path") for entry in entries)
    episode_entries = [entry for entry in entries if entry.get("why") == "episode"]
    assert episode_entries and episode_entries[0].get("episode") == KEY


def test_a_shared_name_with_two_owners_is_not_resolved_to_either(vault: Path, enabled) -> None:
    with request_scope(owner_principal(surface="mcp")):
        _seed_entity(vault)
        _record(vault)
        _workflow(
            vault,
            action="prepare",
            candidate="harbor-site",
            proposal=_proposal(
                "entity",
                [
                    {
                        "leaf_key": "create",
                        "effect_revision": 1,
                        "kind": "create-entity",
                        "args": {
                            "entity_type": "person",
                            "name": "Marsh Dyeworks Keeper",
                            "summary": "The yard keeper also called Marsh Dyeworks.",
                        },
                    }
                ],
                title="Marsh Dyeworks Keeper",
            ),
        )
        _workflow(
            vault, action="disposition", candidate="harbor-site", disposition="routed", reason="r"
        )
        resumed = _workflow(
            vault, action="resume", input_revision=1, journal_digest=_reviewed(vault)
        )
        # The keeper shares the supplier's name as an alias: two owners.
        keeper = vault / resumed["executed"][0]["path"]
        text = keeper.read_text(encoding="utf-8")
        keeper.write_text(
            text.replace("type: entity\n", "type: entity\naliases: [Marsh Dyeworks]\n", 1),
            encoding="utf-8",
        )
    _fresh_session_index(vault)

    with request_scope(owner_principal(surface="mcp")):
        packet = commands.op_activate_context(vault, turn="Tell me about Marsh Dyeworks.")

    # The server reports the ambiguity; it never picks an owner for the agent.
    assert packet.get("turn_status") == "ambiguous" or (
        packet["abstained"] and packet["abstention"]["reason"] == "ambiguous"
    )


def test_an_unready_index_says_warming_instead_of_claiming_currentness(
    vault: Path, monkeypatch: pytest.MonkeyPatch, enabled
) -> None:
    from exomem import freshness, readiness, working_set_runtime

    with request_scope(owner_principal(surface="mcp")):
        _seed_entity(vault)
        recap = _record(vault)
        _prepare_slice(vault, recap["source"]["ref"])
        resumed = _workflow(
            vault, action="resume", input_revision=1, journal_digest=_reviewed(vault)
        )
    assert resumed["publication"] == "pending"

    working_set_runtime.reset_caches_for_tests()
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    monkeypatch.setattr(freshness, "recall_seed_pending", lambda *_a, **_k: True)
    monkeypatch.setattr(working_set_runtime, "_schedule_build", lambda root, **_kw: None)
    with request_scope(owner_principal(surface="mcp")):
        packet = commands.op_activate_context(vault, turn="What about Marsh Dyeworks?")

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "index_warming"}


# --------------------------------------------------------------------------- #
# 3.7 — interrupt after a committed leaf, reorder, resume
# --------------------------------------------------------------------------- #


def _three_notes(vault: Path) -> None:
    for key, slug in (
        ("alpha", "alpha-dye-note"),
        ("beta", "beta-dye-note"),
        ("gamma", "gamma-dye-note"),
    ):
        _workflow(
            vault,
            action="prepare",
            candidate=key,
            proposal=_proposal(
                "focused_note", [_note_leaf(slug, f"The {key} observation holds.")], title=slug
            ),
        )
        _workflow(vault, action="disposition", candidate=key, disposition="routed", reason="r")


def test_interrupt_reorder_and_resume_reuses_the_original_receipt(
    vault: Path, owner, enabled, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_working_set_carry import seed_ordinary_notes

    # Ordinary-note retrieval needs a corpus in which rarity is meaningful.
    # Reuse its established prose control, not a fixture-specific product rule.
    seed_ordinary_notes(vault)
    _record(vault)
    _three_notes(vault)
    inspected = _workflow(vault, action="candidates")
    alpha, beta, gamma = (_leaf_ids(inspected, key)[0] for key in ("alpha", "beta", "gamma"))

    # Interrupt after alpha's writer committed, before the ledger learned it.
    real = EpisodeStore.transition

    def crash_on_reconcile(self, identity, **kwargs):
        if kwargs.get("action") == "reconcile_curation_leaf":
            raise KeyboardInterrupt("host lost the session")
        return real(self, identity, **kwargs)

    monkeypatch.setattr(EpisodeStore, "transition", crash_on_reconcile)
    with pytest.raises(KeyboardInterrupt):
        _workflow(
            vault,
            action="resume",
            input_revision=1,
            order=[alpha, beta, gamma],
            journal_digest=_reviewed(vault),
        )
    monkeypatch.setattr(EpisodeStore, "transition", real)

    interrupted = _workflow(vault, action="candidates")
    alpha_leaf = _candidate(interrupted, "alpha")["leaves"][0]
    assert alpha_leaf["outcome"] == "uncertain"
    alpha_path = vault / "Knowledge Base/Notes/Insights/alpha-dye-note.md"
    committed_bytes = alpha_path.read_bytes()
    store = curation.CurationStore(vault)
    original = store.reconstruct(alpha_leaf["run_id"])["receipts"]
    assert len(original) == 1

    # A fresh semantic revision of gamma must pass current validation: an
    # edit of alpha's page reviewed before alpha committed is stale now.
    journal = _journal(vault)
    with pytest.raises(ValueError, match="CURATION_BINDING_STALE"):
        _workflow(
            vault,
            action="prepare",
            candidate="gamma",
            proposal=_proposal(
                "existing_page",
                [
                    {
                        "leaf_key": "write",
                        "effect_revision": 2,
                        "kind": "edit",
                        "args": {
                            "path": "Knowledge Base/Notes/Insights/alpha-dye-note.md",
                            "why": "Fold gamma into alpha.",
                            "operation": {
                                "kind": "edit_section",
                                "heading": "Observations",
                                "new_string": "- [finding] Gamma folded in. ^gamma-fold",
                                "section_position": "append",
                                "expected_hash": "0" * 64,
                            },
                        },
                    }
                ],
                target="Knowledge Base/Notes/Insights/alpha-dye-note.md",
            ),
        )
    assert _journal(vault) == journal
    # A valid revision is bound afresh to a new sealed plan.
    revised = _workflow(
        vault,
        action="prepare",
        candidate="gamma",
        proposal=_proposal(
            "focused_note",
            [_note_leaf("gamma-dye-note", "The gamma observation, corrected.", revision=2)],
            title="gamma-dye-note",
        ),
    )
    gamma_leaf = _candidate(revised, "gamma")["leaves"][0]
    assert gamma_leaf["leaf_id"] == gamma and gamma_leaf["effect_revision"] == 2
    assert gamma_leaf["run_id"] != _candidate(inspected, "gamma")["leaves"][0]["run_id"]
    # Alpha's committed effect cannot be re-proposed under a new identity.
    with pytest.raises(ValueError, match="EPISODE_DUPLICATE_EFFECT"):
        _workflow(
            vault,
            action="prepare",
            candidate="alpha-again",
            proposal=_proposal(
                "focused_note",
                [_note_leaf("alpha-dye-note", "The alpha observation holds.")],
                title="alpha again",
            ),
        )

    # The revision withdrew gamma's disposition; the agent routes it afresh.
    disposed = _workflow(
        vault, action="disposition", candidate="gamma", disposition="routed", reason="Corrected."
    )

    # Resume with the remainder reordered.
    resumed = _workflow(
        vault,
        action="resume",
        input_revision=1,
        order=[gamma, beta],
        journal_digest=disposed["journal_digest"],
    )

    assert [item["leaf_id"] for item in resumed["reconciled"]] == [alpha]
    assert [item["leaf_id"] for item in resumed["executed"]] == [gamma, beta]
    assert alpha_path.read_bytes() == committed_bytes
    after = store.reconstruct(alpha_leaf["run_id"])["receipts"]
    assert after == original
    final = _workflow(vault, action="candidates")
    proof = _candidate(final, "alpha")["leaves"][0]
    assert proof["outcome"] == "committed"
    assert proof["receipt_digest"] == curation._digest(original[0])
    assert "corrected" in (vault / "Knowledge Base/Notes/Insights/gamma-dye-note.md").read_text(
        encoding="utf-8"
    )

    # Nothing is left to run, and a repeat resume applies nothing again.
    again = _workflow(
        vault, action="resume", input_revision=1, journal_digest=resumed["journal_digest"]
    )
    assert again["executed"] == [] and again["reconciled"] == []
    assert alpha_path.read_bytes() == committed_bytes

    # Recovery is useful only if another session can consume the committed
    # result, not merely if the ledger retains its original receipt.
    read = commands.op_read_memory(vault, path=alpha_path.relative_to(vault).as_posix())
    assert "The alpha observation holds." in read["body"]
    _fresh_session_index(vault)
    with request_scope(owner_principal(surface="mcp")):
        packet = commands.op_activate_context(
            vault, turn="What did the Alpha Dye Note settle?"
        )
    assert packet["generation"]["continuity"] == "absent"
    assert packet["abstained"] is False, json.dumps(packet, default=str)
    units = [unit for unit in packet["units"] if unit["provenance"]["path"] == read["path"]]
    assert units and any("The alpha observation holds." in unit["text"] for unit in units)
    assert all(unit["ref"] for unit in units)
    assert len(json.dumps(packet, default=str).encode()) < 64 * 1024


def test_an_uncertain_leaf_is_never_retried_under_a_fresh_identity(
    vault: Path, owner, enabled, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(vault)
    _three_notes(vault)
    inspected = _workflow(vault, action="candidates")
    alpha = _leaf_ids(inspected, "alpha")[0]

    def writer_fails(*_args, **_kwargs):
        raise curation.CurationError("CURATION_STEP_FAILED", "writer refused")

    monkeypatch.setattr(curation, "apply", writer_fails)
    resumed = _workflow(
        vault, action="resume", input_revision=1, order=[alpha], journal_digest=_reviewed(vault)
    )
    monkeypatch.undo()
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")

    assert resumed["executed"] == []
    assert [item["leaf_id"] for item in resumed["blocked"]] == [alpha]
    # Failure is not proof of non-commit: the leaf stays uncertain, frozen.
    with pytest.raises(ValueError, match="EPISODE_ATTEMPT_UNCERTAIN"):
        _workflow(
            vault,
            action="prepare",
            candidate="alpha",
            proposal=_proposal(
                "focused_note", [_note_leaf("alpha-dye-note", "Changed.", revision=2)], title="a"
            ),
        )
    again = _workflow(
        vault,
        action="resume",
        input_revision=1,
        order=[alpha],
        journal_digest=resumed["journal_digest"],
    )
    assert again["executed"] == []
    assert [item["leaf_id"] for item in again["blocked"]] == [alpha]


def test_a_new_input_revision_requires_fresh_dispositions_before_execution(
    vault: Path, owner, enabled
) -> None:
    _record(vault)
    _three_notes(vault)
    _record(vault, open=["Maybe book the kiln next month", "Check the mordant"])

    digest = _reviewed(vault)
    with pytest.raises(ValueError, match="EPISODE_INPUT_REVISION_STALE"):
        _workflow(vault, action="resume", input_revision=1, journal_digest=digest)
    with pytest.raises(ValueError, match="EPISODE_COVERAGE_INCOMPLETE"):
        _workflow(vault, action="resume", input_revision=2, journal_digest=digest)
    assert not (vault / "Knowledge Base/Notes/Insights/alpha-dye-note.md").exists()


# --------------------------------------------------------------------------- #
# Security review of the typed operations (task 5.5)
# --------------------------------------------------------------------------- #

INSIGHTS = "Knowledge Base/Notes/Insights"


def _note_proposal(slug: str, sentence: str, *, revision: int = 1) -> dict:
    return _proposal("focused_note", [_note_leaf(slug, sentence, revision=revision)], title=slug)


def _routed(vault: Path, key: str, slug: str, *, revision: int = 1) -> dict:
    prepared = _workflow(
        vault,
        action="prepare",
        candidate=key,
        proposal=_note_proposal(slug, f"The {key} observation holds.", revision=revision),
    )
    disposed = _workflow(
        vault, action="disposition", candidate=key, disposition="routed", reason="Reviewed home."
    )
    return {"prepared": prepared, "disposed": disposed}


def _runs(vault: Path) -> set[str]:
    root = curation.CurationStore(vault).root
    return {path.name for path in root.iterdir()} if root.is_dir() else set()


def test_resume_never_runs_an_effect_nobody_dispositioned(vault: Path, owner, enabled) -> None:
    _record(vault)
    disposed = _routed(vault, "indigo", "indigo-note-a")["disposed"]
    # The same candidate is re-prepared as a different note after its review.
    revised = _workflow(
        vault,
        action="prepare",
        candidate="indigo",
        proposal=_note_proposal("indigo-note-b", "Note B replaces it.", revision=2),
    )
    # A revision withdraws the disposition that reviewed the earlier effect.
    assert _candidate(revised, "indigo")["disposition"] is None
    files, journal = _canonical_files(vault), _journal(vault)

    # The state the caller reviewed is no longer the episode's state.
    with pytest.raises(ValueError, match="EPISODE_REVISION_CONFLICT"):
        _workflow(
            vault, action="resume", input_revision=1, journal_digest=disposed["journal_digest"]
        )
    # Against the current state, nothing has routed note B.
    with pytest.raises(ValueError, match="EPISODE_COVERAGE_INCOMPLETE"):
        _workflow(
            vault, action="resume", input_revision=1, journal_digest=revised["journal_digest"]
        )

    assert _canonical_files(vault) == files
    assert _journal(vault) == journal
    assert not (vault / INSIGHTS / "indigo-note-a.md").exists()
    assert not (vault / INSIGHTS / "indigo-note-b.md").exists()


def test_a_stale_binding_is_reported_without_an_attempt(vault: Path, owner, enabled) -> None:
    _record(vault)
    disposed = _routed(vault, "alpha", "alpha-dye-note")["disposed"]
    # The destination appears out of band before resume.
    destination = vault / INSIGHTS / "alpha-dye-note.md"
    destination.write_text("---\ntype: insight\n---\n\nWritten elsewhere.\n", encoding="utf-8")

    resumed = _workflow(
        vault, action="resume", input_revision=1, journal_digest=disposed["journal_digest"]
    )

    leaf = _candidate(resumed, "alpha")["leaves"][0]
    assert resumed["status"] == "stale"
    assert resumed["executed"] == [] and resumed["blocked"] == []
    assert resumed["stale"] == [{"leaf_id": leaf["leaf_id"], "code": "CURATION_BINDING_STALE"}]
    # No attempt was recorded, so nothing freezes the candidate.
    assert (leaf["outcome"], leaf["attempts"]) == ("pending", 0)
    assert destination.read_text(encoding="utf-8").endswith("Written elsewhere.\n")

    # The episode moves on: a free home, a fresh disposition and a resume.
    disposed = _routed(vault, "alpha", "alpha-dye-note-moved", revision=2)["disposed"]
    again = _workflow(
        vault, action="resume", input_revision=1, journal_digest=disposed["journal_digest"]
    )
    assert again["status"] == "ok", again
    assert [item["leaf_id"] for item in again["executed"]] == [leaf["leaf_id"]]
    assert (vault / INSIGHTS / "alpha-dye-note-moved.md").exists()


def test_a_stale_unattempted_binding_is_resealed_by_the_same_proposal(
    vault: Path, owner, enabled, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(vault)
    routed = _routed(vault, "alpha", "alpha-dye-note")
    first = _candidate(routed["prepared"], "alpha")["leaves"][0]
    # A registry changes out of band: the sealed plan is stale, its effect is not.
    real = curation.registry_identities
    monkeypatch.setattr(
        curation, "registry_identities", lambda root: {**real(root), "schemas": "0" * 64}
    )
    stale = _workflow(
        vault,
        action="resume",
        input_revision=1,
        journal_digest=routed["disposed"]["journal_digest"],
    )
    assert [item["code"] for item in stale["stale"]] == ["CURATION_REGISTRY_CHANGED"]

    resealed = _workflow(
        vault,
        action="prepare",
        candidate="alpha",
        proposal=_note_proposal("alpha-dye-note", "The alpha observation holds."),
    )
    leaf = _candidate(resealed, "alpha")["leaves"][0]
    assert leaf["bound"] and leaf["run_id"] != first["run_id"]
    # A registry-only change keeps the effect and its preimage, so the
    # disposition that reviewed them still stands.
    assert _candidate(resealed, "alpha")["disposition"] == "routed"
    again = _workflow(
        vault, action="resume", input_revision=1, journal_digest=resealed["journal_digest"]
    )
    assert [item["leaf_id"] for item in again["executed"]] == [first["leaf_id"]]


def test_a_leaf_already_applied_elsewhere_is_reported_replayed(vault: Path, owner, enabled) -> None:
    _record(vault)
    routed = _routed(vault, "alpha", "alpha-dye-note")
    leaf = _candidate(routed["prepared"], "alpha")["leaves"][0]
    # The sealed plan is an ordinary curation run: the same caller's
    # `maintain_memory mode=curation apply` can execute it without the switch.
    store = curation.CurationStore(vault)
    plan_id, fingerprint = store.identities(leaf["run_id"])
    curation.apply(
        vault,
        run_id=leaf["run_id"],
        plan_id=plan_id,
        expected_plan_fingerprint=fingerprint,
        why="Applied outside the episode.",
    )

    resumed = _workflow(
        vault,
        action="resume",
        input_revision=1,
        journal_digest=routed["disposed"]["journal_digest"],
    )

    assert resumed["status"] == "ok"
    assert resumed["executed"] == []
    assert [item["leaf_id"] for item in resumed["replayed"]] == [leaf["leaf_id"]]
    assert resumed["publication"] == "unchanged"
    assert _candidate(resumed, "alpha")["leaves"][0]["outcome"] == "committed"


def test_a_failed_bind_leaves_no_half_bound_candidate(
    vault: Path, owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(vault)
    before = _journal(vault)
    proposal = _proposal(
        "focused_note",
        [
            _note_leaf("pair-one-note", "The first half holds.", key="one"),
            _note_leaf("pair-two-note", "The second half holds.", key="two"),
        ],
        title="Pair",
    )
    real = EpisodeStore._accepted_evidence  # noqa: SLF001
    binds: list[str] = []

    def second_bind_fails(self, state, action, args):
        if action == "bind_curation_leaf":
            binds.append(args["leaf"])
            if len(binds) == 2:
                raise model.EpisodeError("EPISODE_BINDING_INVALID", "injected bind failure")
        return real(self, state, action, args)

    monkeypatch.setattr(EpisodeStore, "_accepted_evidence", second_bind_fails)
    with pytest.raises(ValueError, match="EPISODE_BINDING_INVALID"):
        _workflow(vault, action="prepare", candidate="pair", proposal=proposal)
    monkeypatch.setattr(EpisodeStore, "_accepted_evidence", real)

    # Nothing of the candidate was recorded: not declared, not half bound.
    assert _journal(vault) == before
    retried = _workflow(vault, action="prepare", candidate="pair", proposal=proposal)
    assert [leaf["bound"] for leaf in _candidate(retried, "pair")["leaves"]] == [True, True]


def test_prepare_without_journal_room_seals_no_plan(
    vault: Path, owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(vault)
    transitions = _workflow(vault, action="candidates")["revision"] - 1
    before, runs = _journal(vault), _runs(vault)
    # Room to declare and revise the candidate, none to bind its leaf.
    monkeypatch.setattr(episode_store, "MAX_TRANSITIONS", transitions + 2)

    with pytest.raises(ValueError, match="EPISODE_TOO_LARGE"):
        _workflow(
            vault,
            action="prepare",
            candidate="alpha",
            proposal=_note_proposal("alpha-dye-note", "The alpha observation holds."),
        )

    assert _journal(vault) == before
    assert _runs(vault) == runs


def test_an_attempt_starts_only_with_room_for_its_reconcile(
    vault: Path, owner, enabled, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(vault)
    disposed = _routed(vault, "alpha", "alpha-dye-note")["disposed"]
    transitions = disposed["revision"] - 1
    # Room for the precommit attestation and an attempt mark, none for its reconcile.
    monkeypatch.setattr(episode_store, "MAX_TRANSITIONS", transitions + 2)

    resumed = _workflow(
        vault, action="resume", input_revision=1, journal_digest=disposed["journal_digest"]
    )

    leaf = _candidate(resumed, "alpha")["leaves"][0]
    assert resumed["executed"] == []
    assert resumed["blocked"] == [{"leaf_id": leaf["leaf_id"], "code": "EPISODE_TOO_LARGE"}]
    assert (leaf["outcome"], leaf["attempts"]) == ("pending", 0)
    assert not (vault / INSIGHTS / "alpha-dye-note.md").exists()

    monkeypatch.setattr(episode_store, "MAX_TRANSITIONS", transitions + 3)
    again = _workflow(
        vault, action="resume", input_revision=1, journal_digest=resumed["journal_digest"]
    )
    assert [item["leaf_id"] for item in again["executed"]] == [leaf["leaf_id"]]
    assert _candidate(again, "alpha")["leaves"][0]["outcome"] == "committed"


def _seed_note(vault: Path, slug: str) -> str:
    """An existing insight page, written through the curation executor."""
    args = _note_leaf(slug, "The seeded observation holds.")["args"]
    proposed = curation.propose(
        vault,
        {
            "version": 1,
            "title": "Seed a note",
            "steps": [{"step_id": "seed", "kind": "create-note", "args": args}],
        },
    )
    curation.apply(
        vault,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Seed the synthetic fixture.",
    )
    return f"{INSIGHTS}/{slug}.md"


def test_a_reseal_onto_changed_content_withdraws_the_disposition(
    vault: Path, owner, enabled
) -> None:
    _record(vault)
    old = _seed_note(vault, "seed-dye-note")
    proposal = _proposal(
        "existing_page",
        [
            {
                "leaf_key": "replace",
                "effect_revision": 1,
                "kind": "supersede",
                "args": {
                    "old_path": old,
                    "title": "Seed dye note revised",
                    "content": (
                        "## Observations\n\n- [finding] The revised seed observation holds."
                        " ^seed-revised\n"
                    ),
                },
            }
        ],
        target=old,
    )
    prepared = _workflow(vault, action="prepare", candidate="seed", proposal=proposal)
    disposed = _workflow(
        vault, action="disposition", candidate="seed", disposition="routed", reason="Reviewed."
    )
    # The page changes out of band after its supersession was reviewed.
    page = vault / old
    page.write_text(
        page.read_text(encoding="utf-8") + "\nAn out-of-band addition.\n", encoding="utf-8"
    )
    stale = _workflow(
        vault, action="resume", input_revision=1, journal_digest=disposed["journal_digest"]
    )
    assert [item["code"] for item in stale["stale"]] == ["CURATION_BINDING_STALE"]

    resealed = _workflow(vault, action="prepare", candidate="seed", proposal=proposal)

    leaf = _candidate(resealed, "seed")["leaves"][0]
    assert leaf["bound"]
    assert leaf["run_id"] != _candidate(prepared, "seed")["leaves"][0]["run_id"]
    # The reviewed preimage is gone, so the owner reviews the new one.
    assert _candidate(resealed, "seed")["disposition"] is None
    edited = page.read_bytes()
    with pytest.raises(ValueError, match="EPISODE_COVERAGE_INCOMPLETE"):
        _workflow(
            vault, action="resume", input_revision=1, journal_digest=resealed["journal_digest"]
        )
    assert page.read_bytes() == edited


def test_a_leaf_committed_elsewhere_then_edited_is_reported_diverged(
    vault: Path, owner, enabled
) -> None:
    _record(vault)
    routed = _routed(vault, "alpha", "alpha-dye-note")
    leaf = _candidate(routed["prepared"], "alpha")["leaves"][0]
    store = curation.CurationStore(vault)
    plan_id, fingerprint = store.identities(leaf["run_id"])
    curation.apply(
        vault,
        run_id=leaf["run_id"],
        plan_id=plan_id,
        expected_plan_fingerprint=fingerprint,
        why="Applied outside the episode.",
    )
    note = vault / INSIGHTS / "alpha-dye-note.md"
    note.write_text(
        note.read_text(encoding="utf-8") + "\nEdited after it was written.\n", encoding="utf-8"
    )

    resumed = _workflow(
        vault,
        action="resume",
        input_revision=1,
        journal_digest=routed["disposed"]["journal_digest"],
    )

    assert resumed["status"] == "diverged"
    assert resumed["diverged"] == [
        {"leaf_id": leaf["leaf_id"], "code": "EPISODE_OUTCOME_UNCERTAIN"}
    ]
    assert resumed["executed"] == resumed["replayed"] == resumed["blocked"] == []
    current = _candidate(resumed, "alpha")["leaves"][0]
    assert (current["outcome"], current["attempts"]) == ("pending", 0)

    # No attempt froze the candidate: the owner settles it and the episode goes on.
    disposed = _workflow(
        vault,
        action="disposition",
        candidate="alpha",
        disposition="rejected",
        reason="The page moved on after it was written.",
    )
    again = _workflow(
        vault, action="resume", input_revision=1, journal_digest=disposed["journal_digest"]
    )
    assert again["status"] == "ok"


def test_prepare_without_journal_bytes_seals_no_plan(
    vault: Path, owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(vault)
    before, runs = _journal(vault), _runs(vault)
    # Room for a few hundred bytes: not for a proposal and its sealed plan.
    monkeypatch.setattr(episode_store, "MAX_JOURNAL_BYTES", len(before) + 256)

    with pytest.raises(ValueError, match="EPISODE_TOO_LARGE"):
        _workflow(
            vault,
            action="prepare",
            candidate="alpha",
            proposal=_note_proposal("alpha-dye-note", "The alpha observation holds."),
        )

    assert _journal(vault) == before
    assert _runs(vault) == runs
