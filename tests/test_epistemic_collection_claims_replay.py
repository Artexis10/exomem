from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest


def test_promotion_corpus_is_deterministic_and_has_a_frequency_matched_twin() -> None:
    from epistemic.journeys.f28_promotion_replay import promotion_corpus

    positive = promotion_corpus()
    twin = promotion_corpus(twin=True)
    assert positive == promotion_corpus()
    assert positive.digest() == promotion_corpus().digest()
    assert positive.family_id == twin.family_id == "f28"
    assert len(positive.turns) == len(twin.turns)
    assert positive.expect_candidate and not twin.expect_candidate
    assert positive.candidate_turn is not None
    assert sum(turn.confirm for turn in positive.turns) == 1
    assert len(positive.expected_records()) == 4
    assert len({row["identity"] for row in positive.expected_records()}) == 2
    assert len({row["identity"] for row in twin.expected_records()}) == 4
    assert any(turn.event is None and not turn.confirm for turn in positive.turns)


def test_publication_corpus_pairs_exact_text_and_evidence_with_each_event() -> None:
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus

    corpus = routing_corpus()
    assert corpus == routing_corpus()
    assert corpus.family_id == "f29"
    rows = corpus.expected_records()
    assert len(rows) == 3
    assert all("\n" in row["exact_text"] for row in rows)
    assert all(row["sources"] for row in rows)
    assert len({row["post_key"] for row in rows}) == 3
    assert any(turn.event is None for turn in corpus.turns)


def test_publication_artifacts_are_real_immutable_client_input() -> None:
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus

    corpus = routing_corpus()
    for turn in corpus.turns:
        if turn.event is None:
            continue
        assert len(turn.attachments) == 1
        proof = turn.attachments[0]
        assert proof.media_type == "text/plain"
        assert dict(turn.event)["exact_text"] in proof.content
        assert dict(turn.event)["post_key"] in proof.content
        assert proof.content in turn.client_input()
        assert proof.name in turn.client_input()


@pytest.mark.parametrize(
    "word", ["collection", "collections", "ledger", "schema", "claims", "tracking", "save this"]
)
def test_corpus_construction_refuses_store_bearing_user_turns(word: str) -> None:
    from epistemic.journeys.collection_replay import StoreBearingUtterance, build_corpus
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus

    corpus = routing_corpus()
    bad = replace(corpus.turns[0], text=f"Please use a {word} for this.")
    with pytest.raises(StoreBearingUtterance, match=bad.turn_id):
        build_corpus(replace(corpus, turns=(bad, *corpus.turns[1:])))


def test_sequence_four_fixtures_are_pinned_to_their_corpus_turns() -> None:
    import yaml
    from epistemic.journeys.collection_replay import fixture_payload
    from epistemic.journeys.f28_promotion_replay import promotion_corpus
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus

    root = Path(__file__).resolve().parents[1] / "benchmarks/epistemic/fixtures/sequence4"
    for corpus in (promotion_corpus(), promotion_corpus(twin=True), routing_corpus()):
        payload = yaml.safe_load((root / f"{corpus.corpus_id}.yaml").read_text())
        assert payload == fixture_payload(corpus)
        for arm in ("hookless", "hooked"):
            phases = [phase for phase in payload["phases"] if phase["phase_id"].startswith(arm)]
            turns = [
                (op["ref"], op["detail"])
                for phase in phases
                for op in phase["ops"]
                if op["op"] == "agent_turn"
            ]
            assert turns == [(turn.turn_id, turn.client_input()) for turn in corpus.turns]
            assert all(sum(op["op"] == "snapshot" for op in phase["ops"]) >= 2 for phase in phases)


def _proof_text(corpus, index):
    return [turn.attachments[0].content for turn in corpus.turns if turn.event is not None][index]


