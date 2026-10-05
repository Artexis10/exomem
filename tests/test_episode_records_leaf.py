"""The episode Records leaf (close-memory-loop 3.5, by ruling with 3.8/3.9).

A `records` candidate carries `append-record` or `update-record` steps: curation is
the executor, the existing Records writer makes the transition, and the curation
witness is committed in that writer's own atomic batch and holds only when the
Records receipt -- the audit transition the writer committed -- corroborates
it. Each authority boundary is pinned here on its own:

* only the `records` route owns these kinds, and it owns no other kind;
* no general curation plan (the `maintain_memory` door) can seal it, though
  curation apply runs one the episode sealed through the same writer;
* every other step kind still cannot reach the protected Records tree, and a
  Records witness cannot name a page outside it;
* the Records writer's own resolution, visibility and profile checks decide,
  and an invisible collection is refused exactly like a missing one;
* a refused value is not parked as a held candidate;
* a Records transition has no compensation.
"""

from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path
from typing import get_args

import pytest

from exomem import commands, curation, episode_workflow
from exomem import episode_model as model
from exomem import schema as schema_module
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

KEY = "ep-" + "4d" * 16
COLLECTION = "Knowledge Base/Records/Vat Readings/_collection.md"
MANIFEST = """---
type: collection
exomem_id: 6b1f2e3d-4c5a-4b6c-8d7e-9f0a1b2c3d4e
title: Vat readings
semantic_profile: {profile}
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Entries
  format_version: 1
item_schema:
  natural_key: [read_on, vat]
  fields:
    read_on:
      type: date
      required: true
    vat:
      type: string
      required: true
    temperature_c:
      type: integer
      required: true
---

One reading per vat and day.
"""
READING = {"read_on": "2026-09-20", "vat": "north", "temperature_c": 41}


@pytest.fixture
def owner():
    with request_scope(owner_principal(surface="mcp")):
        yield


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")


def _collection(
    vault: Path,
    path: str = COLLECTION,
    *,
    profile: str = "records",
    exomem_id: str = "6b1f2e3d-4c5a-4b6c-8d7e-9f0a1b2c3d4e",
) -> str:
    manifest = vault / path
    manifest.parent.mkdir(parents=True, exist_ok=True)
    (manifest.parent / "Entries").mkdir(exist_ok=True)
    manifest.write_text(
        MANIFEST.replace("{profile}", profile).replace(
            "6b1f2e3d-4c5a-4b6c-8d7e-9f0a1b2c3d4e", exomem_id
        ),
        encoding="utf-8",
    )
    return path


def _container(vault: Path, path: str = COLLECTION) -> str:
    inspected = commands.op_record_memory(vault, action="inspect", collection=path)
    return inspected["lifecycle_guards"]["expected_container_hash"]


def _episode(vault: Path, **kwargs: object) -> dict:
    return commands.op_episode_memory(
        vault, schema_module.load_source_schema(vault), episode=KEY, **kwargs
    )


def _record(vault: Path) -> dict:
    return _episode(
        vault,
        action="record",
        subject="Dye vat reading",
        summary="Logged the north vat at forty-one degrees.",
        worked_on=["Read the north vat temperature"],
        decided=["Log the reading in Vat readings"],
    )


def _leaf(vault: Path, item: dict | None = None, **overrides: object) -> dict:
    args: dict[str, object] = {
        "collection": COLLECTION,
        "item": dict(READING if item is None else item),
        "why": "The episode logged a vat reading.",
        "expected_container_hash": _container(vault),
    }
    args.update(overrides)
    return {"leaf_key": "append", "effect_revision": 1, "kind": "append-record", "args": args}


def _proposal(route: str, leaves: list[dict], **fields: object) -> dict:
    return {
        "route": route,
        "alternatives": [],
        "evidence": "complete",
        "reason": "A Records event the episode observed.",
        "leaves": leaves,
        "title": "Vat reading",
        **fields,
    }


def _prepared(vault: Path, leaf: dict | None = None) -> dict:
    _episode(
        vault,
        action="prepare",
        candidate="reading",
        proposal=_proposal("records", [leaf or _leaf(vault)]),
    )
    return _episode(
        vault, action="disposition", candidate="reading", disposition="routed", reason="Observed."
    )


