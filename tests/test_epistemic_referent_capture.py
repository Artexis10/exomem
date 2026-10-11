"""Sequence-seven wiring: the frozen f33 case set, its withholding and its predicates.

The helpers read every name and value from the digest-verified answer key, so no
test restates case wording.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from epistemic.assertions import AssertionContext
from epistemic.registry import resolve
from epistemic.snapshot import (
    EpistemicStateSnapshot,
    FieldDeclaration,
    ProjectorMeta,
    StateItem,
    TypedRelation,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "benchmarks/epistemic/fixtures/sequence7"
RECEIPT = ROOT / "benchmarks/epistemic/contracts/amendment-2026-10-referent-capture.v1.json"
PET = "f33-pet-owner-v1"
PET_TWIN = "f33-pet-owner-twin-v1"
SPARSE_TWIN = "f33-sparse-existing-twin-v1"
SURFACES = ("audit_findings", "review_queue", "proposal_queue", "due_state_counters")
PROJECTOR = ProjectorMeta(
    name="f33-fixture", version="0", author="benchmark-harness", endpoints_used=(), loc=1
)


def _key(case_id: str) -> dict:
    from epistemic.journeys.referent_capture import case_key

    return case_key(case_id)


def _snapshot(items: tuple[StateItem, ...], typed: tuple[TypedRelation, ...] = ()):
    markers = tuple(
        StateItem(id=f"surface-{name}", kind="container", raw={"surface": name, "projection": "complete"})
        for name in SURFACES
    )
    declared = tuple(
        FieldDeclaration(field=field, status="declared", evidence="benchmarks/epistemic/PREREGISTRATION.md:1")
        for field in ("signal", "review_state", "due_state_counters")
    )
    return EpistemicStateSnapshot(
        provider="exomem", phase="hookless", taken_at="2026-10-10T00:00:00Z",
        items=(*items, *markers), typed_relations=typed, declarations=declared, projector=PROJECTOR,
    )


def _entity(item_id: str, name: str, lineage: tuple[str, ...], core: tuple[str, ...]) -> StateItem:
    return StateItem(
        id=item_id, kind="container", title=name, locator=f"{item_id}.md",
        raw={"type": "entity", "entity_type": lineage[0] if lineage else "unregistered",
             "entity_type_lineage": ",".join(lineage), "entity_type_core": ",".join(core)},
    )


def positive_context(
    *, typed: bool = True, edge: bool = True, served: bool = True, exact: bool = True
) -> AssertionContext:
    """The pet-and-owner positive after a run, with one property switched off."""

    key = _key(PET)
    owner = key["owner"]["ref"]
    name = key["referent"]["name"]
    referent_id = f"Knowledge Base/Entities/Companions/{name}"
    lineage, core = (("companion",), ()) if typed else (("concept",), ("concept",))
    relation = TypedRelation(
        subject=owner, relation="owns" if edge else "mentions",
        object=f"Entities/Companions/{name}", family="ownership" if edge else "mention",
    )
    snapshot = _snapshot(
        (_entity(owner, key["owner"]["name"], ("person",), ("person",)),
         _entity(referent_id, name, lineage, core)),
        (relation,),
    )
    return AssertionContext(
        snapshot=snapshot, subject=PET,
        served_items=(referent_id if served else owner,),
        fresh_answer=f"The answer is {key['answer_value']}." if exact else "I cannot say.",
    )


def twin_context(*, created: bool = False) -> AssertionContext:
    """The pet-and-owner twin: seeded, then either quiet or with a new entity."""

    key = _key(PET_TWIN)
    owner = _entity(key["owner"]["ref"], key["owner"]["name"], ("person",), ("person",))
    prior = _snapshot((owner,))
    name = key["referent"]["name"]
    added = (_entity(f"Knowledge Base/Entities/Companions/{name}", name, ("companion",), ()),)
    final = _snapshot((owner, *(added if created else ())))
    return AssertionContext(snapshot=final, prior=prior, subject=PET_TWIN)


def test_the_receipt_freezes_every_case_file_and_binds_the_amended_document() -> None:
    """Fails when a case file is added or edited without refreezing, or §7 drifts."""

    from protocol.contracts import working_amendment_receipts

    chain = working_amendment_receipts(ROOT)
    seventh = chain[6]
    assert [receipt.sequence for receipt in chain] == [1, 2, 3, 4, 5, 6, 7]
    assert seventh.acknowledgment_status == "pending"
    assert seventh.parent_contract_sha256 == chain[5].contract_sha256
    prereg = (ROOT / "benchmarks/epistemic/PREREGISTRATION.md").read_bytes()
    assert seventh.contract_sha256 == hashlib.sha256(prereg).hexdigest()
    on_disk = {
        f"benchmarks/epistemic/fixtures/sequence7/{path.name}": hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in FIXTURES.iterdir()
    }
    assert seventh.fixture_sha256 == on_disk
    entry = prereg.decode("utf-8").split("**Referent-capture replay amendment, sequence 7.**")[1]
    for path, digest in on_disk.items():
        assert f"`{Path(path).name}`: `{digest}`" in entry


def test_f33_is_withheld_at_load_and_evaluation_until_acknowledged() -> None:
    """Fails when an unacknowledged f33 case could back a run or a score."""

    from epistemic.amendments import amendment_sequence_for, withheld_family_ids
    from epistemic.runner import evaluate_scenario
    from epistemic.schema import Scenario, ScenarioLoadError, load_scenario
    from protocol.contracts import AmendmentAcknowledgmentPendingError

    assert amendment_sequence_for("f33") == 7
    assert "f33" in withheld_family_ids(ROOT)
    with pytest.raises(ScenarioLoadError, match=r"sequence 7 .*pending.*f33"):
        load_scenario(FIXTURES / f"{PET}.yaml")
    import yaml

    payload = yaml.safe_load((FIXTURES / f"{PET}.yaml").read_text(encoding="utf-8"))
    with pytest.raises(AmendmentAcknowledgmentPendingError, match=r"sequence 7 .*pending"):
        evaluate_scenario(Scenario.model_validate_json(json.dumps(payload)), snapshots={})


def test_case_files_load_only_from_their_frozen_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails when an edited case file would still load and be scored."""

    from epistemic.schema import ScenarioLoadError, load_scenario_text, load_scenarios

    monkeypatch.setattr("epistemic.schema.require_family_released", lambda _family: None)
    loaded = load_scenarios(FIXTURES)
    assert len(loaded) == 12
    text = (FIXTURES / f"{PET}.yaml").read_text(encoding="utf-8")
    with pytest.raises(ScenarioLoadError, match="frozen digest"):
        load_scenario_text(text.replace("hooked-balanced", "hooked-maximal"), source=PET)