@pytest.mark.parametrize("source_kind", ["markdown", "sidecar", "stable", "artifact"])
def test_real_projector_checks_preserved_proof_bytes(tmp_path, monkeypatch, source_kind) -> None:
    from epistemic.assertions import (
        AssertionContext,
        claimed_observation_reflected,
        no_structured_write_beyond_expectation,
    )
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus
    from epistemic.projectors.exomem_vault import VaultProjector
    from exomem import commands

    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path.with_name(tmp_path.name + "-state")))
    corpus = routing_corpus()
    sidecar = source_kind != "markdown"
    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base/log.md").write_text("# Activity\n")
    projector = VaultProjector(tmp_path)
    seed = projector.project(phase="hookless-observed", taken_at="2026-09-08T12:00:00Z")
    seed = seed.model_copy(update={"collections": (_collection(corpus, rows=[]),)})
    rows = corpus.expected_records()
    for index, row in enumerate(rows):
        proof = _proof_text(corpus, index)
        if sidecar:
            result = commands.op_preserve_evidence(
                tmp_path,
                scope="studio",
                category="publications",
                filename=f"publication-{index}.txt",
                content=proof,
                description="Published studio bulletin update",
            )
            row["sources"] = [result[{"sidecar": "sidecar_path", "stable": "ref", "artifact": "path"}[source_kind]]]
        else:
            path = tmp_path / row["sources"][0]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("---\ntype: source\n---\n\n" + proof)
    observed = VaultProjector(tmp_path).project(
        phase="hookless-observed", taken_at="2026-09-08T12:00:00Z"
    )
    observed = observed.model_copy(update={"collections": (_collection(corpus, rows=rows),)})
    if sidecar:
        assert result["path"] in {item.locator for item in observed.items}
        assert result["sidecar_path"] in {item.locator for item in observed.items}
    assert (
        claimed_observation_reflected(AssertionContext(observed, subject=corpus.corpus_id)).outcome
        == "pass"
    )
    assert (
        no_structured_write_beyond_expectation(
            AssertionContext(observed, prior=seed, subject=corpus.corpus_id)
        ).outcome
        == "pass"
    )
    orphan = tmp_path / "Knowledge Base/Records/Studio/Items/unexpected.txt"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("An unrepresented extra write.")
    with_orphan = projector.project(phase=observed.phase, taken_at=observed.taken_at).model_copy(
        update={"collections": observed.collections}
    )
    assert (
        no_structured_write_beyond_expectation(
            AssertionContext(with_orphan, prior=seed, subject=corpus.corpus_id)
        ).outcome
        == "fail"
    )
    if sidecar:
        # The companion's declared digest remains unchanged: the observed bytes
        # must determine whether the preserved proof is intact.
        (tmp_path / result["path"]).write_text("A different artifact.\n")
        tampered = projector.project(phase=observed.phase, taken_at=observed.taken_at).model_copy(
            update={"collections": observed.collections}
        )
        assert (
            claimed_observation_reflected(
                AssertionContext(tampered, subject=corpus.corpus_id)
            ).outcome
            == "fail"
        )


def _snapshot(*, collections=(), signals=(), phase="hookless-observed"):
    from epistemic.snapshot import (
        EpistemicStateSnapshot,
        FieldDeclaration,
        ProjectorMeta,
        StateItem,
    )

    surfaces = ("audit_findings", "review_queue", "proposal_queue", "due_state_counters")
    return EpistemicStateSnapshot(
        provider="fixture",
        phase=phase,
        taken_at="2026-09-08T12:00:00Z",
        collections=tuple(collections),
        declarations=tuple(
            FieldDeclaration(
                field=name, status="declared", evidence="benchmarks/epistemic/PREREGISTRATION.md:1"
            )
            for name in ("signal", "review_state", "due_state_counters")
        ),
        items=tuple(
            StateItem(
                id=f"surface-{surface}",
                kind="container",
                raw={"surface": surface, "projection": "complete"},
            )
            for surface in surfaces
        )
        + tuple(signals),
        projector=ProjectorMeta(
            name="fixture", version="1", author="test", endpoints_used=("fixture",), loc=1
        ),
    )


def _collection(corpus, *, rows=None):
    from epistemic.snapshot import CollectionItem, CollectionProjection

    return CollectionProjection(
        id=corpus.seeded_collection or "promoted",
        profile="records",
        manifest="Knowledge Base/Records/Studio/_collection.md",
        title="Studio events",
        schema_version=1,
        natural_key=corpus.natural_key,
        storage_source="Items",
        claims={"terms": (corpus.domain,)},
        items=tuple(
            CollectionItem(
                key=f"item-{index}",
                natural_key={name: row[name] for name in corpus.natural_key},
                values=row,
            )
            for index, row in enumerate(corpus.expected_records() if rows is None else rows)
        ),
    )