def _resume(vault: Path, reviewed: dict, **kwargs: object) -> dict:
    return _episode(
        vault,
        action="resume",
        input_revision=reviewed["input_revision"],
        journal_digest=reviewed["journal_digest"],
        **kwargs,
    )


def _entries(vault: Path, path: str = COLLECTION) -> list[Path]:
    return sorted((vault / path).parent.joinpath("Entries").glob("*.md"))


def _correction(
    vault: Path, temperature: int, *, key: str = "correct", collection: str = COLLECTION
) -> dict:
    from exomem.vault import parse_frontmatter

    (entry,) = _entries(vault, collection)
    frontmatter, _body, _raw = parse_frontmatter(entry.read_text(encoding="utf-8"))
    return {
        "leaf_key": key,
        "effect_revision": 1,
        "kind": "update-record",
        "args": {
            "collection": collection,
            "item_key": frontmatter["record_id"],
            "changes": {"temperature_c": temperature},
            "expected_container_hash": _container(vault, collection),
            "expected_item_version": hashlib.sha256(entry.read_bytes()).hexdigest(),
            "why": "Correct the interpretation of the reading, preserving the event.",
        },
    }


def test_two_corrections_preserve_history_and_old_replay_keeps_latest_value(
    vault: Path, owner, enabled
) -> None:
    _collection(vault)
    _record(vault)
    append = _leaf(vault)
    original = _resume(vault, _prepared(vault, append))
    assert original["status"] == "ok", original["blocked"]
    retained = {
        path: path.read_bytes()
        for layer in ("Sources", "Evidence")
        for path in (vault / "Knowledge Base" / layer).rglob("*.md")
    }
    leaves = [append]
    for temperature, key in ((14, "first-correction"), (15, "second-correction")):
        leaves.append(_correction(vault, temperature, key=key))
        canonical = {
            path: path.read_bytes()
            for path in (*_entries(vault), vault / COLLECTION, vault / "Knowledge Base/log.md")
        }
        _episode(vault, action="prepare", candidate="reading", proposal=_proposal("records", leaves))
        assert {path: path.read_bytes() for path in canonical} == canonical
        reviewed = _episode(
            vault, action="disposition", candidate="reading", disposition="routed", reason="Corrected."
        )
        executed = _resume(vault, reviewed)
        assert executed["status"] == "ok", (executed["blocked"], executed["stale"])
        assert len(executed["executed"]) == 1
    passed = _episode(vault, action="coverage")
    assert [item["readback"] for item in passed["receipts"]] == ["verified"] * 3
    assert _resume(vault, passed, postcommit=True)["complete"] is True
    with pytest.raises(ValueError, match="EPISODE_REVISION_CONFLICT"):
        _resume(vault, original)
    replayed = _resume(
        vault, _episode(vault, action="candidates"),
        order=[original["candidates"][0]["leaves"][0]["leaf_id"]],
    )
    assert replayed["executed"] == []
    assert "temperature_c: 15" in _entries(vault)[0].read_text(encoding="utf-8")
    assert {path: path.read_bytes() for path in retained} == retained
    history = (vault / "Knowledge Base/log.md").read_text(encoding="utf-8")
    assert history.count('"operation":"append"') == 1
    assert history.count('"operation":"update"') == 2


def _plan(kind: str, args: dict) -> dict:
    return {"version": 1, "title": "t", "steps": [{"step_id": "s", "kind": kind, "args": args}]}


# --------------------------------------------------------------------------- #
# Ownership: the records route alone, and only through the episode seal
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", ["append-record", "update-record"])
def test_only_the_records_route_owns_the_records_leaf(vault: Path, owner, kind: str) -> None:
    _collection(vault)
    state = model.start_episode(KEY, {"excerpt": "A vat reading."})
    state = model.declare_candidate(state, "reading")
    identity = state["candidates"][0]["candidate_id"]
    leaf = _leaf(vault)
    if kind == "update-record":
        commands.op_record_memory(vault, action="append", **leaf["args"])
        leaf = _correction(vault, 14)

    accepted = model.revise_proposal(state, identity, _proposal("records", [leaf]))
    assert accepted["candidates"][0]["leaves"][0]["kind"] == kind
    for route in ("focused_note", "entity", "relation_only", "existing_page", "semantic_unit"):
        with pytest.raises(model.EpisodeError, match="route does not admit"):
            model.revise_proposal(state, identity, _proposal(route, [leaf], target="x"))
    note = {
        "leaf_key": "note",
        "effect_revision": 1,
        "kind": "create-note",
        "args": {"title": "Reading", "content": "A reading."},
    }
    with pytest.raises(model.EpisodeError, match="route does not admit"):
        model.revise_proposal(state, identity, _proposal("records", [note]))
    # A records event the agent routes without a leaf stays pending, as before.
    pending = model.revise_proposal(state, identity, _proposal("records", []))
    assert pending["candidates"][0]["leaves"] == []

    schema = get_args(commands._EpisodeProposalArgument)[1].json_schema  # noqa: SLF001
    kinds = schema["anyOf"][0]["properties"]["leaves"]["items"]["properties"]["kind"]["enum"]
    assert kind in kinds