def _frozen_copy(tmp_path: Path, edit) -> Path:
    """A repository copy whose fixtures carry ``edit``, refrozen in its receipt."""

    root = tmp_path / "repo"
    target = root / "benchmarks/epistemic/fixtures/sequence7"
    shutil.copytree(FIXTURES, target)
    (root / RECEIPT.relative_to(ROOT)).parent.mkdir(parents=True)
    edit(target)
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    receipt["fixture_sha256"] = {
        f"benchmarks/epistemic/fixtures/sequence7/{path.name}": hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in target.iterdir()
    }
    (root / RECEIPT.relative_to(ROOT)).write_text(json.dumps(receipt), encoding="utf-8")
    return root


def test_key_and_seed_reads_refuse_edited_bytes(tmp_path: Path) -> None:
    """Fails when an assertion or a seeded run could read an edited key or seed."""

    from epistemic.journeys.referent_capture import (
        KEY_PATH,
        SEED_PATH,
        ReferentCaseError,
        answer_key,
        reset_cache,
        seed_inputs,
    )

    root = _frozen_copy(tmp_path, lambda _target: None)
    for relative in (KEY_PATH, SEED_PATH):
        path = root / relative
        path.write_bytes(path.read_bytes() + b"\n")
    reset_cache()
    with pytest.raises(ReferentCaseError, match="frozen digest"):
        answer_key(root)
    with pytest.raises(ReferentCaseError, match="frozen digest"):
        seed_inputs(tmp_path / "vault", repo_root=root)
    reset_cache()


def _append_to_first_turn(case_id: str, suffix: str):
    def edit(target: Path) -> None:
        import yaml

        path = target / f"{case_id}.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        for phase in payload["phases"]:
            turn = next(op for op in phase["ops"] if op["op"] == "agent_turn")
            turn["detail"] += suffix
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    return edit