def test_claimed_observation_requires_exact_text_hash_and_resolved_evidence() -> None:
    from epistemic.assertions import AssertionContext, claimed_observation_reflected
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus
    from epistemic.snapshot import StateItem

    corpus = routing_corpus()
    evidence = tuple(
        StateItem(
            id=f"proof-{i}",
            kind="evidence",
            locator=row["sources"][0],
            locator_kind="file",
            text=_proof_text(corpus, i),
        )
        for i, row in enumerate(corpus.expected_records())
    )
    good = _snapshot(collections=(_collection(corpus),), signals=evidence)
    assert (
        claimed_observation_reflected(AssertionContext(good, subject=corpus.corpus_id)).outcome
        == "pass"
    )
    rows = corpus.expected_records()
    rows[0] = {**rows[0], "exact_text": rows[0]["exact_text"].replace("\n", "\\n")}
    wrong = good.model_copy(update={"collections": (_collection(corpus, rows=rows),)})
    assert (
        claimed_observation_reflected(AssertionContext(wrong, subject=corpus.corpus_id)).outcome
        == "fail"
    )
    missing = good.model_copy(update={"items": ()})
    assert (
        claimed_observation_reflected(AssertionContext(missing, subject=corpus.corpus_id)).outcome
        == "fail"
    )
    rows = corpus.expected_records()
    rows[0] = {**rows[0], "sources": ["Knowledge Base/Evidence/another-valid-destination.md"]}
    alternate = evidence[0].model_copy(update={"locator": rows[0]["sources"][0]})
    moved = _snapshot(
        collections=(_collection(corpus, rows=rows),), signals=(alternate, *evidence[1:])
    )
    assert (
        claimed_observation_reflected(AssertionContext(moved, subject=corpus.corpus_id)).outcome
        == "pass"
    )
    unrelated = alternate.model_copy(update={"text": "This is a different artifact."})
    wrong_proof = _snapshot(collections=moved.collections, signals=(unrelated, *evidence[1:]))
    assert (
        claimed_observation_reflected(
            AssertionContext(wrong_proof, subject=corpus.corpus_id)
        ).outcome
        == "fail"
    )


def test_routing_false_write_dual_catches_duplicates_and_other_collections() -> None:
    from epistemic.assertions import AssertionContext, no_structured_write_beyond_expectation
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus

    corpus = routing_corpus()
    seed = _snapshot(collections=(_collection(corpus, rows=[]),))
    good = _snapshot(collections=(_collection(corpus),))
    assert (
        no_structured_write_beyond_expectation(
            AssertionContext(good, prior=seed, subject=corpus.corpus_id)
        ).outcome
        == "pass"
    )
    duplicate = _snapshot(
        collections=(
            _collection(corpus, rows=[*corpus.expected_records(), corpus.expected_records()[0]]),
        )
    )
    assert (
        no_structured_write_beyond_expectation(
            AssertionContext(duplicate, prior=seed, subject=corpus.corpus_id)
        ).outcome
        == "fail"
    )
    wrong_arm = seed.model_copy(update={"phase": "hooked-observed"})
    assert (
        no_structured_write_beyond_expectation(
            AssertionContext(good, prior=wrong_arm, subject=corpus.corpus_id)
        ).outcome
        == "blocked"
    )


def test_routing_false_write_dual_rejects_orphan_storage_pages() -> None:
    from epistemic.assertions import AssertionContext, no_structured_write_beyond_expectation
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus
    from epistemic.snapshot import StateItem

    corpus = routing_corpus()
    seed = _snapshot(collections=(_collection(corpus, rows=[]),))
    orphan = StateItem(
        id="extra",
        kind="claim",
        locator="Knowledge Base/Records/Studio/Items/unexpected.md",
        locator_kind="file",
    )
    snapshot = _snapshot(collections=(_collection(corpus),), signals=(orphan,))
    assert (
        no_structured_write_beyond_expectation(
            AssertionContext(snapshot, prior=seed, subject=corpus.corpus_id)
        ).outcome
        == "fail"
    )
    collection = snapshot.collections[0]
    represented = collection.model_copy(
        update={
            "items": (
                collection.items[0].model_copy(update={"locator": orphan.locator}),
                *collection.items[1:],
            )
        }
    )
    snapshot = snapshot.model_copy(update={"collections": (represented,)})
    assert (
        no_structured_write_beyond_expectation(
            AssertionContext(snapshot, prior=seed, subject=corpus.corpus_id)
        ).outcome
        == "pass"
    )