@pytest.mark.parametrize("kind", ["append-record", "update-record"])
def test_the_curation_door_cannot_seal_a_records_plan_but_runs_one_the_episode_sealed(
    vault: Path, owner, kind: str
) -> None:
    """The episode seal is the only way a Records plan comes to exist. Once
    sealed, it is an ordinary curation run: `maintain_memory` curation apply
    runs it through the same Records writer and checks `record_memory` append
    applies, which grants nothing that append does not (ruling on L1)."""
    _collection(vault)
    leaf = _leaf(vault)
    if kind == "update-record":
        commands.op_record_memory(vault, action="append", **leaf["args"])
        leaf = _correction(vault, 14)
    args = leaf["args"]
    with pytest.raises(curation.CurationError) as refused:
        curation.propose(vault, _plan(kind, args))
    assert refused.value.code == "INVALID_STEP_KIND"
    with pytest.raises(ValueError, match="INVALID_STEP_KIND"):
        commands.op_maintain_memory(
            vault, mode="curation", curation_action="propose", plan=_plan(kind, args)
        )
    assert len(_entries(vault)) == (1 if kind == "update-record" else 0)
    # The one entry point that may seal it seals one step, nothing beside it.
    two = _plan(kind, args)
    two["steps"].append(
        {"step_id": "t", "kind": "create-note", "args": {"title": "Reading", "content": "R."}}
    )
    with pytest.raises(curation.CurationError, match="CURATION_RECORDS_LEAF_ALONE"):
        curation.propose(vault, two, allow_records=True)

    # An episode-sealed Records plan, applied through the curation door.
    _record(vault)
    reviewed = _prepared(vault, leaf)
    run = reviewed["candidates"][0]["leaves"][0]["run_id"]
    plan_id, fingerprint = curation.CurationStore(vault).identities(run)
    applied = commands.op_maintain_memory(
        vault,
        mode="curation",
        curation_action="apply",
        run_id=run,
        plan_id=plan_id,
        expected_plan_fingerprint=fingerprint,
        why="Applied through the curation door.",
    )
    assert applied["step"]["outcome"] == "committed"
    assert len(_entries(vault)) == 1
    inspected = commands.op_record_memory(vault, action="inspect", collection=COLLECTION)
    assert inspected["audit"]["status"] == "ok"

    with pytest.raises(curation.CurationError, match="CURATION_COMPENSATION_UNAVAILABLE"):
        curation.propose_compensation(vault, run_id=run)
    assert curation.compensation_kind(kind) == "unavailable"


def test_every_other_kind_stays_out_of_the_records_tree(vault: Path, owner) -> None:
    item = "Knowledge Base/Records/Vat Readings/Entries/reading.md"
    for kind, args in (
        (
            "edit",
            {
                "path": item,
                "why": "Rewrite a reading.",
                "operation": {
                    "kind": "replace_body",
                    "new_body": "Changed.",
                    "expected_hash": "0" * 64,
                },
            },
        ),
        ("supersede", {"old_path": item, "title": "Reading", "content": "Changed."}),
    ):
        for allow in (False, True):
            with pytest.raises(curation.CurationError) as refused:
                curation.validate_forward_plan(_plan(kind, args), allow_records=allow)
            assert refused.value.code == "CURATION_TARGET_PROTECTED"

    # A Records witness may name only a page inside the Records tree.
    assert curation.normalize_target_path(item, allow_records=True) == item
    for outside in ("Knowledge Base/Notes/Insights/x.md", "Knowledge Base/Sources/x.md"):
        with pytest.raises(curation.CurationError) as refused:
            curation.normalize_target_path(outside, allow_records=True)
        assert refused.value.code == "CURATION_TARGET_PROTECTED"