def _set_answer_value(case_id: str, value: str):
    def edit(target: Path) -> None:
        from epistemic.journeys.referent_capture import KEY_PATH

        path = target / Path(KEY_PATH).name
        key = json.loads(path.read_text(encoding="utf-8"))
        next(case for case in key["cases"] if case["case_id"] == case_id)["answer_value"] = value
        path.write_text(json.dumps(key), encoding="utf-8")

    return edit


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ("twin_mention", "as often"),
        ("store_bearing", "store-bearing"),
        ("unstated_value", "not stated"),
    ],
)
def test_case_set_construction_refuses_a_malformed_case(
    tmp_path: Path, change: str, refusal: str
) -> None:
    """Fails when a mention-mismatched twin, a storage command or an unstated key value builds."""

    from epistemic.journeys.referent_capture import ReferentCaseError, build_case_set, reset_cache

    name = _key(PET)["referent"]["name"]
    edits = {
        "twin_mention": _append_to_first_turn(PET_TWIN, f" {name} again."),
        "store_bearing": _append_to_first_turn(PET, " Save this."),
        "unstated_value": _set_answer_value(PET, "999999"),
    }
    root = _frozen_copy(tmp_path, edits[change])
    reset_cache()
    with pytest.raises(ReferentCaseError, match=refusal):
        build_case_set(root)
    reset_cache()


def test_the_real_case_set_builds() -> None:
    """Fails when the frozen set itself breaks a construction rule."""

    from epistemic.journeys.referent_capture import build_case_set

    cases = build_case_set()
    positives = [case for case in cases if case.polarity == "positive"]
    assert len(cases) == 12
    assert len({case.category for case in positives}) == 6
    assert all(case.question for case in positives)