@pytest.mark.parametrize("source", ["missing.md", "wrong.md"])
def test_publication_rejects_each_bad_additional_source(source) -> None:
    from epistemic.assertions import AssertionContext, claimed_observation_reflected
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus
    from epistemic.snapshot import StateItem

    corpus = routing_corpus()
    rows = corpus.expected_records()
    evidence = tuple(
        StateItem(
            id=f"proof-{i}",
            kind="evidence",
            locator=row["sources"][0],
            locator_kind="file",
            text=_proof_text(corpus, i),
        )
        for i, row in enumerate(rows)
    )
    rows[0]["sources"].append(source)
    wrong = StateItem(
        id="wrong",
        kind="evidence",
        locator="wrong.md",
        locator_kind="file",
        text="A different publication.",
    )
    snapshot = _snapshot(collections=(_collection(corpus, rows=rows),), signals=(*evidence, wrong))
    assert (
        claimed_observation_reflected(AssertionContext(snapshot, subject=corpus.corpus_id)).outcome
        == "fail"
    )


def test_twin_rejects_collection_created_before_final_phase() -> None:
    from epistemic.assertions import AssertionContext, ledger_state_matches_expectation
    from epistemic.journeys.f28_promotion_replay import promotion_corpus

    corpus = promotion_corpus(twin=True)
    already_created = _snapshot(collections=(_collection(corpus),), phase="hookless-confirmed")
    assert (
        ledger_state_matches_expectation(
            AssertionContext(already_created, prior=already_created, subject=corpus.corpus_id)
        ).outcome
        == "fail"
    )


def test_promotion_ledger_matches_fields_and_requires_confirmation_baseline() -> None:
    from epistemic.assertions import AssertionContext, ledger_state_matches_expectation
    from epistemic.journeys.f28_promotion_replay import promotion_corpus

    corpus = promotion_corpus()
    seed = _snapshot(phase="hookless-confirmed")
    good = _snapshot(collections=(_collection(corpus),), phase=seed.phase)
    assert (
        ledger_state_matches_expectation(
            AssertionContext(good, prior=seed, subject=corpus.corpus_id)
        ).outcome
        == "pass"
    )
    assert (
        ledger_state_matches_expectation(AssertionContext(good, subject=corpus.corpus_id)).outcome
        == "blocked"
    )
    assert (
        ledger_state_matches_expectation(
            AssertionContext(good, prior=good, subject=corpus.corpus_id)
        ).outcome
        == "fail"
    )
    rows = corpus.expected_records()
    rows[0] = {**rows[0], "observed_precision": "inferred"}
    wrong = good.model_copy(update={"collections": (_collection(corpus, rows=rows),)})
    assert (
        ledger_state_matches_expectation(
            AssertionContext(wrong, prior=seed, subject=corpus.corpus_id)
        ).outcome
        == "fail"
    )


def test_candidate_twin_checks_all_surfaces_and_unknown_projection_blocks() -> None:
    from epistemic.assertions import AssertionContext, collection_candidate_surfaced_within_budget
    from epistemic.journeys.f28_promotion_replay import promotion_corpus
    from epistemic.snapshot import StateItem

    twin = promotion_corpus(twin=True)
    quiet = _snapshot()
    assert (
        collection_candidate_surfaced_within_budget(
            AssertionContext(quiet, subject=twin.corpus_id)
        ).outcome
        == "pass"
    )
    unknown = quiet.model_copy(update={"items": ()})
    assert collection_candidate_surfaced_within_budget(
        AssertionContext(unknown, subject=twin.corpus_id)
    ).outcome in {"blocked", "unsupported"}
    signal = StateItem(
        id="candidate",
        kind="container",
        raw={
            "surface": "due_state_counters",
            "signal_class": "collection_candidate",
            "targets": twin.domain,
            "category": "collection_candidate",
        },
    )
    nag = _snapshot(signals=(signal,))
    assert (
        collection_candidate_surfaced_within_budget(
            AssertionContext(nag, subject=twin.corpus_id)
        ).outcome
        == "fail"
    )
    positive = promotion_corpus()
    assert (
        collection_candidate_surfaced_within_budget(
            AssertionContext(nag, subject=positive.corpus_id)
        ).outcome
        == "pass"
    )