# --------------------------------------------------------------------------- #
# Execution through the Records writer, witnessed by its receipt
# --------------------------------------------------------------------------- #


def test_a_records_event_runs_through_the_records_writer(vault: Path, owner, enabled) -> None:
    _collection(vault)
    _record(vault)
    reviewed = _prepared(vault)
    leaf = reviewed["candidates"][0]["leaves"][0]
    assert leaf["kind"] == "append-record" and leaf["bound"] is True
    assert _entries(vault) == []  # preparation wrote nothing

    resumed = _resume(vault, reviewed)

    assert resumed["status"] == "ok", (resumed["blocked"], resumed["stale"])
    (executed,) = resumed["executed"]
    (entry,) = _entries(vault)
    assert executed["path"] == entry.relative_to(vault).as_posix()
    assert "temperature_c: 41" in entry.read_text(encoding="utf-8")
    # The Records writer's own receipt and audit transition exist...
    inspected = commands.op_record_memory(vault, action="inspect", collection=COLLECTION)
    assert inspected["audit"]["status"] == "ok"
    assert inspected["coverage"]["committed"] == 1
    # ...and curation, the one executor, holds the committed receipt.
    store = curation.CurationStore(vault)
    assert store.reconstruct(leaf["run_id"])["committed_steps"] == ["leaf"]

    passed = _episode(vault, action="coverage")
    assert [item["readback"] for item in passed["receipts"]] == ["verified"]
    attested = _resume(vault, passed, postcommit=True)
    assert attested["complete"] is True


def test_the_records_receipt_is_the_witness(vault: Path, owner, enabled) -> None:
    _collection(vault)
    _record(vault)
    executed = _resume(vault, _prepared(vault))
    assert executed["status"] == "ok", executed["blocked"]

    # The item file is untouched, but the audit transition that committed it
    # is gone: without the Records receipt the witness no longer holds.
    log = vault / "Knowledge Base/log.md"
    kept = [line for line in log.read_text(encoding="utf-8").splitlines() if '"operation":"append"' not in line]
    log.write_text("\n".join(kept) + "\n", encoding="utf-8")

    passed = _episode(vault, action="coverage")
    assert [item["readback"] for item in passed["receipts"]] == ["changed"]
    with pytest.raises(ValueError, match="EPISODE_OUTCOME_UNCERTAIN"):
        _resume(vault, executed, postcommit=True)


@pytest.mark.parametrize("kind", ["append-record", "update-record"])
def test_a_changed_collection_is_stale_before_any_attempt(
    vault: Path, owner, enabled, kind: str
) -> None:
    _collection(vault)
    _record(vault)
    leaf = _leaf(vault)
    if kind == "update-record":
        commands.op_record_memory(vault, action="append", **leaf["args"])
        leaf = _correction(vault, 14)
    reviewed = _prepared(vault, leaf)
    commands.op_record_memory(
        vault,
        action="append",
        collection=COLLECTION,
        item={"read_on": "2026-09-21", "vat": "south", "temperature_c": 39},
        expected_container_hash=_container(vault),
        why="Out-of-band reading.",
    )

    stale = _resume(vault, reviewed)

    assert stale["status"] == "stale" and stale["executed"] == []
    assert [item["code"] for item in stale["stale"]] == ["CURATION_BINDING_STALE"]
    leaf = stale["candidates"][0]["leaves"][0]
    assert leaf["outcome"] == "pending" and leaf["attempts"] == 0
    assert len(_entries(vault)) == (2 if kind == "update-record" else 1)