def test_entity_graph_projection_reads_the_vault_registries(tmp_path: Path) -> None:
    """Fails when the projector drops a typed edge or misreads a vault-declared type."""

    from epistemic.journeys.referent_capture import seed_inputs
    from epistemic.projectors.exomem_vault import VaultProjector

    key = _key(PET)
    name = key["referent"]["name"]
    seed_inputs(tmp_path)
    kb = tmp_path / "Knowledge Base"
    (kb / "_Schema/entity-types.yaml").write_text(
        "schema_version: 1\nentity_types:\n  companion:\n    folder: Companions\n"
        "    label: Companion\n    aliases: []\n    cue_nouns: []\n    capture_guidance: A durable companion.\n"
        "    status: active\n",
        encoding="utf-8",
    )
    referent = kb / f"Entities/Companions/{name}.md"
    referent.parent.mkdir(parents=True)
    referent.write_text(
        f"---\ntype: entity\nentity_type: companion\ntitle: {name}\nstatus: active\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    owner = tmp_path / f"{key['owner']['ref']}.md"
    owner.write_text(
        owner.read_text(encoding="utf-8") + f"\n## Relations\n\n- owns [[Entities/Companions/{name}]]\n",
        encoding="utf-8",
    )
    projected = VaultProjector(tmp_path, entity_graph=True).project(
        phase="hookless", taken_at="2026-10-10T00:00:00Z"
    )
    entity = projected.item(f"Knowledge Base/Entities/Companions/{name}")
    assert entity is not None
    assert (entity.raw["entity_type_lineage"], entity.raw["entity_type_core"]) == ("companion", "")
    assert [(edge.relation, edge.family) for edge in projected.typed_relations] == [
        ("owns", "ownership")
    ]
    ctx = AssertionContext(snapshot=projected, subject=PET)
    assert resolve("referent_entity_typed")(ctx).outcome == "pass"
    assert resolve("referent_key_edge_present")(ctx).outcome == "pass"
    plain = VaultProjector(tmp_path).project(phase="hookless", taken_at="2026-10-10T00:00:00Z")
    assert plain.typed_relations == ()
    assert "entity_type_lineage" not in plain.item(entity.id).raw


@pytest.mark.parametrize(
    ("served", "exact", "missing", "outcome", "evidence"),
    [
        (False, True, False, "fail", "does not serve"),
        (True, False, False, "unsupported", "judge_fallback"),
        (True, True, True, "blocked", "not captured"),
    ],
)
def test_uses_is_deterministic_except_the_one_judge_fallback_case(
    served: bool, exact: bool, missing: bool, outcome: str, evidence: str
) -> None:
    """Fails when an unserved ref passes on its value, or the judge reaches a decided case."""

    ctx = positive_context(served=served, exact=exact)
    if missing:
        ctx = ctx.replace(fresh_answer=None)
    result = resolve("referent_used_in_fresh_session")(ctx)
    assert (result.outcome, evidence in result.evidence) == (outcome, True)


def test_twin_counts_only_what_the_run_added_to_a_seeded_entity() -> None:
    """Fails when the sparse-existing twin is charged with its own seeded entity."""

    key = _key(SPARSE_TWIN)
    seeded = key["referent"]["seeded_ref"]
    owner = _entity(key["owner"]["ref"], key["owner"]["name"], ("person",), ("person",))
    existing = _entity(seeded, key["referent"]["name"], ("organization",), ("organization",))
    prior = _snapshot((owner, existing))
    quiet = AssertionContext(snapshot=_snapshot((owner, existing)), prior=prior, subject=SPARSE_TWIN)
    edge = TypedRelation(subject=owner.id, relation="operates", object=seeded, family="operation")
    linked = quiet.replace(snapshot=_snapshot((owner, existing), (edge,)))
    assert resolve("twin_left_no_referent")(quiet).outcome == "pass"
    assert resolve("twin_left_no_referent")(linked).outcome == "fail"


def _project_with_surfaces(vault: Path):
    """The real entity-graph projection, with the four notice surfaces marked complete."""

    from epistemic.projectors.exomem_vault import VaultProjector

    projected = VaultProjector(vault, entity_graph=True).project(
        phase="hookless", taken_at="2026-10-10T00:00:00Z"
    )
    marked = _snapshot(())
    return projected.model_copy(
        update={
            "items": (
                *(item for item in projected.items if "surface" not in item.raw),
                *marked.items,
            ),
            "declarations": marked.declarations,
        }
    )


def test_twin_fails_on_a_new_generic_edge_to_its_name(tmp_path: Path) -> None:
    """Fails when a generic or neutral-predicate edge to the twin's name passes the twin."""

    from epistemic.journeys.referent_capture import seed_inputs

    key = _key(PET_TWIN)
    seed_inputs(tmp_path)
    prior = _project_with_surfaces(tmp_path)
    owner = tmp_path / f"{key['owner']['ref']}.md"
    owner.write_text(
        owner.read_text(encoding="utf-8")
        + f"\n## Relations\n\n- relates_to [[{key['referent']['name']}]]\n",
        encoding="utf-8",
    )
    ctx = AssertionContext(
        snapshot=_project_with_surfaces(tmp_path), prior=prior, subject=PET_TWIN
    )
    assert ctx.snapshot.typed_relations == ()
    result = resolve("twin_left_no_referent")(ctx)
    assert (result.outcome, "edge" in result.evidence) == ("fail", True)


def test_an_untyped_referent_page_fails_typing_and_leaves_the_key_edge_scored(
    tmp_path: Path,
) -> None:
    """Fails when an untyped entity page reads as a projection gap instead of a miss."""

    from epistemic.journeys.referent_capture import seed_inputs

    key = _key(PET)
    name = key["referent"]["name"]
    seed_inputs(tmp_path)
    kb = tmp_path / "Knowledge Base"
    (kb / "_Schema/entity-types.yaml").write_text(
        "schema_version: 1\nentity_types:\n  companion:\n    folder: Companions\n"
        "    label: Companion\n    aliases: []\n    cue_nouns: []\n"
        "    capture_guidance: A durable companion.\n    status: active\n",
        encoding="utf-8",
    )
    typed = kb / f"Entities/Companions/{name}.md"
    typed.parent.mkdir(parents=True)
    typed.write_text(
        f"---\ntype: entity\nentity_type: companion\ntitle: {name}\nstatus: active\n---\n",
        encoding="utf-8",
    )
    (kb / f"Entities/{name}.md").write_text(
        f"---\ntype: entity\ntitle: {name}\nstatus: active\n---\n", encoding="utf-8"
    )
    owner = tmp_path / f"{key['owner']['ref']}.md"
    owner.write_text(
        owner.read_text(encoding="utf-8")
        + f"\n## Relations\n\n- owns [[Entities/Companions/{name}]]\n",
        encoding="utf-8",
    )
    with_duplicate = AssertionContext(snapshot=_project_with_surfaces(tmp_path), subject=PET)
    assert resolve("referent_key_edge_present")(with_duplicate).outcome == "pass"
    assert resolve("referent_entity_typed")(with_duplicate).outcome == "pass"
    typed.unlink()
    untyped_only = AssertionContext(snapshot=_project_with_surfaces(tmp_path), subject=PET)
    assert resolve("referent_entity_typed")(untyped_only).outcome == "fail"