def test_backfill_sources_accept_stable_references_to_the_same_seeded_unit() -> None:
    from epistemic.assertions import AssertionContext, ledger_state_matches_expectation
    from epistemic.journeys.f28_promotion_replay import promotion_corpus
    from epistemic.snapshot import StateItem

    corpus = promotion_corpus()
    rows = corpus.expected_records()
    source = rows[0]["sources"][0].split("#")[0]
    identity = "62ab3323-a9de-4e8f-989b-1a1ef2267ce8"
    rows[0]["sources"] = [f"exomem://memory/{identity}#event"]
    page = StateItem(
        id="seeded-note",
        kind="claim",
        locator=source,
        locator_kind="file",
        raw={"exomem_id": identity},
    )
    seed = _snapshot(signals=(page,), phase="hookless-confirmed")
    observed = _snapshot(
        collections=(_collection(corpus, rows=rows),), signals=(page,), phase=seed.phase
    )
    assert (
        ledger_state_matches_expectation(
            AssertionContext(observed, prior=seed, subject=corpus.corpus_id)
        ).outcome
        == "pass"
    )


def test_sequence_four_is_registered_but_withheld_at_load_evaluation_and_manifest(
    monkeypatch,
) -> None:
    import json

    from epistemic import amendments, registry
    from epistemic.journeys.collection_replay import fixture_payload
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus
    from epistemic.runner import evaluate_scenario
    from epistemic.schema import Scenario, ScenarioLoadError, load_scenario_text
    from protocol.contracts import AmendmentAcknowledgmentPendingError

    assert registry.AMENDMENT_INTRODUCED_FAMILIES["f28"] == 4
    assert registry.AMENDMENT_INTRODUCED_FAMILIES["f29"] == 4
    assert {"f28", "f29"} <= amendments.withheld_family_ids()
    payload = fixture_payload(routing_corpus())
    with pytest.raises(ScenarioLoadError, match="4") as refusal:
        load_scenario_text(json.dumps(payload), source="fixture")
    assert isinstance(refusal.value.__cause__, AmendmentAcknowledgmentPendingError)
    scenario = Scenario.model_validate_json(json.dumps(payload))
    with pytest.raises(AmendmentAcknowledgmentPendingError, match="4"):
        evaluate_scenario(scenario, snapshots={})


def test_projector_preserves_claims_exact_fields_and_candidate_targets(tmp_path) -> None:
    from types import SimpleNamespace

    import yaml
    from epistemic.projectors.exomem_vault import VaultProjector, _audit_finding_items

    root = tmp_path / "Knowledge Base/Records/Studio"
    root.mkdir(parents=True)
    manifest = {
        "type": "collection",
        "exomem_id": "studio",
        "semantic_profile": "records",
        "schema_version": 1,
        "claims": {"terms": ["bulletin", "studio"]},
        "item_schema": {
            "natural_key": ["post_key"],
            "fields": {
                "post_key": {"type": "string"},
                "exact_text": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "link"}},
            },
        },
    }
    item = {
        "type": "record",
        "collection_id": "studio",
        "record_id": "first",
        "post_key": "first",
        "exact_text": "First line\nSecond line",
        "sources": ["proof.md"],
    }
    for name, data in (("_collection.md", manifest), ("first.md", item)):
        (root / name).write_text("---\n" + yaml.safe_dump(data) + "---\n")
    snapshot = VaultProjector(tmp_path).project(phase="fixture", taken_at="2026-09-08T12:00:00Z")
    assert snapshot.collections[0].claims == {"terms": ("bulletin", "studio")}
    assert snapshot.collections[0].items[0].values["exact_text"] == item["exact_text"]
    assert snapshot.collections[0].items[0].values["sources"] == ["proof.md"]
    assert snapshot.collections[0].items[0].locator == "Knowledge Base/Records/Studio/first.md"
    assert "collection_id" not in snapshot.collections[0].items[0].values
    report = SimpleNamespace(
        summary={"collection_candidate": 1},
        findings=[
            SimpleNamespace(
                category="collection_candidate",
                path="context.md",
                detail="Recurring licences",
                meta={"domain_terms": ["licences"], "evidence_units": ["context.md#purchase"]},
            )
        ],
    )
    signal = _audit_finding_items(report)[1]
    assert signal.raw["signal_class"] == "collection_candidate"
    assert signal.raw["targets"] == "licences"
    assert "context.md#purchase" in signal.raw["evidence_units"]


@pytest.mark.parametrize("word", ["collection", "ledger", "schema", "claims", "tracking"])
def test_sequence_four_loader_checks_store_words_after_amendment_gate(monkeypatch, word) -> None:
    import json

    from epistemic import schema
    from epistemic.journeys.collection_replay import fixture_payload
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus

    monkeypatch.setattr(schema, "require_family_released", lambda _: None)
    payload = fixture_payload(routing_corpus())
    payload["phases"][0]["ops"][2]["detail"] = f"Please make a {word}."
    with pytest.raises(schema.ScenarioLoadError, match="store-bearing"):
        schema.load_scenario_text(json.dumps(payload), source="fixture")