@pytest.mark.parametrize("fault", ["invalid-value", "stale-container", "stale-item"])
def test_refused_correction_preparation_writes_nothing(vault: Path, owner, fault: str) -> None:
    _collection(vault)
    commands.op_record_memory(vault, action="append", **_leaf(vault)["args"])
    _record(vault)
    leaf = _correction(vault, 14)
    field, value = {
        "invalid-value": ("changes", {"temperature_c": "warm"}),
        "stale-container": ("expected_container_hash", "0" * 64),
        "stale-item": ("expected_item_version", "0" * 64),
    }[fault]
    leaf["args"][field] = value
    before = {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    reviewed = _episode(vault, action="candidates")
    with pytest.raises(ValueError):
        _prepared(vault, leaf)
    assert {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()} == before
    assert _episode(vault, action="candidates")["journal_digest"] == reviewed["journal_digest"]
    assert commands.op_record_memory(vault, action="inspect", collection=COLLECTION)["coverage"]["held"] == 0


def test_correction_exposes_no_internal_writer_options(vault: Path, owner) -> None:
    _collection(vault)
    commands.op_record_memory(vault, action="append", **_leaf(vault)["args"])
    leaf = _correction(vault, 14)
    for field in ("operation", "body", "delete_fields", "hold", "held", "refresh_presentation"):
        with pytest.raises(curation.CurationError, match="CURATION_UNKNOWN_FIELD"):
            curation.validate_forward_plan(
                _plan("update-record", {**leaf["args"], field: True}), allow_records=True
            )


def test_correction_refuses_a_log_collection(vault: Path, owner) -> None:
    from record_fixtures import copy_x3_fixture

    collection = (copy_x3_fixture(vault) / "_collection.md").relative_to(vault).as_posix()
    appended = commands.op_record_memory(
        vault,
        action="append",
        collection=collection,
        item={"occurred_on": "2026-09-20", "title": "Pull", "status": "completed"},
        expected_container_hash=_container(vault, collection),
        why="Record a session.",
    )
    _record(vault)
    leaf = {
        "leaf_key": "correct",
        "effect_revision": 1,
        "kind": "update-record",
        "args": {
            "collection": collection,
            "item_key": appended["item_key"],
            "changes": {"title": "Push"},
            "expected_container_hash": appended["after_container_hash"],
            "expected_item_version": appended["after_item_hash"],
            "why": "Correct the session title.",
        },
    }
    before = {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="CURATION_RECORDS_STORAGE_UNSUPPORTED"):
        _prepared(vault, leaf)
    assert {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("proof", ["valid", "missing", "wrong-before-item"])
def test_interrupted_correction_requires_its_exact_audit_receipt_and_never_reexecutes(
    vault: Path, owner, enabled, monkeypatch: pytest.MonkeyPatch, proof: str
) -> None:
    from exomem import records

    _collection(vault)
    commands.op_record_memory(vault, action="append", **_leaf(vault)["args"])
    _record(vault)
    reviewed = _prepared(vault, _correction(vault, 14))
    run = reviewed["candidates"][0]["leaves"][0]["run_id"]
    store = curation.CurationStore(vault)
    plan_id, _fingerprint = store.identities(run)

    def interrupt(name: str) -> None:
        if name == "after-leaf-witness":
            raise curation.CurationFault(name)

    monkeypatch.setattr(curation, "_fault_barrier", interrupt)
    with pytest.raises(curation.CurationFault, match="after-leaf-witness"):
        _resume(vault, reviewed)
    monkeypatch.setattr(curation, "_fault_barrier", lambda _name: None)
    monkeypatch.setattr(records, "update_record", lambda *a, **kw: pytest.fail("reexecuted"))
    (entry,) = _entries(vault)
    committed = entry.read_bytes()
    assert b"temperature_c: 14" in committed

    log = vault / "Knowledge Base/log.md"
    if proof != "valid":
        lines = []
        for line in log.read_text(encoding="utf-8").splitlines():
            if '"operation":"update"' in line:
                if proof == "missing":
                    continue
                start = line.index("{")
                event = json.loads(line[start:])
                event["before_item_hash"] = "0" * 64
                line = line[:start] + json.dumps(event, separators=(",", ":"), sort_keys=True)
            lines.append(line)
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if proof == "valid":
        recovered = curation.resume(vault, run_id=run, plan_id=plan_id)
        assert recovered["step"]["outcome"] == "recovered-committed"
        for _ in range(2):
            resumed = _resume(vault, _episode(vault, action="candidates"))
            assert resumed["status"] == "ok", resumed["blocked"]
            assert resumed["executed"] == []
        assert len(store.reconstruct(run)["receipts"]) == 1
    else:
        with pytest.raises(curation.CurationError, match="CURATION_OUTCOME_UNCERTAIN"):
            curation.resume(vault, run_id=run, plan_id=plan_id)
        resumed = _resume(vault, _episode(vault, action="candidates"))
        assert resumed["status"] == "blocked" and resumed["executed"] == []
        assert resumed["candidates"][0]["leaves"][0]["outcome"] == "uncertain"
    assert entry.read_bytes() == committed


# --------------------------------------------------------------------------- #
# The Records writer's own authority decides
# --------------------------------------------------------------------------- #


def _withhold_records_from(vault: Path, audience: str) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "withheld-records.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAE\n"
        "name: Withheld records\n"
        'paths: ["Records/Withheld Readings/**"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "withheld-records.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAF\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAE"]\n'
        f"audience: {audience}\n"
        "ceiling: 0\n",
        encoding="utf-8",
    )
    egress.clear_decision_memo()
    from exomem.governance import membership, policy

    membership.clear_memo()
    policy._CACHE.clear()  # noqa: SLF001


def test_an_invisible_collection_is_refused_exactly_like_a_missing_one(vault: Path) -> None:
    withheld = _collection(vault, "Knowledge Base/Records/Withheld Readings/_collection.md")
    with request_scope(owner_principal(surface="mcp")):
        container = _container(vault, withheld)
    _withhold_records_from(vault, "client-a")
    errors = []
    with request_scope(RequestPrincipal(audience_id="client-a", surface="mcp")):
        _record(vault)
        for collection in (withheld, "Knowledge Base/Records/Missing Readings/_collection.md"):
            leaf = {
                "leaf_key": "append",
                "effect_revision": 1,
                "kind": "append-record",
                "args": {
                    "collection": collection,
                    "item": READING,
                    "why": "A reading.",
                    "expected_container_hash": container,
                },
            }
            with pytest.raises(ValueError) as refused:
                _episode(
                    vault, action="prepare", candidate="reading", proposal=_proposal("records", [leaf])
                )
            errors.append(str(refused.value))
    assert errors[0] == errors[1] and "COLLECTION_NOT_FOUND" in errors[0]
    assert _entries(vault, withheld) == []


@pytest.mark.parametrize("kind", ["append-record", "update-record"])
def test_a_planning_collection_is_not_a_records_destination(vault: Path, owner, kind: str) -> None:
    from lifecycle_fixtures import PLANNING_PATH, planning_manifest

    planning = vault / PLANNING_PATH
    planning.parent.mkdir(parents=True)
    (planning.parent / "Items").mkdir()
    planning.write_text(planning_manifest(), encoding="utf-8")
    _record(vault)
    leaf = {
        "leaf_key": "append",
        "effect_revision": 1,
        "kind": "append-record",
        "args": {
            "collection": PLANNING_PATH,
            "item": {"title": "Book the kiln"},
            "why": "A mentioned possibility.",
            "expected_container_hash": "0" * 64,
        },
    }
    if kind == "update-record":
        leaf["kind"] = kind
        leaf["args"].pop("item")
        leaf["args"].update(
            changes={"title": "Book the kiln"},
            item_key="11111111-1111-4111-8111-111111111111",
            expected_item_version="0" * 64,
        )
    with pytest.raises(ValueError, match="RECORDS_PROFILE_REQUIRED"):
        _episode(vault, action="prepare", candidate="kiln", proposal=_proposal("records", [leaf]))
    assert list((planning.parent / "Items").iterdir()) == []


def test_a_refused_value_is_not_parked_as_a_held_candidate(vault: Path, owner) -> None:
    _collection(vault)
    _record(vault)
    before = sorted(path.relative_to(vault) for path in (vault / "Knowledge Base").rglob("*"))
    with pytest.raises(ValueError):
        _episode(
            vault,
            action="prepare",
            candidate="reading",
            proposal=_proposal("records", [_leaf(vault, {"read_on": "2026-09-20", "vat": "north"})]),
        )
    after = sorted(path.relative_to(vault) for path in (vault / "Knowledge Base").rglob("*"))
    assert after == before
    inspected = commands.op_record_memory(vault, action="inspect", collection=COLLECTION)
    assert inspected["coverage"]["held"] == 0


def test_a_records_append_has_no_compensation(vault: Path, owner, enabled) -> None:
    _collection(vault)
    _record(vault)
    executed = _resume(vault, _prepared(vault))
    run = executed["candidates"][0]["leaves"][0]["run_id"]
    with pytest.raises(curation.CurationError) as refused:
        curation.propose_compensation(vault, run_id=run)
    assert refused.value.code == "CURATION_COMPENSATION_UNAVAILABLE"
    assert curation.compensation_kind("append-record") == "unavailable"


def test_the_synthetic_slice_records_event_lands_in_its_collection(
    vault: Path, owner, enabled
) -> None:
    """3.5: the slice's Records event, once deferred, runs through its writer."""
    _collection(vault)
    _record(vault)
    before_planning = sorted((vault / "Knowledge Base").rglob("Planning/**/*.md"))
    executed = _resume(vault, _prepared(vault))
    assert executed["status"] == "ok", executed["blocked"]
    queried = commands.op_record_memory(vault, action="query", collection=COLLECTION)
    encoded = json.dumps(queried, default=str)
    assert '"temperature_c": 41' in encoded and '"vat": "north"' in encoded
    # Nothing was expressed as Planning, and nothing appeared there.
    assert sorted((vault / "Knowledge Base").rglob("Planning/**/*.md")) == before_planning


# --------------------------------------------------------------------------- #
# Security review round: withheld equals absent after preparation (M1), the
# Records tree under path folding (I1)
# --------------------------------------------------------------------------- #

_IDS = {"episode", "episode_id", "journal_digest", "revision", "leaf_id", "candidate_id",
        "run_id", "operation_id", "effect_digest", "receipt_digest", "ref"}


def _shape(value: object) -> object:
    """A resume outcome without the identities that differ between episodes."""
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items() if key not in _IDS}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    return value


