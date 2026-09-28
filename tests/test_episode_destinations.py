"""Episode destination decisions and their precommit review (close-memory-loop 3.8, 3.9).

A candidate's destination is the active agent's judgment: which home, which
inspected alternatives it weighed and why. The server records that decision
and checks only its structure: the alternatives are pages this caller can see
at the versions it read, an existing-page home is the one page its leaves
write, and evidence that changed before commit needs fresh consideration. It
never scores fitness, asks for another confirmation or runs a second executor;
every leaf still runs through the existing curation writers.

The synthesis/refinement pair below is a scripted writer check: the script
makes the agent's choices, so it proves the plumbing supports a first-pass
focused home, scope-owned updates and an unfragmented refinement. Whether an
ordinary agent initiates those choices without a nudge is measured elsewhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest

from exomem import commands, curation, episode_workflow, memory_refs
from exomem import episode_model as model
from exomem import schema as schema_module
from exomem.episode_store import EpisodeStore
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from exomem.vault import content_hash

KEY = "ep-" + "7d" * 16
INSIGHTS = "Knowledge Base/Notes/Insights"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def owner():
    with request_scope(owner_principal(surface="mcp")):
        yield


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")


def _episode(vault: Path, *, key: str = KEY, **kwargs: object) -> dict:
    return commands.op_episode_memory(
        vault, schema_module.load_source_schema(vault), episode=key, **kwargs
    )


def _record(vault: Path, *, key: str = KEY, **overrides: object) -> dict:
    args: dict[str, object] = {
        "action": "record",
        "subject": "Tidewater workshop operating model",
        "summary": "Named the steady-bath operating model behind five workshop practices.",
        "worked_on": ["Connected the five workshop practice notes"],
        "decided": ["The steady-bath operating model is its own thesis"],
        "open": ["Check the soak timing next week"],
    }
    args.update(overrides)
    return _episode(vault, key=key, **args)


def _write_note(vault: Path, slug: str, memory_id: str, body: str) -> str:
    path = vault / INSIGHTS / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        f"exomem_id: {memory_id}\n"
        f"title: {slug.replace('-', ' ').title()}\n"
        "status: active\n"
        "created: 2026-09-20\n"
        "updated: 2026-09-20\n"
        "sources: []\n"
        "tags: []\n"
        "---\n\n"
        f"# {slug.replace('-', ' ').title()}\n\n"
        f"## Observations\n\n- [finding] {body} ^{slug}\n",
        encoding="utf-8",
    )
    return f"{INSIGHTS}/{slug}.md"


def _ref(vault: Path, path: str) -> str:
    ref = memory_refs.ref_from_markdown((vault / path).read_text(encoding="utf-8"))
    assert ref is not None
    return ref


def _version(vault: Path, path: str) -> str:
    return content_hash((vault / path).read_text(encoding="utf-8"))


def _alternative(vault: Path, path: str, scope: str) -> dict:
    return {"target": _ref(vault, path), "scope": scope, "version": _version(vault, path)}


def _note_leaf(slug: str, title: str, content: str, *, key: str = "write") -> dict:
    return {
        "leaf_key": key,
        "effect_revision": 1,
        "kind": "create-note",
        "args": {
            "title": title,
            "slug": slug,
            "content": content,
            "relation_disposition": "reviewed_none",
            "relation_review_reason": "The synthetic fixture authors its relations inline.",
        },
    }


def _append_leaf(vault: Path, path: str, line: str, *, key: str = "append") -> dict:
    return {
        "leaf_key": key,
        "effect_revision": 1,
        "kind": "edit",
        "args": {
            "path": path,
            "why": "A scope-owned detail from the episode.",
            "operation": {
                "kind": "edit_section",
                "heading": "Observations",
                "new_string": line,
                "section_position": "append",
                "expected_hash": _version(vault, path),
            },
        },
    }


def _proposal(
    route: str,
    leaves: list[dict],
    *,
    title: str | None = None,
    target: str | None = None,
    alternatives: list[dict] | None = None,
    reason: str = "The active agent weighed the inspected pages and chose this home.",
) -> dict:
    proposal: dict[str, object] = {
        "route": route,
        "alternatives": sorted(alternatives or [], key=model._json),  # noqa: SLF001
        "evidence": "complete",
        "reason": reason,
        "leaves": leaves,
    }
    if title is not None:
        proposal["title"] = title
    if target is not None:
        proposal["target"] = target
    return proposal


def _journal(vault: Path, key: str = KEY) -> bytes:
    store = EpisodeStore(vault, owner_audience_id=owner_principal(surface="mcp").audience_id)
    return store.path(model.episode_id(key)).read_bytes()


def _canonical_files(vault: Path) -> dict[str, bytes]:
    kb = vault / "Knowledge Base"
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in kb.rglob("*.md")
        if "_Governance" not in path.relative_to(kb).parts
        and not path.relative_to(kb).parts[0].startswith(".")
    }


def _candidate(result: dict, key: str) -> dict:
    return next(item for item in result["candidates"] if item["candidate_key"] == key)


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


MODEL = "## Observations\n\n- [finding] One steady bath runs every dye stage. ^steady-bath\n"

ANTECEDENTS = (
    ("batch-dyeing-cadence", "Dye batches run on a fixed weekly cadence."),
    ("mordant-preparation", "The alum mordant soaks for thirty minutes."),
    ("water-hardness-control", "Soft water keeps the bath colour even."),
    ("vat-temperature-logging", "Vat temperature is logged at every dip."),
    ("fibre-scouring-order", "Fibre is scoured before it is mordanted."),
)


def _antecedents(vault: Path) -> list[str]:
    return [
        _write_note(vault, slug, f"0000000{index}-7d7d-4d7d-8d7d-7d7d7d7d7d7d", body)
        for index, (slug, body) in enumerate(ANTECEDENTS, start=1)
    ]


# --------------------------------------------------------------------------- #
# 3.8 — bounded destination decisions with alternative scope/version evidence
# --------------------------------------------------------------------------- #


def test_inspected_alternatives_bind_the_versions_the_agent_read(vault: Path, owner) -> None:
    _record(vault)
    pages = _antecedents(vault)
    alternatives = [_alternative(vault, path, "One narrower workshop practice.") for path in pages]

    prepared = _episode(
        vault,
        action="prepare",
        candidate="steady-bath-thesis",
        proposal=_proposal(
            "focused_note",
            [_note_leaf("steady-bath-model", "Steady-bath model", MODEL)],
            title="Steady-bath model",
            alternatives=alternatives,
        ),
    )

    candidate = _candidate(prepared, "steady-bath-thesis")
    assert candidate["route"] == "focused_note" and candidate["leaves"][0]["bound"] is True
    # The decision is the agent's and stays in its journal: the projection
    # returns neither the alternatives' scope text nor any server judgment.
    encoded = json.dumps(prepared)
    assert "narrower workshop practice" not in encoded
    for word in ("score", "rank", "confidence", "fitness", "similarity"):
        assert word not in encoded


@pytest.mark.parametrize("bad", ["not-a-ref", "Knowledge Base/Notes/Insights/x.md"])
def test_an_alternative_names_a_page_by_its_memory_ref(vault: Path, owner, bad: str) -> None:
    _record(vault)
    before = _journal(vault)
    with pytest.raises(ValueError, match="EPISODE_PROPOSAL_INVALID"):
        _episode(
            vault,
            action="prepare",
            candidate="thesis",
            proposal=_proposal(
                "focused_note",
                [_note_leaf("steady-bath-model", "Steady-bath model", MODEL)],
                title="Steady-bath model",
                alternatives=[{"target": bad, "scope": "s", "version": "0" * 64}],
            ),
        )
    assert _journal(vault) == before


def test_a_stale_alternative_version_needs_a_fresh_read(vault: Path, owner) -> None:
    _record(vault)
    (page, *_rest) = _antecedents(vault)
    stale = {**_alternative(vault, page, "A narrower practice."), "version": "0" * 64}
    before = _journal(vault)

    with pytest.raises(ValueError, match="EPISODE_DESTINATION_STALE"):
        _episode(
            vault,
            action="prepare",
            candidate="thesis",
            proposal=_proposal(
                "focused_note",
                [_note_leaf("steady-bath-model", "Steady-bath model", MODEL)],
                title="Steady-bath model",
                alternatives=[stale],
            ),
        )
    assert _journal(vault) == before


def test_a_withheld_alternative_is_refused_exactly_like_an_unknown_one(vault: Path) -> None:
    visible = _write_note(vault, "visible-practice", "11111111-7d7d-4d7d-8d7d-7d7d7d7d7d7d", "V.")
    withheld = _write_note(vault, "withheld-practice", "22222222-7d7d-4d7d-8d7d-7d7d7d7d7d7d", "W.")
    unknown = memory_refs.memory_ref("33333333-7d7d-4d7d-8d7d-7d7d7d7d7d7d")
    _withhold_notes_from(vault, "client-a")
    errors: list[str] = []
    with request_scope(RequestPrincipal(audience_id="client-a", surface="mcp")):
        _record(vault)
        accepted = _episode(
            vault,
            action="prepare",
            candidate="thesis",
            proposal=_proposal(
                "focused_note",
                [_note_leaf("steady-bath-model", "Steady-bath model", MODEL)],
                title="Steady-bath model",
                alternatives=[_alternative(vault, visible, "Visible.")],
            ),
        )
        assert _candidate(accepted, "thesis")["route"] == "focused_note"
        for ref in (_ref(vault, withheld), unknown):
            with pytest.raises(ValueError) as refused:
                _episode(
                    vault,
                    action="prepare",
                    candidate="other",
                    proposal=_proposal(
                        "focused_note",
                        [_note_leaf("other-model", "Other model", MODEL)],
                        title="Other model",
                        alternatives=[
                            {"target": ref, "scope": "Hidden.", "version": _version(vault, withheld)}
                        ],
                    ),
                )
            errors.append(str(refused.value))
    assert errors[0] == errors[1]
    assert "EPISODE_DESTINATION_UNAVAILABLE" in errors[0]


def test_an_existing_page_home_is_the_page_its_leaves_write(vault: Path, owner) -> None:
    _record(vault)
    first, second, *_rest = _antecedents(vault)
    before = _journal(vault)

    # A detail aimed at one page cannot quietly write another.
    with pytest.raises(ValueError, match="EPISODE_DESTINATION_MISMATCH"):
        _episode(
            vault,
            action="prepare",
            candidate="detail",
            proposal=_proposal(
                "existing_page",
                [_append_leaf(vault, second, "- [finding] Copied onto a neighbour. ^copied")],
                target=_ref(vault, first),
            ),
        )
    # No home is ever inferred from the conversation's open page.
    with pytest.raises(ValueError, match="EPISODE_PROPOSAL_INVALID"):
        _episode(
            vault,
            action="prepare",
            candidate="detail",
            proposal=_proposal(
                "semantic_unit",
                [_append_leaf(vault, first, "- [finding] No target named. ^untargeted")],
                title="Batch dyeing cadence",
            ),
        )
    assert _journal(vault) == before

    # Named by memory ref or by path, the home binds the page the leaf guards.
    for candidate, target in (("by-ref", _ref(vault, first)), ("by-path", second)):
        page = first if candidate == "by-ref" else second
        prepared = _episode(
            vault,
            action="prepare",
            candidate=candidate,
            proposal=_proposal(
                "existing_page",
                [_append_leaf(vault, page, f"- [finding] A {candidate} detail. ^{candidate}")],
                target=target,
            ),
        )
        assert _candidate(prepared, candidate)["leaves"][0]["bound"] is True


def test_a_withheld_home_is_refused_exactly_like_an_unknown_one(vault: Path) -> None:
    withheld = _write_note(vault, "withheld-practice", "22222222-7d7d-4d7d-8d7d-7d7d7d7d7d7d", "W.")
    hidden_ref = _ref(vault, withheld)
    hidden_version = _version(vault, withheld)
    _withhold_notes_from(vault, "client-a")
    unknown_path = f"{INSIGHTS}/withheld-missing.md"
    errors: list[str] = []
    with request_scope(RequestPrincipal(audience_id="client-a", surface="mcp")):
        _record(vault)
        for target, path in (
            (hidden_ref, withheld),
            (memory_refs.memory_ref("44444444-7d7d-4d7d-8d7d-7d7d7d7d7d7d"), withheld),
            (withheld, withheld),
            (unknown_path, unknown_path),
        ):
            leaf = {
                "leaf_key": "append",
                "effect_revision": 1,
                "kind": "edit",
                "args": {
                    "path": path,
                    "why": "A detail.",
                    "operation": {
                        "kind": "edit_section",
                        "heading": "Observations",
                        "new_string": "- [finding] Hidden. ^hidden",
                        "section_position": "append",
                        "expected_hash": hidden_version,
                    },
                },
            }
            with pytest.raises(ValueError) as refused:
                _episode(
                    vault,
                    action="prepare",
                    candidate="detail",
                    proposal=_proposal("existing_page", [leaf], target=target),
                )
            errors.append(str(refused.value))
    assert len(set(errors)) == 1, errors
    assert "EPISODE_DESTINATION_UNAVAILABLE" in errors[0]


def test_destination_judgments_stay_with_the_agent(vault: Path, owner, enabled) -> None:
    """No server score, extra confirmation or second executor; no open-note privilege."""
    pages = _antecedents(vault)
    open_note = pages[0]
    # The conversation concerned `open_note`: it is the page the session has open.
    _record(vault, about=[_ref(vault, open_note)])
    alternatives = [_alternative(vault, path, "A narrower practice.") for path in pages]

    # A focused home is accepted although the open note is a close alternative,
    # and a minor refinement stays on its page although it coins a new label:
    # both are the agent's calls, recorded as made.
    _episode(
        vault,
        action="prepare",
        candidate="thesis",
        proposal=_proposal(
            "focused_note",
            [_note_leaf("steady-bath-model", "Steady-bath model", MODEL)],
            title="Steady-bath model",
            alternatives=alternatives,
        ),
    )
    _episode(
        vault,
        action="prepare",
        candidate="refinement",
        proposal=_proposal(
            "semantic_unit",
            [_append_leaf(vault, pages[1], "- [finding] Call it the long soak. ^long-soak")],
            target=_ref(vault, pages[1]),
            alternatives=[_alternative(vault, open_note, "The open page.")],
        ),
    )
    for key in ("thesis", "refinement"):
        _episode(vault, action="disposition", candidate=key, disposition="routed", reason="r")
    inspected = _episode(vault, action="candidates")
    assert {c["candidate_key"]: c["route"] for c in inspected["candidates"]} == {
        "thesis": "focused_note",
        "refinement": "semantic_unit",
    }

    # The published proposal schema carries decisions and evidence, no score.
    schema = get_args(commands._EpisodeProposalArgument)[1].json_schema  # noqa: SLF001
    assert set(schema["anyOf"][0]["properties"]) == {
        "route",
        "target",
        "title",
        "alternatives",
        "evidence",
        "reason",
        "leaves",
    }
    # Resume runs on the reviewed journal alone: no confirmation argument.
    resumed = _episode(
        vault, action="resume", input_revision=1, journal_digest=inspected["journal_digest"]
    )
    assert resumed["status"] == "ok" and len(resumed["executed"]) == 2, resumed["blocked"]
    # Every effect ran through the existing curation executor's receipts.
    store = curation.CurationStore(vault)
    for candidate in resumed["candidates"]:
        run = candidate["leaves"][0]["run_id"]
        assert store.reconstruct(run)["committed_steps"]


def test_decision_evidence_that_changes_before_commit_needs_fresh_consideration(
    vault: Path, owner, enabled
) -> None:
    _record(vault)
    pages = _antecedents(vault)
    proposal = _proposal(
        "focused_note",
        [_note_leaf("steady-bath-model", "Steady-bath model", MODEL)],
        title="Steady-bath model",
        alternatives=[_alternative(vault, path, "A narrower practice.") for path in pages],
    )
    prepared = _episode(vault, action="prepare", candidate="thesis", proposal=proposal)
    leaf = _candidate(prepared, "thesis")["leaves"][0]
    disposed = _episode(
        vault, action="disposition", candidate="thesis", disposition="routed", reason="r"
    )
    files = _canonical_files(vault)

    # An inspected alternative changes after the decision was made: the
    # decision was made on evidence that no longer holds.
    changed = vault / pages[2]
    changed.write_text(
        changed.read_text(encoding="utf-8") + "- [finding] It now owns a thesis. ^owns\n",
        encoding="utf-8",
    )
    files[pages[2]] = changed.read_bytes()
    stale = _episode(
        vault, action="resume", input_revision=1, journal_digest=disposed["journal_digest"]
    )

    assert stale["status"] == "stale" and stale["executed"] == []
    assert [item["code"] for item in stale["stale"]] == ["EPISODE_DESTINATION_STALE"]
    after = _candidate(stale, "thesis")["leaves"][0]
    assert after["outcome"] == "pending" and after["attempts"] == 0
    assert _canonical_files(vault) == files

    # A fresh read revises the proposal: same candidate and leaf identity, a
    # new proposal revision, and a disposition that must be made again.
    fresh = _proposal(
        "focused_note",
        proposal["leaves"],
        title="Steady-bath model",
        alternatives=[_alternative(vault, path, "A narrower practice.") for path in pages],
    )
    revised = _candidate(
        _episode(vault, action="prepare", candidate="thesis", proposal=fresh), "thesis"
    )
    assert revised["proposal_revision"] == 2 and revised["disposition"] is None
    assert revised["leaves"][0]["leaf_id"] == leaf["leaf_id"]
    redisposed = _episode(
        vault, action="disposition", candidate="thesis", disposition="routed", reason="r"
    )
    resumed = _episode(
        vault, action="resume", input_revision=1, journal_digest=redisposed["journal_digest"]
    )
    assert resumed["status"] == "ok"
    assert [item["path"] for item in resumed["executed"]] == [f"{INSIGHTS}/steady-bath-model.md"]


# --------------------------------------------------------------------------- #
# 3.9 — precommit destination/coverage review and postcommit reconciliation,
# wired through the 3.4-3.6 slice. Scripted writer checks: the script plays the
# agent, so these prove the plumbing, not ordinary-agent initiation.
# --------------------------------------------------------------------------- #

THESIS = f"{INSIGHTS}/steady-bath-model.md"


def _thesis_leaf(pages: list[str], *, key: str = "home", revision: int = 1) -> dict:
    relations = "\n".join(f"- derived_from [[{path.removesuffix('.md')}]]" for path in pages)
    return {
        "leaf_key": key,
        "effect_revision": revision,
        "kind": "create-note",
        "args": {
            "title": "Steady-bath operating model",
            "slug": "steady-bath-model",
            "content": (
                "## Observations\n\n- [finding] Cadence, mordant, water, temperature and"
                " scouring are one steady-bath operating model. ^steady-bath-model\n\n"
                f"## Relations\n\n{relations}\n"
            ),
        },
    }


def _fresh_session_index(vault: Path) -> None:
    from exomem import find, lexstore, working_set_index, working_set_runtime

    find.clear_cache()
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()


def test_a_named_synthesis_gets_its_own_home_on_the_first_pass(vault: Path, enabled) -> None:
    with request_scope(owner_principal(surface="mcp")):
        pages = _antecedents(vault)
        _record(vault)
        alternatives = [
            _alternative(vault, path, f"Only {slug.replace('-', ' ')}.")
            for path, (slug, _body) in zip(pages, ANTECEDENTS, strict=True)
        ]
        temperature = pages[3]

        # First draft: every fact present, but the thesis appended to the
        # nearest page, whose narrower title does not own it.
        nearest = _proposal(
            "existing_page",
            [
                {
                    **_append_leaf(
                        vault,
                        pages[0],
                        "- [finding] All five practices are one operating model. ^model",
                    ),
                    "leaf_key": "home",
                }
            ],
            target=_ref(vault, pages[0]),
            alternatives=alternatives,
        )
        drafted = _episode(vault, action="prepare", candidate="thesis", proposal=nearest)
        leaf_id = _candidate(drafted, "thesis")["leaves"][0]["leaf_id"]
        _episode(vault, action="disposition", candidate="thesis", disposition="routed", reason="r")
        # Leaf validation passed; that is neither destination fitness nor coverage.
        assert drafted["complete"] is False and drafted["covered_through_input_revision"] is None

        # The precommit review revises the home before any canonical write:
        # same candidate, same leaf identity, next effect revision.
        focused = _proposal(
            "focused_note",
            [_thesis_leaf(pages, revision=2)],
            title="Steady-bath operating model",
            alternatives=alternatives,
            reason="A named thesis none of the five narrower titles owns.",
        )
        revised = _candidate(
            _episode(vault, action="prepare", candidate="thesis", proposal=focused), "thesis"
        )
        assert revised["proposal_revision"] == 2 and revised["disposition"] is None
        assert revised["leaves"][0]["leaf_id"] == leaf_id
        assert revised["leaves"][0]["effect_revision"] == 2

        # A narrower page receives only the update its own scope owns.
        _episode(
            vault,
            action="prepare",
            candidate="dip-log",
            proposal=_proposal(
                "existing_page",
                [
                    _append_leaf(
                        vault,
                        temperature,
                        "- [finding] The dip log keeps bath temperature before and after. ^dip-log",
                    )
                ],
                target=_ref(vault, temperature),
                reason="A logging detail inside this page's own scope.",
            ),
        )
        _episode(
            vault,
            action="prepare",
            candidate="kiln-mention",
            proposal=_proposal("no_capture", [], title="The kiln", reason="Incidental mention."),
        )
        for key, value in (
            ("thesis", "routed"),
            ("dip-log", "routed"),
            ("kiln-mention", "no_capture"),
        ):
            reviewed = _episode(
                vault, action="disposition", candidate=key, disposition=value, reason="Reviewed."
            )
        before = _canonical_files(vault)

        resumed = _episode(
            vault, action="resume", input_revision=1, journal_digest=reviewed["journal_digest"]
        )
        assert resumed["status"] == "ok", (resumed["blocked"], resumed["stale"])
        assert sorted(item["path"] for item in resumed["executed"]) == sorted([THESIS, temperature])
        after = _canonical_files(vault)

        # First pass: the thesis has its own home, and it is the only new page.
        assert set(after) - set(before) == {THESIS}
        changed = {path for path in before if before[path] != after[path]}
        assert changed <= {temperature, "Knowledge Base/index.md", "Knowledge Base/log.md"}
        assert temperature in changed
        thesis = after[THESIS].decode()
        for path in pages:
            assert f"- derived_from [[{path.removesuffix('.md')}]]" in thesis
        assert "operating model" not in after[temperature].decode()
        assert "dip log" in after[temperature].decode()

        # Postcommit reconciliation: a separate attestation that reverifies
        # every committed receipt against its live page.
        attested = _episode(
            vault,
            action="resume",
            input_revision=1,
            postcommit=True,
            journal_digest=resumed["journal_digest"],
        )
        assert attested["executed"] == [] and attested["complete"] is True
        assert attested["covered_through_input_revision"] == 1

    # Publication reaches a fresh session: the recap leads its recent context,
    # and ordinary recall finds the new home rather than a narrower page.
    _fresh_session_index(vault)
    with request_scope(owner_principal(surface="mcp")):
        packet = commands.op_activate_context(
            vault, turn="What is the steady-bath operating model for the dye workshop?"
        )
        recalled = commands.op_ask_memory(vault, query="steady-bath operating model")
    recent = packet["recent_context"]
    entries = recent["entries"] if isinstance(recent, dict) else recent
    assert any(entry.get("why") == "episode" and entry.get("episode") == KEY for entry in entries)
    assert THESIS in json.dumps(recalled, default=str)


def test_a_minor_refinement_stays_on_its_existing_page(vault: Path, owner, enabled) -> None:
    pages = _antecedents(vault)
    mordant = pages[1]
    key = "ep-" + "8e" * 16
    _record(
        vault,
        key=key,
        subject="Mordant soak timing",
        summary="The alum soak runs forty minutes.",
        worked_on=["Timed the alum mordant soak"],
        decided=["Soak alum for forty minutes"],
        open=None,
    )
    _episode(
        vault,
        key=key,
        action="prepare",
        candidate="soak-timing",
        proposal=_proposal(
            "semantic_unit",
            [
                _append_leaf(
                    vault, mordant, "- [finding] The long soak runs forty minutes. ^long-soak"
                )
            ],
            target=_ref(vault, mordant),
            alternatives=[_alternative(vault, mordant, "Mordant preparation practice.")],
            reason="Refines the existing mordant practice; no independent future question.",
        ),
    )
    reviewed = _episode(
        vault,
        key=key,
        action="disposition",
        candidate="soak-timing",
        disposition="routed",
        reason="Reviewed.",
    )
    before = _canonical_files(vault)

    resumed = _episode(
        vault,
        key=key,
        action="resume",
        input_revision=1,
        journal_digest=reviewed["journal_digest"],
    )

    assert resumed["status"] == "ok", resumed["stale"]
    after = _canonical_files(vault)
    # Not fragmented: no new page, and only the refined page changed.
    assert set(after) == set(before)
    assert {path for path in before if before[path] != after[path]} <= {
        mordant,
        "Knowledge Base/index.md",
        "Knowledge Base/log.md",
    }
    assert "forty minutes" in after[mordant].decode()
    attested = _episode(
        vault,
        key=key,
        action="resume",
        input_revision=1,
        postcommit=True,
        journal_digest=resumed["journal_digest"],
    )
    assert attested["complete"] is True


def test_the_capture_guidance_carries_the_precommit_review_and_final_pass() -> None:
    """Wired into 3.4: the scaffold and its lifecycle-capable adapter copy."""
    root = Path(__file__).resolve().parents[1]
    for engagement in (
        root / "src/exomem/_scaffold/_Schema/references/engagement.md",
        root / "plugins/claude-code/skills/exomem/references/engagement.md",
    ):
        text = engagement.read_text(encoding="utf-8")
        section = text[text.index("**Episode candidates") :]
        section = section[: section.index("\n\n")]
        for phrase in (
            "execution: enabled",
            "content_hash",
            "before `resume`",
            'action="coverage"',
            "postcommit=true",
            "never claims",
        ):
            assert phrase in section, (engagement, phrase)
