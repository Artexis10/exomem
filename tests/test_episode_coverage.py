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

from exomem import commands, episode_workflow
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