@pytest.mark.parametrize("kind", ["append-record", "update-record"])
def test_a_collection_withheld_after_preparation_resumes_exactly_like_a_deleted_one(
    vault: Path, enabled, kind: str
) -> None:
    import shutil

    withheld = _collection(
        vault,
        "Knowledge Base/Records/Withheld Readings/_collection.md",
        exomem_id="11111111-4c5a-4b6c-8d7e-9f0a1b2c3d4e",
    )
    deleted = _collection(
        vault,
        "Knowledge Base/Records/Deleted Readings/_collection.md",
        exomem_id="22222222-4c5a-4b6c-8d7e-9f0a1b2c3d4e",
    )
    keys = {withheld: "ep-" + "a7" * 16, deleted: "ep-" + "b8" * 16}
    client = RequestPrincipal(audience_id="client-a", surface="mcp")
    reviewed: dict[str, dict] = {}
    with request_scope(client):
        for collection, key in keys.items():
            episode = functools.partial(
                commands.op_episode_memory,
                vault,
                schema_module.load_source_schema(vault),
                episode=key,
            )
            episode(
                action="record",
                subject="Dye vat reading",
                summary="Logged a vat reading.",
                decided=["Log the reading"],
            )
            leaf = {
                "leaf_key": "append",
                "effect_revision": 1,
                "kind": "append-record",
                "args": {
                    "collection": collection,
                    "item": READING,
                    "why": "The episode logged a vat reading.",
                    "expected_container_hash": _container(vault, collection),
                },
            }
            if kind == "update-record":
                commands.op_record_memory(vault, action="append", **leaf["args"])
                leaf = _correction(vault, 14, collection=collection)
            episode(action="prepare", candidate="reading", proposal=_proposal("records", [leaf]))
            reviewed[collection] = episode(
                action="disposition", candidate="reading", disposition="routed", reason="r"
            )
    # One collection is withheld from this caller, the other is gone.
    _withhold_records_from(vault, "client-a")
    shutil.rmtree((vault / deleted).parent)

    outcomes = {}
    with request_scope(client):
        for collection, key in keys.items():
            outcomes[collection] = commands.op_episode_memory(
                vault,
                schema_module.load_source_schema(vault),
                episode=key,
                action="resume",
                input_revision=1,
                journal_digest=reviewed[collection]["journal_digest"],
            )
    assert json.dumps(_shape(outcomes[withheld]), sort_keys=True) == json.dumps(
        _shape(outcomes[deleted]), sort_keys=True
    )
    assert outcomes[withheld]["status"] == "stale"
    assert [item["code"] for item in outcomes[withheld]["stale"]] == ["CURATION_BINDING_STALE"]
    leaf = outcomes[withheld]["candidates"][0]["leaves"][0]
    assert leaf["outcome"] == "pending" and leaf["attempts"] == 0
    assert len(_entries(vault, withheld)) == (1 if kind == "update-record" else 0)


