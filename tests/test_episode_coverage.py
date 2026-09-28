"""Episode coverage: attempted, pending and covered-through state, and the final pass.

Contract: openspec/changes/close-memory-loop tasks 4.1 (the ledger state a host
checkpoint reads instead of treating a write as completion) and 4.2 (the
active agent's final coverage pass against the original input plus receipt and
readback evidence, separate from the precommit destination review).

Coverage is the agent's attestation against an input revision. The server
records it, reverifies the committed effects it names and says what comes next;
it never claims the candidates exhaust the input. The omitted-candidate and
misrouted-synthesis cases below are scripted: the script plays the agent that
notices them in the final pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import commands, curation, episode_workflow
from exomem import episode_model as model
from exomem import schema as schema_module
from exomem.episode_store import EpisodeStore
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

KEY = "ep-" + "c0" * 16
INSIGHTS = "Knowledge Base/Notes/Insights"


@pytest.fixture
def owner():
    with request_scope(owner_principal(surface="mcp")):
        yield


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")


def _episode(vault: Path, **kwargs: object) -> dict:
    return commands.op_episode_memory(
        vault, schema_module.load_source_schema(vault), episode=KEY, **kwargs
    )


def _record(vault: Path, **overrides: object) -> dict:
    args: dict[str, object] = {
        "action": "record",
        "subject": "Loom shed lighting review",
        "summary": "Chose warm lamps for the shed; the north window question is settled.",
        "worked_on": ["Reviewed the loom shed lighting"],
        "decided": [
            "Warm lamps light the loom shed",
            "The north window stays unshuttered for daylight",
        ],
        "open": ["Order spare bulbs"],
    }
    args.update(overrides)
    return _episode(vault, **args)


def _note(slug: str, sentence: str, *, key: str = "write", revision: int = 1) -> dict:
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
        },
    }


def _proposal(route: str, leaves: list[dict], **fields: object) -> dict:
    return {
        "route": route,
        "alternatives": [],
        "evidence": "complete",
        "reason": "The active agent chose this home.",
        "leaves": leaves,
        **fields,
    }


def _route(vault: Path, candidate: str, proposal: dict, disposition: str = "routed") -> dict:
    _episode(vault, action="prepare", candidate=candidate, proposal=proposal)
    return _episode(
        vault, action="disposition", candidate=candidate, disposition=disposition, reason="r"
    )


def _resume(vault: Path, reviewed: dict, **kwargs: object) -> dict:
    return _episode(
        vault,
        action="resume",
        input_revision=reviewed["input_revision"],
        journal_digest=reviewed["journal_digest"],
        **kwargs,
    )


def _state(vault: Path) -> dict:
    store = EpisodeStore(vault, owner_audience_id=owner_principal(surface="mcp").audience_id)
    return store.read(model.episode_id(KEY))["state"]


def _canonical_files(vault: Path) -> dict[str, bytes]:
    kb = vault / "Knowledge Base"
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in kb.rglob("*.md")
        if "_Governance" not in path.relative_to(kb).parts
        and not path.relative_to(kb).parts[0].startswith(".")
    }


# --------------------------------------------------------------------------- #
# 4.1 — the ledger's attempted/pending/covered-through state
# --------------------------------------------------------------------------- #


def test_the_ledger_separates_attempted_pending_and_covered_through(
    vault: Path, owner, enabled
) -> None:
    _record(vault)
    assert _episode(vault, action="candidates")["coverage"]["next"] == "none"

    for key, slug in (("lamps", "warm-shed-lamps"), ("window", "north-window-daylight")):
        _episode(
            vault,
            action="prepare",
            candidate=key,
            proposal=_proposal("focused_note", [_note(slug, f"The {key} choice holds.")], title=slug),
        )
    _episode(
        vault,
        action="prepare",
        candidate="bulbs",
        proposal=_proposal("planning", [], title="Order spare bulbs"),
    )
    undecided = _episode(vault, action="candidates")["coverage"]
    assert undecided["next"] == "decide" and undecided["attempted"] == 0
    assert undecided["covered_through_input_revision"] is None

    for key, value in (("lamps", "routed"), ("window", "routed"), ("bulbs", "deferred")):
        reviewed = _episode(
            vault, action="disposition", candidate=key, disposition=value, reason="r"
        )
    assert reviewed["coverage"]["next"] == "resume"

    # One committed write leaves the rest pending: never complete.
    partial = _resume(vault, reviewed, max_leaves=1)
    assert len(partial["executed"]) == 1
    assert partial["coverage"]["next"] == "resume"
    assert partial["coverage"]["attempted"] == 1
    assert partial["complete"] is False

    finished = _resume(vault, partial)
    assert finished["coverage"]["next"] == "attest"
    assert finished["coverage"]["attempted"] == 2
    assert finished["coverage"]["covered_through_input_revision"] is None

    attested = _resume(vault, finished, postcommit=True)
    coverage = attested["coverage"]
    # Deferred work stays pending and honest; nothing more is due this revision.
    assert coverage["next"] == "none" and coverage["pending"] >= 1
    assert attested["complete"] is False
    assert coverage["basis"] == "agent_attestation"


# --------------------------------------------------------------------------- #
# 4.2 — the final coverage pass
# --------------------------------------------------------------------------- #


def test_the_final_pass_reads_the_input_and_each_receipt_without_writing(
    vault: Path, owner, enabled
) -> None:
    recap = _record(vault)
    reviewed = _route(
        vault,
        "lamps",
        _proposal("focused_note", [_note("warm-shed-lamps", "Warm lamps light the shed.")], title="Lamps"),
    )
    executed = _resume(vault, reviewed)
    note = f"{INSIGHTS}/warm-shed-lamps.md"
    files, journal = _canonical_files(vault), _state(vault)

    passed = _episode(vault, action="coverage")

    assert _canonical_files(vault) == files and _state(vault) == journal
    assert passed["input"] == {
        "input_revision": 1,
        "ref": recap["source"]["ref"],
        "recovery": "available",
    }
    (receipt,) = passed["receipts"]
    leaf = passed["candidates"][0]["leaves"][0]
    assert receipt == {
        "candidate_key": "lamps",
        "leaf_id": leaf["leaf_id"],
        "operation_id": leaf["operation_id"],
        "receipt_digest": leaf["receipt_digest"],
        "path": note,
        "readback": "verified",
    }
    assert passed["coverage_current"] == "verified"
    assert passed["journal_digest"] == executed["journal_digest"]
    # The precommit destination review and the final pass are separate records.
    state = _state(vault)
    assert len(state["precommit_attestations"]) == 1
    assert state["postcommit_attestations"] == []
    attested = _resume(vault, passed, postcommit=True)
    assert attested["complete"] is True
    assert len(_state(vault)["postcommit_attestations"]) == 1


def test_a_deliberately_omitted_candidate_is_noticed_in_the_final_pass(
    vault: Path, owner, enabled
) -> None:
    recap = _record(vault)
    # The first plan covers the lamps and silently drops the window decision.
    reviewed = _route(
        vault,
        "lamps",
        _proposal("focused_note", [_note("warm-shed-lamps", "Warm lamps light the shed.")], title="Lamps"),
    )
    executed = _resume(vault, reviewed)
    # An attestation over that plan is only the agent's word: the server
    # records it complete without claiming the input was exhausted.
    hasty = _resume(vault, executed, postcommit=True)
    assert hasty["complete"] is True
    assert hasty["coverage"]["basis"] == "agent_attestation"

    # The final pass puts the original input beside what was written.
    passed = _episode(vault, action="coverage")
    original = commands.op_read_memory(vault, path=passed["input"]["ref"])
    assert "north window stays unshuttered" in json.dumps(original, default=str)
    assert [item["candidate_key"] for item in passed["receipts"]] == ["lamps"]

    # The agent notices the omission and declares it: current coverage is
    # withdrawn, the earlier attestation stays in history.
    _episode(
        vault,
        action="prepare",
        candidate="window",
        proposal=_proposal(
            "focused_note",
            [_note("north-window-daylight", "The north window stays unshuttered.")],
            title="North window",
        ),
    )
    reopened = _episode(vault, action="candidates")
    assert reopened["complete"] is False
    assert reopened["coverage"]["next"] == "decide"
    assert reopened["coverage"]["covered_through_input_revision"] is None
    assert reopened["coverage"]["historically_covered_through"] == 1

    for key in ("lamps", "window"):
        reviewed = _episode(
            vault, action="disposition", candidate=key, disposition="routed", reason="r"
        )
    done = _resume(vault, reviewed)
    assert [item["path"] for item in done["executed"]] == [f"{INSIGHTS}/north-window-daylight.md"]
    covered = _resume(vault, _episode(vault, action="coverage"), postcommit=True)
    assert covered["complete"] is True and covered["coverage"]["next"] == "none"
    assert recap["source"]["ref"] == passed["input"]["ref"]


def test_a_misrouted_synthesis_is_noticed_from_its_receipt(vault: Path, owner, enabled) -> None:
    _record(vault)
    lamps = _route(
        vault,
        "lamps",
        _proposal("focused_note", [_note("warm-shed-lamps", "Warm lamps light the shed.")], title="Lamps"),
    )
    _resume(vault, lamps)
    page = f"{INSIGHTS}/warm-shed-lamps.md"
    hash_ = commands.op_read_memory(vault, path=page)["content_hash"]
    # The shed-lighting thesis is appended to the lamp page instead of a home.
    misrouted = _route(
        vault,
        "thesis",
        _proposal(
            "existing_page",
            [
                {
                    "leaf_key": "home",
                    "effect_revision": 1,
                    "kind": "edit",
                    "args": {
                        "path": page,
                        "why": "Append the thesis.",
                        "operation": {
                            "kind": "edit_section",
                            "heading": "Observations",
                            "new_string": "- [finding] Warm light and daylight are one plan. ^plan",
                            "section_position": "append",
                            "expected_hash": hash_,
                            "relation_disposition": "reviewed_none",
                            "relation_review_reason": "No supported relation here.",
                        },
                    },
                }
            ],
            target=page,
        ),
    )
    landed = _resume(vault, misrouted)
    assert landed["status"] == "ok", (landed["blocked"], landed["stale"])

    passed = _episode(vault, action="coverage")
    thesis = next(item for item in passed["receipts"] if item["candidate_key"] == "thesis")
    # The receipt shows where the synthesis landed: on the narrower lamp page.
    assert thesis["path"] == page and thesis["readback"] == "verified"

    # The committed misroute stays history; it cannot be rewritten or removed.
    with pytest.raises(ValueError, match="EPISODE_ATTEMPTED_LEAF"):
        _episode(
            vault,
            action="prepare",
            candidate="thesis",
            proposal=_proposal(
                "focused_note",
                [_note("shed-lighting-plan", "Warm light and daylight are one plan.", key="home", revision=2)],
                title="Shed lighting plan",
            ),
        )
    # A corrective candidate gives the thesis its home.
    _episode(
        vault,
        action="prepare",
        candidate="thesis-home",
        proposal=_proposal(
            "focused_note",
            [_note("shed-lighting-plan", "Warm light and daylight are one plan.")],
            title="Shed lighting plan",
        ),
    )
    assert _episode(vault, action="candidates")["coverage"]["next"] == "decide"


def test_a_later_correction_invalidates_current_coverage_but_keeps_its_history(
    vault: Path, owner, enabled
) -> None:
    _record(vault)
    reviewed = _route(
        vault,
        "lamps",
        _proposal("focused_note", [_note("warm-shed-lamps", "Warm lamps light the shed.")], title="Lamps"),
    )
    covered = _resume(vault, _resume(vault, reviewed), postcommit=True)
    assert covered["covered_through_input_revision"] == 1

    # The user corrects a claim: a new input revision.
    _record(vault, decided=["Warm lamps light the shed", "The north window gets a shutter"])
    corrected = _episode(vault, action="candidates")
    assert corrected["input_revision"] == 2
    assert corrected["complete"] is False
    assert corrected["coverage"]["covered_through_input_revision"] is None
    assert corrected["coverage"]["historically_covered_through"] == 1
    assert corrected["coverage"]["next"] == "decide"
    with pytest.raises(ValueError, match="EPISODE_INPUT_REVISION_STALE"):
        _episode(
            vault,
            action="resume",
            input_revision=1,
            postcommit=True,
            journal_digest=corrected["journal_digest"],
        )

    # Reconsidered against revision two, coverage advances only by attestation.
    reviewed = _episode(
        vault, action="disposition", candidate="lamps", disposition="routed", reason="Still holds."
    )
    readied = _resume(vault, reviewed)
    assert readied["coverage"]["next"] == "attest"
    advanced = _resume(vault, readied, postcommit=True)
    assert advanced["covered_through_input_revision"] == 2


def test_a_changed_write_reads_back_changed_and_cannot_be_attested(
    vault: Path, owner, enabled
) -> None:
    _record(vault)
    reviewed = _route(
        vault,
        "lamps",
        _proposal("focused_note", [_note("warm-shed-lamps", "Warm lamps light the shed.")], title="Lamps"),
    )
    executed = _resume(vault, reviewed)
    note = vault / INSIGHTS / "warm-shed-lamps.md"
    note.write_text(note.read_text(encoding="utf-8") + "\nEdited later.\n", encoding="utf-8")

    passed = _episode(vault, action="coverage")

    assert passed["receipts"][0]["readback"] == "changed"
    assert passed["coverage_current"] == "changed"
    with pytest.raises(ValueError, match="EPISODE_OUTCOME_UNCERTAIN"):
        _resume(vault, executed, postcommit=True)


def _withhold_notes_from(vault: Path, audience: str) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "withheld-notes.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAC\n"
        "name: Withheld notes\n"
        'paths: ["Notes/Insights/withheld-*.md"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "withheld-notes.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAD\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAC"]\n'
        f"audience: {audience}\n"
        "ceiling: 0\n",
        encoding="utf-8",
    )
    egress.clear_decision_memo()
    from exomem.governance import membership, policy

    membership.clear_memo()
    policy._CACHE.clear()  # noqa: SLF001


def test_a_withheld_write_reads_back_exactly_like_a_deleted_one(vault: Path, enabled) -> None:
    with request_scope(RequestPrincipal(audience_id="client-a", surface="mcp")):
        _record(vault)
        for key, slug in (("hidden", "withheld-lamp-note"), ("gone", "deleted-lamp-note")):
            reviewed = _route(
                vault,
                key,
                _proposal("focused_note", [_note(slug, f"The {key} note holds.")], title=slug),
            )
        _resume(vault, reviewed)
    (vault / INSIGHTS / "deleted-lamp-note.md").unlink()
    _withhold_notes_from(vault, "client-a")

    with request_scope(RequestPrincipal(audience_id="client-a", surface="mcp")):
        passed = _episode(vault, action="coverage")

    by_key = {item["candidate_key"]: item for item in passed["receipts"]}
    shape = {key: {k: v for k, v in item.items() if k in {"path", "readback"}} for key, item in by_key.items()}
    assert shape["hidden"] == shape["gone"] == {"path": None, "readback": "unavailable"}
    assert "withheld-lamp-note" not in json.dumps(passed)


def test_the_coverage_pass_is_a_read_only_action_of_the_command() -> None:
    product = {command.name: command for command in commands.PRODUCT_COMMANDS}
    command = product["episode_memory"]
    assert commands.invocation_is_read_only(command, {"action": "coverage"}) is True
    with pytest.raises(ValueError, match="EPISODE_INVALID"):
        commands.op_episode_memory(Path("."), None, action="coverage", episode=KEY, candidate="x")


# --------------------------------------------------------------------------- #
# Historical versus current coverage: one chain per path (ruling on 4.2)
# --------------------------------------------------------------------------- #
#
# A later leaf of the same episode may edit a page an earlier leaf wrote. Each
# earlier leaf's recorded result must equal the next leaf's recorded starting
# state on that path, and only the last leaf per path is checked against the
# live page. Any gap -- a missing receipt, a mismatched hash, or a live page
# that differs from the last leaf -- leaves coverage unproven.

ENTITY = "Knowledge Base/Entities/Organizations/Marsh Dyeworks.md"
THESIS = f"{INSIGHTS}/indigo-vat-thesis.md"


def _seed_entity(vault: Path) -> None:
    from exomem import curation

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
                        "summary": "Dye supplier for the loom trial.",
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


def _create_thesis(vault: Path) -> dict:
    reviewed = _route(
        vault,
        "thesis",
        _proposal(
            "focused_note",
            [
                {
                    "leaf_key": "write",
                    "effect_revision": 1,
                    "kind": "create-note",
                    "args": {
                        "title": "Indigo vat thesis",
                        "slug": "indigo-vat-thesis",
                        "content": (
                            "## Observations\n\n- [finding] Indigo vats keep a stable warm"
                            " bath. ^indigo-vat-thesis\n\n"
                            f"## Relations\n\nSee [[{ENTITY.removesuffix('.md')}]].\n"
                        ),
                    },
                }
            ],
            title="Indigo vat thesis",
        ),
    )
    return _resume(vault, reviewed)


def _accept_link(vault: Path) -> dict:
    """A later leaf accepts a relation on the note the earlier leaf created."""
    from exomem import deferred_index, epistemic_graph, find, index_sync, semantic_contract

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
        if item["from"] == THESIS and item["to"] == ENTITY
    )
    expected_hash = next(g["content_hash"] for g in review["groups"] if g["path"] == THESIS)
    reviewed = _route(
        vault,
        "link",
        _proposal(
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
    executed = _resume(vault, reviewed)
    assert executed["status"] == "ok", (executed["blocked"], executed["stale"])
    assert [row["path"] for row in executed["executed"]] == [THESIS]
    return executed


def _chained(vault: Path) -> dict:
    _record(vault)
    _seed_entity(vault)
    _create_thesis(vault)
    return _accept_link(vault)


def test_a_relation_accepted_on_an_earlier_leafs_note_still_attests(
    vault: Path, owner, enabled
) -> None:
    executed = _chained(vault)

    passed = _episode(vault, action="coverage")
    assert {row["candidate_key"]: row["readback"] for row in passed["receipts"]} == {
        "thesis": "verified",
        "link": "verified",
    }
    attested = _resume(vault, executed, postcommit=True)
    assert attested["complete"] is True
    assert attested["covered_through_input_revision"] == 1


def test_a_missing_receipt_breaks_the_chain(vault: Path, owner, enabled) -> None:
    from exomem import curation

    executed = _chained(vault)
    thesis_run = next(
        c["leaves"][0]["run_id"] for c in executed["candidates"] if c["candidate_key"] == "thesis"
    )
    receipts = list(curation.CurationStore(vault).receipts_dir(thesis_run).rglob("*.json"))
    assert receipts
    for receipt in receipts:
        receipt.unlink()

    with pytest.raises(ValueError, match="EPISODE_OUTCOME_UNCERTAIN"):
        _resume(vault, executed, postcommit=True)


def test_an_unrecorded_edit_between_leaves_breaks_the_chain(vault: Path, owner, enabled) -> None:
    _record(vault)
    _seed_entity(vault)
    _create_thesis(vault)
    # The page changes between the two leaves, outside the episode: the later
    # leaf's recorded starting state no longer equals the earlier leaf's result.
    page = vault / THESIS
    page.write_text(page.read_text(encoding="utf-8") + "\nAn unrecorded edit.\n", encoding="utf-8")
    executed = _accept_link(vault)

    passed = _episode(vault, action="coverage")
    assert passed["coverage_current"] == "changed"
    with pytest.raises(ValueError, match="EPISODE_OUTCOME_UNCERTAIN"):
        _resume(vault, executed, postcommit=True)


def test_a_live_page_that_differs_from_the_last_leaf_breaks_the_chain(
    vault: Path, owner, enabled
) -> None:
    executed = _chained(vault)
    page = vault / THESIS
    page.write_text(page.read_text(encoding="utf-8") + "\nEdited after both.\n", encoding="utf-8")

    passed = _episode(vault, action="coverage")
    assert {row["readback"] for row in passed["receipts"]} == {"changed"}
    with pytest.raises(ValueError, match="EPISODE_OUTCOME_UNCERTAIN"):
        _resume(vault, executed, postcommit=True)


# --------------------------------------------------------------------------- #
# Security review round (M1): a page withheld after its leaf was prepared or
# committed answers exactly like a page that is gone -- before an attempt, and
# at the postcommit attestation -- so neither is a one-bit existence oracle.
# --------------------------------------------------------------------------- #

_IDS = {"episode", "episode_id", "journal_digest", "revision", "leaf_id", "candidate_id",
        "run_id", "operation_id", "effect_digest", "receipt_digest", "path", "ref"}


def _shape(value: object) -> object:
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items() if key not in _IDS}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    return value


def _client_episode(vault: Path, key: str, **kwargs: object) -> dict:
    return commands.op_episode_memory(
        vault, schema_module.load_source_schema(vault), episode=key, **kwargs
    )


def _seed_insight(vault: Path, slug: str, memory_id: str) -> str:
    path = vault / INSIGHTS / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: insight\nexomem_id: {memory_id}\ntitle: "
        + slug
        + "\nstatus: active\ncreated: 2026-09-20\nupdated: 2026-09-20\nsources: []\ntags: []\n"
        "---\n\n## Observations\n\n- [finding] A seeded observation. ^seed\n",
        encoding="utf-8",
    )
    return f"{INSIGHTS}/{slug}.md"


def _edit_leaf(vault: Path, page: str) -> dict:
    return {
        "leaf_key": "append",
        "effect_revision": 1,
        "kind": "edit",
        "args": {
            "path": page,
            "why": "A scope-owned detail.",
            "operation": {
                "kind": "edit_section",
                "heading": "Observations",
                "new_string": "- [finding] A later detail. ^later",
                "section_position": "append",
                "expected_hash": commands.op_read_memory(vault, path=page)["content_hash"],
                "relation_disposition": "reviewed_none",
                "relation_review_reason": "No supported relation here.",
            },
        },
    }


def test_a_page_withheld_before_its_attempt_resumes_exactly_like_a_deleted_one(
    vault: Path, enabled
) -> None:
    pages = {
        "withheld": _seed_insight(vault, "withheld-dye-note", "5a5a5a5a-1111-4111-8111-111111111111"),
        "deleted": _seed_insight(vault, "gone-dye-note", "5b5b5b5b-2222-4222-8222-222222222222"),
    }
    keys = {"withheld": "ep-" + "a1" * 16, "deleted": "ep-" + "b2" * 16}
    client = RequestPrincipal(audience_id="client-a", surface="mcp")
    reviewed = {}
    with request_scope(client):
        for case, key in keys.items():
            _client_episode(
                vault, key, action="record", subject="Dye note", summary="A detail.",
                decided=["Add the detail"],
            )
            _client_episode(
                vault,
                key,
                action="prepare",
                candidate="detail",
                proposal=_proposal("existing_page", [_edit_leaf(vault, pages[case])], target=pages[case]),
            )
            reviewed[case] = _client_episode(
                vault, key, action="disposition", candidate="detail", disposition="routed", reason="r"
            )
    _withhold_notes_from(vault, "client-a")
    (vault / pages["deleted"]).unlink()

    with request_scope(client):
        outcomes = {
            case: _client_episode(
                vault,
                key,
                action="resume",
                input_revision=1,
                journal_digest=reviewed[case]["journal_digest"],
            )
            for case, key in keys.items()
        }
    assert json.dumps(_shape(outcomes["withheld"]), sort_keys=True) == json.dumps(
        _shape(outcomes["deleted"]), sort_keys=True
    )
    # The precommit destination review answers first for a bound home.
    assert [item["code"] for item in outcomes["withheld"]["stale"]] == ["EPISODE_DESTINATION_STALE"]
    leaf = outcomes["withheld"]["candidates"][0]["leaves"][0]
    assert leaf["outcome"] == "pending" and leaf["attempts"] == 0


def _linked_insight(vault: Path, slug: str, memory_id: str) -> str:
    path = vault / INSIGHTS / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: insight\nexomem_id: {memory_id}\ntitle: {slug}\nstatus: active\n"
        "created: 2026-09-20\nupdated: 2026-09-20\nsources: []\ntags: []\n---\n\n"
        "## Observations\n\n- [finding] Indigo vats keep a stable warm bath. ^seed\n\n"
        f"## Relations\n\nSee [[{ENTITY.removesuffix('.md')}]].\n",
        encoding="utf-8",
    )
    return f"{INSIGHTS}/{slug}.md"


def _relation_leaf(vault: Path, page: str) -> dict:
    from exomem import deferred_index, epistemic_graph, find, index_sync, semantic_contract

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
        if item["from"] == page and item["to"] == ENTITY
    )
    return {
        "leaf_key": "accept",
        "effect_revision": 1,
        "kind": "accept-relation",
        "args": {
            "ref": item["ref"],
            "expected_hash": next(g["content_hash"] for g in review["groups"] if g["path"] == page),
            "why": "The note names the supplier it depends on.",
            "expected_fingerprint": item["fingerprint"],
        },
    }


@pytest.mark.parametrize("registry_changed", [False, True], ids=["as-sealed", "registry-changed"])
def test_a_relation_source_withheld_before_its_attempt_resumes_like_a_deleted_one(
    vault: Path, enabled, registry_changed: bool
) -> None:
    """A page leaf with no bound home: only its own permission check answers.

    With a routine registry change after the page went away (recheck N1), the
    deleted page's preview puts `CURATION_REGISTRY_CHANGED` first; the withheld
    page must run the same preview and answer the same list, not a code of its
    own.
    """
    with request_scope(owner_principal(surface="mcp")):
        _seed_entity(vault)
    pages = {
        "withheld": _linked_insight(vault, "withheld-vat-note", "6a6a6a6a-1111-4111-8111-111111111111"),
        "deleted": _linked_insight(vault, "gone-vat-note", "6b6b6b6b-2222-4222-8222-222222222222"),
    }
    keys = {"withheld": "ep-" + "e5" * 16, "deleted": "ep-" + "f6" * 16}
    client = RequestPrincipal(audience_id="client-a", surface="mcp")
    reviewed = {}
    with request_scope(client):
        for case, key in keys.items():
            _client_episode(
                vault, key, action="record", subject="Vat note", summary="A link.",
                decided=["Link the supplier"],
            )
            _client_episode(
                vault,
                key,
                action="prepare",
                candidate="link",
                proposal=_proposal("relation_only", [_relation_leaf(vault, pages[case])], target=ENTITY),
            )
            reviewed[case] = _client_episode(
                vault, key, action="disposition", candidate="link", disposition="routed", reason="r"
            )
    _withhold_notes_from(vault, "client-a")
    (vault / pages["deleted"]).unlink()
    if registry_changed:
        contracts = vault / "Knowledge Base" / "_Schema" / "contracts"
        contracts.mkdir(parents=True, exist_ok=True)
        (contracts / "extra.yaml").write_text("x: 1\n", encoding="utf-8")

    with request_scope(client):
        keep = egress.restricted_release_filter(vault, principal=client)
        previews = {
            case: curation.preview(
                vault,
                run_id=reviewed[case]["candidates"][0]["leaves"][0]["run_id"],
                keep=keep,
            )["blockers"]
            for case in keys
        }
        outcomes = {
            case: _client_episode(
                vault,
                key,
                action="resume",
                input_revision=1,
                journal_digest=reviewed[case]["journal_digest"],
            )
            for case, key in keys.items()
        }
    # The same blockers, in the same order, for withheld and deleted alike.
    assert _shape(previews["withheld"]) == _shape(previews["deleted"])
    assert json.dumps(_shape(outcomes["withheld"]), sort_keys=True) == json.dumps(
        _shape(outcomes["deleted"]), sort_keys=True
    )
    first = "CURATION_REGISTRY_CHANGED" if registry_changed else "CURATION_BINDING_STALE"
    assert [item["code"] for item in outcomes["withheld"]["stale"]] == [first]
    leaf = outcomes["withheld"]["candidates"][0]["leaves"][0]
    assert leaf["outcome"] == "pending" and leaf["attempts"] == 0


def test_a_page_withheld_after_commit_attests_exactly_like_a_deleted_one(
    vault: Path, enabled
) -> None:
    slugs = {"withheld": "withheld-lamp-note", "deleted": "gone-lamp-note"}
    keys = {"withheld": "ep-" + "c3" * 16, "deleted": "ep-" + "d4" * 16}
    client = RequestPrincipal(audience_id="client-a", surface="mcp")
    executed = {}
    with request_scope(client):
        for case, key in keys.items():
            _client_episode(
                vault, key, action="record", subject="Lamp note", summary="A lamp note.",
                decided=["Keep the lamp note"],
            )
            _client_episode(
                vault,
                key,
                action="prepare",
                candidate="lamp",
                proposal=_proposal("focused_note", [_note(slugs[case], "The lamp note holds.")], title="Lamp"),
            )
            reviewed = _client_episode(
                vault, key, action="disposition", candidate="lamp", disposition="routed", reason="r"
            )
            executed[case] = _client_episode(
                vault,
                key,
                action="resume",
                input_revision=1,
                journal_digest=reviewed["journal_digest"],
            )
            assert executed[case]["status"] == "ok", executed[case]["blocked"]
    _withhold_notes_from(vault, "client-a")
    (vault / INSIGHTS / f"{slugs['deleted']}.md").unlink()

    errors = {}
    passes = {}
    with request_scope(client):
        for case, key in keys.items():
            passes[case] = _client_episode(vault, key, action="coverage")
            with pytest.raises(ValueError) as refused:
                _client_episode(
                    vault,
                    key,
                    action="resume",
                    input_revision=1,
                    postcommit=True,
                    journal_digest=executed[case]["journal_digest"],
                )
            errors[case] = str(refused.value)
    assert errors["withheld"] == errors["deleted"]
    assert "EPISODE_OUTCOME_UNCERTAIN" in errors["withheld"]
    assert json.dumps(_shape(passes["withheld"]), sort_keys=True) == json.dumps(
        _shape(passes["deleted"]), sort_keys=True
    )


def test_an_uncertain_leaf_whose_page_is_withheld_resumes_like_a_deleted_one(
    vault: Path, enabled, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the writer committed leaves the leaf uncertain; its page
    withheld afterwards must not reconcile where a deleted one would not."""
    slugs = {"withheld": "withheld-crash-note", "deleted": "gone-crash-note"}
    keys = {"withheld": "ep-" + "a9" * 16, "deleted": "ep-" + "b9" * 16}
    client = RequestPrincipal(audience_id="client-a", surface="mcp")
    real = EpisodeStore.transition

    def crash_on_reconcile(self, identity, **kwargs):
        if kwargs.get("action") == "reconcile_curation_leaf":
            raise KeyboardInterrupt("host lost the session")
        return real(self, identity, **kwargs)

    with request_scope(client):
        for case, key in keys.items():
            _client_episode(
                vault, key, action="record", subject="Crash note", summary="A crash note.",
                decided=["Keep the crash note"],
            )
            _client_episode(
                vault,
                key,
                action="prepare",
                candidate="note",
                proposal=_proposal("focused_note", [_note(slugs[case], "The crash note holds.")], title="Crash"),
            )
            reviewed = _client_episode(
                vault, key, action="disposition", candidate="note", disposition="routed", reason="r"
            )
            monkeypatch.setattr(EpisodeStore, "transition", crash_on_reconcile)
            with pytest.raises(KeyboardInterrupt):
                _client_episode(
                    vault,
                    key,
                    action="resume",
                    input_revision=1,
                    journal_digest=reviewed["journal_digest"],
                )
            monkeypatch.setattr(EpisodeStore, "transition", real)
    _withhold_notes_from(vault, "client-a")
    (vault / INSIGHTS / f"{slugs['deleted']}.md").unlink()

    with request_scope(client):
        outcomes = {}
        for case, key in keys.items():
            digest = _client_episode(vault, key, action="candidates")["journal_digest"]
            outcomes[case] = _client_episode(
                vault, key, action="resume", input_revision=1, journal_digest=digest
            )
    assert json.dumps(_shape(outcomes["withheld"]), sort_keys=True) == json.dumps(
        _shape(outcomes["deleted"]), sort_keys=True
    )
    assert [item["code"] for item in outcomes["withheld"]["blocked"]] == [
        "EPISODE_OUTCOME_UNCERTAIN"
    ]


