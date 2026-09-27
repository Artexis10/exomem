"""`episode_workflow`: typed candidate operations over existing curation leaves.

Contract: openspec/changes/close-memory-loop tasks 3.3, 3.5, 3.6 and 3.7, with
the live-enablement gate of task 5.5. A candidate names a typed destination for
an existing writer (a closed curation step), never a free-form effect. Only
`resume` can reach a writer, and it refuses with `episode_workflow_disabled`
unless the service sets `EXOMEM_EPISODE_WORKFLOW`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands, curation, episode_workflow, server
from exomem import episode_model as model
from exomem import schema as schema_module
from exomem.__main__ import main as cli_main
from exomem.episode_store import EpisodeStore
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from exomem.vault import content_hash

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
    return commands.op_episode_workflow(vault, episode=KEY, **kwargs)


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


def _candidate(result: dict, key: str) -> dict:
    return next(item for item in result["candidates"] if item["candidate_key"] == key)


def _leaf_ids(inspected: dict, key: str) -> list[str]:
    return [leaf["leaf_id"] for leaf in _candidate(inspected, key)["leaves"]]


# --------------------------------------------------------------------------- #
# 3.3 — the typed operations on the canonical surface
# --------------------------------------------------------------------------- #


def test_the_command_is_generated_onto_mcp_cli_and_rest() -> None:
    product = {command.name: command for command in commands.PRODUCT_COMMANDS}
    command = product["episode_workflow"]

    assert command.surfaces >= {"mcp", "rest", "cli"}
    assert command.leaf is commands.op_episode_workflow
    assert commands.invocation_is_read_only(command, {"action": "inspect"}) is True
    for action in ("prepare", "disposition", "resume"):
        assert commands.invocation_is_read_only(command, {"action": action}) is False
    assert "episode_workflow" in commands.HOSTED_SURFACE_EXCLUSIONS
    for profile in commands.PRODUCT_SURFACE_PROFILES.values():
        assert "episode_workflow" not in profile.command_names


def test_execution_is_off_unless_the_service_enables_it(monkeypatch) -> None:
    monkeypatch.delenv(episode_workflow.ENABLE_ENV, raising=False)
    assert episode_workflow.enabled() is False
    for value in ("", "0", "false", "off", "no"):
        monkeypatch.setenv(episode_workflow.ENABLE_ENV, value)
        assert episode_workflow.enabled() is False
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    assert episode_workflow.enabled() is True


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
    inspected = _workflow(vault, action="inspect")

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


def test_resume_is_refused_while_disabled_and_writes_nothing(
    vault: Path, owner, disabled
) -> None:
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

    refused = _workflow(vault, action="resume", input_revision=1)

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
            ("inspect", {}),
            ("resume", {"input_revision": 1}),
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
            _workflow(vault, action="resume", input_revision=1)
    assert _journal(vault) == journal


def test_arguments_of_another_action_are_refused(vault: Path, owner) -> None:
    _record(vault)
    with pytest.raises(ValueError, match="EPISODE_WORKFLOW_INVALID"):
        _workflow(vault, action="inspect", candidate="smuggled")
    with pytest.raises(ValueError, match="EPISODE_WORKFLOW_INVALID"):
        _workflow(vault, action="disposition", candidate="c", disposition="routed")
    with pytest.raises(ValueError, match="EPISODE_WORKFLOW_INVALID"):
        _workflow(vault, action="resume")


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
        "/api/episode_workflow",
        json={"action": "resume", "episode": KEY, "input_revision": 1},
        headers={"Authorization": "Bearer sekret"},
    )
    assert rest.status_code == 200, rest.text
    assert rest.json()["data"]["code"] == "episode_workflow_disabled"
    for field in ("effects", "payload", "command"):
        response = client.post(
            "/api/episode_workflow",
            json={"action": "inspect", "episode": KEY, field: ["anything"]},
            headers={"Authorization": "Bearer sekret"},
        )
        assert response.status_code >= 400, response.text
        assert "UNKNOWN_PARAM" in response.text

    mcp = server.build_server(require_auth=False)
    with request_scope(owner_principal(surface="mcp")):
        called = asyncio.run(
            mcp.call_tool(
                "episode_workflow", {"action": "inspect", "episode": KEY}, run_middleware=False
            )
        )
        with pytest.raises(Exception):  # noqa: B017 - the MCP schema refuses the field
            asyncio.run(
                mcp.call_tool(
                    "episode_workflow",
                    {"action": "inspect", "episode": KEY, "effects": ["x"]},
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
        code = cli_main(
            ["episode_workflow", "--action", "inspect", "--episode", KEY, "--json"]
        )
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    out = capsys.readouterr().out
    assert code == 0, out
    assert json.loads(out)["data"]["episode"] == KEY


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
    entity_text = (vault / ENTITY).read_text(encoding="utf-8")
    # A focused claim that cites the recap Source as its provenance.
    _workflow(
        vault,
        action="prepare",
        candidate="indigo-thesis",
        proposal=_proposal(
            "focused_note",
            [
                _note_leaf(
                    "indigo-vat-thesis",
                    "The loom trial keeps indigo vats in a stable warm bath.",
                    sources=[recap_ref],
                )
            ],
            title="Indigo vat thesis",
        ),
    )
    # Hydrate the existing entity rather than create a second one.
    _workflow(
        vault,
        action="prepare",
        candidate="supplier-facet",
        proposal=_proposal(
            "entity",
            [
                {
                    "leaf_key": "hydrate",
                    "effect_revision": 1,
                    "kind": "edit",
                    "args": {
                        "path": ENTITY,
                        "why": "Hydrate the supplier with the episode's decision.",
                        "operation": {
                            "kind": "edit_section",
                            "heading": "Summary",
                            "new_string": "Chosen indigo source for the loom trial.",
                            "section_position": "append",
                            "expected_hash": content_hash(entity_text),
                        },
                    },
                }
            ],
            target=ENTITY,
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
        ("supplier-facet", "routed", "The decision belongs on the supplier's own page."),
        ("kiln-cooperative", "routed", "A stable organization the trial depends on."),
        ("vat-temperature-log", "deferred", "Records execution is not integrated yet."),
        ("kiln-booking", "no_capture", "Only a possibility was mentioned, not intent."),
    ):
        _workflow(vault, action="disposition", candidate=key, disposition=value, reason=reason)


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

    resumed = _workflow(vault, action="resume", input_revision=1)

    assert resumed["status"] == "ok"
    assert len(resumed["executed"]) == 3 and resumed["blocked"] == []
    assert resumed["publication"] == "pending"
    after = _canonical_files(vault)
    note = "Knowledge Base/Notes/Insights/indigo-vat-thesis.md"
    kiln = "Knowledge Base/Entities/Organizations/Ember Kiln Cooperative.md"
    # Exactly the expressed effects: one created note, one created entity, one
    # hydrated entity. No Planning or Records page appears.
    assert set(after) - set(before) == {note, kiln}
    assert {path for path in before if before[path] != after.get(path)} == {ENTITY}
    assert not any("/Planning/" in path or "/Records/" in path for path in set(after) - set(before))

    # Canonical readback through the ordinary read path.
    read = commands.op_read_memory(vault, path=note)
    assert "stable warm bath" in json.dumps(read)
    hydrated = (vault / ENTITY).read_text(encoding="utf-8")
    assert "Chosen indigo source for the loom trial." in hydrated
    assert len(list((vault / ENTITY).parent.glob("Marsh Dyeworks*.md"))) == 1

    # Source/claim: the recap stays a raw episode Source; the claim cites it.
    recap_page = (vault / recap["source"]["path"]).read_text(encoding="utf-8")
    assert "type: source" in recap_page and "source_type: episode" in recap_page
    claim = (vault / note).read_text(encoding="utf-8")
    assert "type: insight" in claim
    assert recap["source"]["path"].removesuffix(".md") in claim

    inspected = _workflow(vault, action="inspect")
    assert _candidate(inspected, "vat-temperature-log")["disposition"] == "deferred"
    assert _candidate(inspected, "kiln-booking")["disposition"] == "no_capture"
    assert inspected["complete"] is False
    assert {c["candidate_key"] for c in inspected["candidates"] if c["pending"]} == {
        "vat-temperature-log"
    }

    # The final pass: an active-agent postcommit attestation that reverifies
    # every committed proof, while the deferred candidate stays pending.
    attested = _workflow(vault, action="resume", input_revision=1, postcommit=True)
    assert attested["executed"] == []
    assert attested["reviewed_through_input_revision"] == 1
    assert attested["complete"] is False


# --------------------------------------------------------------------------- #
# 3.6 — derived state reaches a fresh session, honestly
# --------------------------------------------------------------------------- #


def _fresh_session_index(vault: Path) -> None:
    from exomem import find, lexstore, working_set_index, working_set_runtime

    find.clear_cache()
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()


def test_a_fresh_session_activates_bounded_provenance_bearing_context(
    vault: Path, enabled
) -> None:
    with request_scope(owner_principal(surface="mcp")):
        _seed_entity(vault)
        recap = _record(vault)
        _prepare_slice(vault, recap["source"]["ref"])
        _workflow(vault, action="resume", input_revision=1)
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
                            "entity_type": "location",
                            "name": "Marsh Dyeworks Yard",
                            "summary": "The riverside yard also called Marsh Dyeworks.",
                        },
                    }
                ],
                title="Marsh Dyeworks Yard",
            ),
        )
        _workflow(
            vault, action="disposition", candidate="harbor-site", disposition="routed", reason="r"
        )
        _workflow(vault, action="resume", input_revision=1)
        # The yard shares the supplier's name as an alias: two owners.
        yard = vault / "Knowledge Base/Entities/Locations/Marsh Dyeworks Yard.md"
        text = yard.read_text(encoding="utf-8")
        yard.write_text(text.replace("type: entity\n", "type: entity\naliases: [Marsh Dyeworks]\n", 1), encoding="utf-8")
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
        resumed = _workflow(vault, action="resume", input_revision=1)
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
    for key, slug in (("alpha", "alpha-dye-note"), ("beta", "beta-dye-note"), ("gamma", "gamma-dye-note")):
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
    _record(vault)
    _three_notes(vault)
    inspected = _workflow(vault, action="inspect")
    alpha, beta, gamma = (_leaf_ids(inspected, key)[0] for key in ("alpha", "beta", "gamma"))

    # Interrupt after alpha's writer committed, before the ledger learned it.
    real = EpisodeStore.transition

    def crash_on_reconcile(self, identity, **kwargs):
        if kwargs.get("action") == "reconcile_curation_leaf":
            raise KeyboardInterrupt("host lost the session")
        return real(self, identity, **kwargs)

    monkeypatch.setattr(EpisodeStore, "transition", crash_on_reconcile)
    with pytest.raises(KeyboardInterrupt):
        _workflow(vault, action="resume", input_revision=1, order=[alpha, beta, gamma])
    monkeypatch.setattr(EpisodeStore, "transition", real)

    interrupted = _workflow(vault, action="inspect")
    alpha_leaf = _candidate(interrupted, "alpha")["leaves"][0]
    assert alpha_leaf["outcome"] == "uncertain"
    alpha_path = vault / "Knowledge Base/Notes/Insights/alpha-dye-note.md"
    committed_bytes = alpha_path.read_bytes()
    store = curation.CurationStore(vault)
    original = store.reconstruct(alpha_leaf["run_id"])["receipts"]
    assert len(original) == 1

    # A fresh semantic revision of gamma must pass current validation: its
    # new destination is alpha's committed page, so preparation refuses it.
    with pytest.raises(ValueError, match="CURATION_BINDING_STALE"):
        _workflow(
            vault,
            action="prepare",
            candidate="gamma",
            proposal=_proposal(
                "focused_note",
                [_note_leaf("alpha-dye-note", "Gamma, revised.", revision=2)],
                title="gamma-dye-note",
            ),
        )
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

    # Resume with the remainder reordered.
    resumed = _workflow(vault, action="resume", input_revision=1, order=[gamma, beta])

    assert [item["leaf_id"] for item in resumed["reconciled"]] == [alpha]
    assert [item["leaf_id"] for item in resumed["executed"]] == [gamma, beta]
    assert alpha_path.read_bytes() == committed_bytes
    after = store.reconstruct(alpha_leaf["run_id"])["receipts"]
    assert after == original
    final = _workflow(vault, action="inspect")
    proof = _candidate(final, "alpha")["leaves"][0]
    assert proof["outcome"] == "committed"
    assert proof["receipt_digest"] == curation._digest(original[0])
    assert "corrected" in (vault / "Knowledge Base/Notes/Insights/gamma-dye-note.md").read_text(
        encoding="utf-8"
    )

    # Nothing is left to run, and a repeat resume applies nothing again.
    again = _workflow(vault, action="resume", input_revision=1)
    assert again["executed"] == [] and again["reconciled"] == []
    assert alpha_path.read_bytes() == committed_bytes


def test_an_uncertain_leaf_is_never_retried_under_a_fresh_identity(
    vault: Path, owner, enabled, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(vault)
    _three_notes(vault)
    inspected = _workflow(vault, action="inspect")
    alpha = _leaf_ids(inspected, "alpha")[0]

    def writer_fails(*_args, **_kwargs):
        raise curation.CurationError("CURATION_STEP_FAILED", "writer refused")

    monkeypatch.setattr(curation, "apply", writer_fails)
    resumed = _workflow(vault, action="resume", input_revision=1, order=[alpha])
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
    again = _workflow(vault, action="resume", input_revision=1, order=[alpha])
    assert again["executed"] == []
    assert [item["leaf_id"] for item in again["blocked"]] == [alpha]


def test_a_new_input_revision_requires_fresh_dispositions_before_execution(
    vault: Path, owner, enabled
) -> None:
    _record(vault)
    _three_notes(vault)
    _record(vault, open=["Maybe book the kiln next month", "Check the mordant"])

    with pytest.raises(ValueError, match="EPISODE_INPUT_REVISION_STALE"):
        _workflow(vault, action="resume", input_revision=1)
    with pytest.raises(ValueError, match="EPISODE_COVERAGE_INCOMPLETE"):
        _workflow(vault, action="resume", input_revision=2)
    assert not (vault / "Knowledge Base/Notes/Insights/alpha-dye-note.md").exists()