def test_development_driver_dry_run_writes_nothing_and_pins_each_arm(tmp_path, monkeypatch) -> None:
    from epistemic.journeys.collection_replay_driver import run_development
    from epistemic.journeys.f27_replay import AgentEnvelope
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus

    # No real client starts in these unit probes; the shared client's ancestor
    # guard has its own tests and the host may put pytest's /tmp under a repo.
    monkeypatch.setattr("epistemic.journeys.f27_replay.refuse_unsafe_out_dir", lambda path: path)

    out = tmp_path / "not-created"
    result = run_development(
        routing_corpus(),
        out_dir=out,
        taken_at="2026-09-08T12:00:00Z",
        envelope=AgentEnvelope(Path("/bin/false"), "fixture"),
        dry_run=True,
        parent_env={"PATH": "/bin", "EXOMEM_STATE_ROOT": "/real-state"},
    )
    assert not out.exists()
    assert result["claim_status"] == "development-only"
    for arm in result["arms"]:
        assert arm["env"]["EXOMEM_STATE_ROOT"].startswith(str(out))
        assert arm["env"]["EXOMEM_VAULT_PATH"].startswith(str(out))
        assert len(arm["argv"]) == len(routing_corpus().turns)


def test_development_driver_fault_blocks_arm_without_scoring(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from epistemic.journeys.collection_replay_driver import run_development
    from epistemic.journeys.f27_replay import AgentEnvelope
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus

    monkeypatch.setattr("epistemic.journeys.f27_replay.refuse_unsafe_out_dir", lambda path: path)

    calls = []

    def factory(ctx):
        calls.append(ctx)
        return lambda _: SimpleNamespace(returncode=1, stdout="", stderr="not logged in")

    class Projector:
        def __init__(self, _root):
            pass

        def project(self, *, phase, taken_at):
            return _snapshot(phase=phase)

    result = run_development(
        routing_corpus(),
        out_dir=tmp_path / "run",
        taken_at="2026-09-08T12:00:00Z",
        envelope=AgentEnvelope(Path("/bin/false"), "fixture"),
        arm_ids=("hookless",),
        runner_factory=factory,
        seed=lambda *_: None,
        projector_factory=Projector,
        prominence_writer=lambda *_: None,
        parent_env={"PATH": "/bin"},
    )
    assert len(calls) == 1
    arm = result["arms"][0]
    assert arm["harness_fault"] and "not logged in" in arm["reason"]
    assert arm["outcome"] == "blocked"
    assert "scores" not in result and "scores" not in arm


@pytest.mark.parametrize(
    "stdout,complete",
    [
        ("", False),
        ('{"type":"system","subtype":"init"}', False),
        (
            '{"type":"result","subtype":"success","is_error":false,"result":"No action needed."}',
            True,
        ),
    ],
)
def test_development_driver_requires_completed_client_turn(
    tmp_path, monkeypatch, stdout, complete
) -> None:
    from types import SimpleNamespace

    from epistemic.journeys.collection_replay_driver import run_development
    from epistemic.journeys.f27_replay import AgentEnvelope
    from epistemic.journeys.f29_claimed_routing_replay import routing_corpus

    monkeypatch.setattr("epistemic.journeys.f27_replay.refuse_unsafe_out_dir", lambda path: path)

    class Projector:
        def __init__(self, _root):
            pass

        def project(self, *, phase, taken_at):
            return _snapshot(phase=phase)

    result = run_development(
        routing_corpus(),
        out_dir=tmp_path / "run",
        taken_at="2026-09-08T12:00:00Z",
        envelope=AgentEnvelope(Path("/bin/false"), "fixture"),
        arm_ids=("hookless",),
        runner_factory=lambda _: lambda _: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
        seed=lambda *_: None,
        projector_factory=Projector,
        prominence_writer=lambda *_: None,
        parent_env={"PATH": "/bin"},
    )
    arm = result["arms"][0]
    assert arm["harness_fault"] is not complete
    assert arm["outcome"] == ("observed" if complete else "blocked")
    assert arm["turns_executed"] == (len(routing_corpus().turns) if complete else 0)