# --------------------------------------------------------------------------- #
# Semantic sink: one page receiving several distinct topic clusters
# --------------------------------------------------------------------------- #


def _append_cluster(vault: Path, key: str, page: str, sentence: str) -> None:
    hash_ = commands.op_read_memory(vault, path=page)["content_hash"]
    reviewed = _route(
        vault,
        key,
        _proposal(
            "existing_page",
            [
                {
                    "leaf_key": "home",
                    "effect_revision": 1,
                    "kind": "edit",
                    "args": {
                        "path": page,
                        "why": "Append the cluster.",
                        "operation": {
                            "kind": "edit_section",
                            "heading": "Observations",
                            "new_string": f"- [finding] {sentence} ^{key}",
                            "section_position": "append",
                            "expected_hash": hash_,
                            "relation_disposition": "reviewed_none",
                            "relation_review_reason": "No supported relation here.",
                        },
                    },
                }
            ],
            target=page,
        ),
    )
    assert _resume(vault, reviewed)["status"] == "ok"


def test_a_page_receiving_several_topic_clusters_is_flagged_as_a_sink(
    vault: Path, owner, enabled
) -> None:
    _record(vault)
    _resume(
        vault,
        _route(
            vault,
            "hub",
            _proposal("focused_note", [_note("shed-odds-and-ends", "The shed needs care.")], title="Shed"),
        ),
    )
    page = f"{INSIGHTS}/shed-odds-and-ends.md"
    # One page is not a sink; the flag is for several distinct clusters.
    assert _episode(vault, action="coverage")["sink"] == []
    _append_cluster(vault, "schedule", page, "Loom class runs on Tuesdays.")
    assert _episode(vault, action="coverage")["sink"] == []
    _append_cluster(vault, "supplies", page, "Spare bulbs are ordered.")

    passed = _episode(vault, action="coverage")
    assert passed["sink"] == [
        {"path": page, "candidates": ["hub", "schedule", "supplies"]}
    ]
    ask = passed["sink_guidance"]
    # Per-cluster disposition, with the honest exits named; guidance, not a block.
    for phrase in ("each cluster", "existing canonical page", "no_capture"):
        assert phrase in ask
    assert passed["coverage_current"] == "verified"


def test_distinct_pages_are_not_a_sink(vault: Path, owner, enabled) -> None:
    _record(vault)
    for key, slug in (("a", "lamp-one"), ("b", "lamp-two"), ("c", "lamp-three")):
        _resume(
            vault,
            _route(vault, key, _proposal("focused_note", [_note(slug, f"{slug} fact.")], title=slug)),
        )
    passed = _episode(vault, action="coverage")
    assert passed["sink"] == [] and "sink_guidance" not in passed