def test_path_folding_keeps_the_records_tree_closed(vault: Path) -> None:
    # Spellings a filesystem folds onto the Records tree stay refused for
    # every other kind...
    for spelling in ("Records.", "Records ", "records", "Recordſ", "ＲＥＣＯＲＤＳ"):
        path = f"Knowledge Base/{spelling}/Vat Readings/Entries/reading.md"
        with pytest.raises(curation.CurationError) as refused:
            curation.normalize_target_path(path)
        assert refused.value.code == "CURATION_TARGET_PROTECTED", spelling
    for spelling in ("Sources.", "_Schema ", "Evidencе"):
        path = f"Knowledge Base/{spelling}/x.md"
        if spelling == "Evidencе":  # a Cyrillic homoglyph is a different name
            assert curation.normalize_target_path(path) == path
            continue
        with pytest.raises(curation.CurationError) as refused:
            curation.normalize_target_path(path)
        assert refused.value.code == "CURATION_TARGET_PROTECTED", spelling
    # ...and the Records leaf itself accepts only the exact Records layer.
    exact = "Knowledge Base/Records/Vat Readings/Entries/reading.md"
    assert curation.normalize_target_path(exact, allow_records=True) == exact
    for spelling in ("Records.", "Records ", "records", "Recordſ"):
        path = f"Knowledge Base/{spelling}/Vat Readings/Entries/reading.md"
        with pytest.raises(curation.CurationError) as refused:
            curation.normalize_target_path(path, allow_records=True)
        assert refused.value.code == "CURATION_TARGET_PROTECTED", spelling


def test_a_collection_withheld_after_commit_attests_exactly_like_a_deleted_one(
    vault: Path, enabled
) -> None:
    import shutil

    withheld = _collection(
        vault,
        "Knowledge Base/Records/Withheld Readings/_collection.md",
        exomem_id="33333333-4c5a-4b6c-8d7e-9f0a1b2c3d4e",
    )
    deleted = _collection(
        vault,
        "Knowledge Base/Records/Deleted Readings/_collection.md",
        exomem_id="44444444-4c5a-4b6c-8d7e-9f0a1b2c3d4e",
    )
    keys = {withheld: "ep-" + "c7" * 16, deleted: "ep-" + "d8" * 16}
    client = RequestPrincipal(audience_id="client-a", surface="mcp")
    executed: dict[str, dict] = {}
    with request_scope(client):
        for collection, key in keys.items():
            episode = functools.partial(
                commands.op_episode_memory,
                vault,
                schema_module.load_source_schema(vault),
                episode=key,
            )
            episode(
                action="record",
                subject="Dye vat reading",
                summary="Logged a vat reading.",
                decided=["Log the reading"],
            )
            leaf = {
                "leaf_key": "append",
                "effect_revision": 1,
                "kind": "append-record",
                "args": {
                    "collection": collection,
                    "item": READING,
                    "why": "The episode logged a vat reading.",
                    "expected_container_hash": _container(vault, collection),
                },
            }
            episode(action="prepare", candidate="reading", proposal=_proposal("records", [leaf]))
            reviewed = episode(
                action="disposition", candidate="reading", disposition="routed", reason="r"
            )
            executed[collection] = episode(
                action="resume", input_revision=1, journal_digest=reviewed["journal_digest"]
            )
            assert executed[collection]["status"] == "ok", executed[collection]["blocked"]
    _withhold_records_from(vault, "client-a")
    shutil.rmtree((vault / deleted).parent)

    errors = {}
    passes = {}
    with request_scope(client):
        for collection, key in keys.items():
            episode = functools.partial(
                commands.op_episode_memory,
                vault,
                schema_module.load_source_schema(vault),
                episode=key,
            )
            passes[collection] = episode(action="coverage")
            with pytest.raises(ValueError) as refused:
                episode(
                    action="resume",
                    input_revision=1,
                    postcommit=True,
                    journal_digest=executed[collection]["journal_digest"],
                )
            errors[collection] = str(refused.value)
    assert errors[withheld] == errors[deleted]
    assert "EPISODE_OUTCOME_UNCERTAIN" in errors[withheld]
    assert json.dumps(_shape(passes[withheld]), sort_keys=True) == json.dumps(
        _shape(passes[deleted]), sort_keys=True
    )
    assert {row["readback"] for row in passes[withheld]["receipts"]} == {"unavailable"}
