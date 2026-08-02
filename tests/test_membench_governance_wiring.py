"""Governance wiring: PolicySet → `_Governance/` translation, persona threading,
and three-state reporting (wired / default-open-labelled / unsupported).

Spec: openspec/changes/expand-memory-proof-benchmark/specs/memory-proof-harness
— Requirement "Governed Views Are Wired, Not Simulated". The exomem adapter
translates the corpus policy through PUBLIC product surfaces (documented
`_Governance/` YAML + the canonical principal mapping); the harness never
simulates governance. Retrieval probes use source TITLES (the established
lexical-profile pattern from test_membench_adapter_exomem): natural-language
prompts honestly return zero hits in the model-free profile, and a zero-hit
withhold would prove nothing — the title probe makes the persona the ONLY
variable between the two searches.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from membench.adapters.base import AdapterEnvironmentError, Capability
from membench.adapters.exomem_local import ExomemLocalAdapter, lexical_profile
from membench.cli import main as cli_main
from membench.clock import end_of_window
from membench.generate import generate_corpus
from membench.native import exomem_kb, load_corpus_view
from membench.reporting import build_comparison_report
from membench.runner import RunSpec, execute_run
from membench.schema import ClaimRecord, ExpectedRecord, PolicySet, QueryRecord, load_jsonl
from membench.scoring import GateStatus, ScoringContext, evaluate
from membench.scoring.extractive import build_answer

T16 = "t16_governance_audiences"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("corpus") / "s1"
    generate_corpus(1, root, template_ids=[T16])
    return root


def _load_records(corpus: Path) -> tuple[list[QueryRecord], dict[str, ExpectedRecord]]:
    queries = load_jsonl(QueryRecord, corpus / "queries.jsonl")
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, corpus / "expected.jsonl")}
    return queries, expected


def _dropped_rule_expectations(corpus: Path) -> tuple[set[str], dict[str, set[str]]]:
    """Independently derive (dropped rule ids, affected query ids → rule ids).

    A corpus rule whose ``declassify_at`` falls inside the knowledge horizon
    cannot be represented by exomem's time-free policy schema; queries whose
    forbidden expectations trace to such a rule's targets are unmeasurable
    under wired governance. Computed straight from the corpus records so the
    test pins the behaviour, not the implementation's own join.
    """

    policy = PolicySet.model_validate(
        yaml.safe_load((corpus / "policies.yaml").read_text(encoding="utf-8")) or {}
    )
    claims = load_jsonl(ClaimRecord, corpus / "claims.jsonl")
    horizon = end_of_window()
    dropped_ids: set[str] = set()
    affected: dict[str, set[str]] = {}
    _, expected = _load_records(corpus)
    for rule in policy.rules:
        if rule.declassify_at is None or horizon < rule.declassify_at:
            continue
        dropped_ids.add(rule.rule_id)
        covered_claims = set(rule.target_claims)
        covered_values: set[str] = set()
        for claim in claims:
            if claim.claim_id in covered_claims or any(
                assertion.source_id in rule.target_sources
                for assertion in claim.assertions
            ):
                covered_claims.add(claim.claim_id)
                covered_values.add(claim.object.value)
        for query_id, exp in expected.items():
            if set(exp.forbidden_claims) & covered_claims or (
                set(exp.forbidden_disclosures) & covered_values
            ):
                affected.setdefault(query_id, set()).add(rule.rule_id)
    return dropped_ids, affected


def _restricted_owner_pair(
    queries: list[QueryRecord], expected: dict[str, ExpectedRecord]
) -> tuple[QueryRecord, ExpectedRecord, QueryRecord, ExpectedRecord, str]:
    """The audience-restricted query and the owner query about the same value."""

    for restricted in queries:
        if restricted.persona == "owner":
            continue
        exp_restricted = expected[restricted.query_id]
        if not exp_restricted.abstain or not exp_restricted.forbidden_disclosures:
            continue
        forbidden = exp_restricted.forbidden_disclosures[0]
        for owner in queries:
            if owner.persona != "owner":
                continue
            exp_owner = expected[owner.query_id]
            if forbidden in exp_owner.answer.values:
                return restricted, exp_restricted, owner, exp_owner, forbidden
    raise AssertionError("t16 corpus lost its restricted/owner query pair")


def _gate(items, name: str):
    for item in items:
        if item.gate == name:
            return item
    raise AssertionError(f"gate {name} missing from {items}")


def test_wired_restricted_persona_withholds_while_owner_sees(
    corpus: Path, tmp_path: Path
) -> None:
    """Scenario: restricted claim withheld for the restricted persona.

    Same query string, wiring active: the restricted persona's retrieved
    context and extractive answer contain no forbidden value and the no-leak
    gate passes with a withhold observed (the expected abstention actually
    happens); the owner persona passes with the value present.
    """

    view = load_corpus_view(corpus)
    native_dir = tmp_path / "native"
    exomem_kb.render(view, native_dir)
    queries, expected = _load_records(corpus)
    restricted_q, restricted_exp, owner_q, owner_exp, forbidden = _restricted_owner_pair(
        queries, expected
    )
    probe_source = next(
        source for source in view.sources if forbidden in view.source_text(source)
    )
    ctx = ScoringContext(
        claims_by_id={c.claim_id: c for c in view.claims},
        sources_by_id={s.source_id: s for s in view.sources},
    )

    adapter = ExomemLocalAdapter(mode="leaf", governance="wired")
    assert Capability.GOVERNED_VIEWS in adapter.capabilities()
    assert adapter.governance_state == "wired"
    adapter.setup(tmp_path / "provider", lexical_profile())
    try:
        results = adapter.ingest(corpus, native_dir)
        assert results and all(r.ok for r in results), [
            r.detail for r in results if not r.ok
        ]

        owner_hits = adapter.search(probe_source.title, 10, persona=owner_q.persona)
        assert owner_hits, "owner persona must retrieve the governed source"
        assert any(forbidden in (hit.text or "") for hit in owner_hits)

        restricted_hits = adapter.search(
            probe_source.title, 10, persona=restricted_q.persona
        )
        for hit in restricted_hits:  # retrieved context carries no forbidden value
            assert forbidden not in (hit.text or "")
            assert forbidden not in (hit.excerpt or "")
        assert not restricted_hits, (
            "the governed source must be withheld from the restricted persona"
        )

        restricted_answer = build_answer(restricted_q, restricted_hits)
        assert forbidden not in restricted_answer.answer_text
        assert restricted_answer.abstained, "withhold must be observed"
        restricted_items = evaluate(restricted_q, restricted_exp, restricted_answer, ctx)
        assert _gate(restricted_items, "no_leak").status is GateStatus.PASS
        assert _gate(restricted_items, "abstention").status is GateStatus.PASS

        owner_answer = build_answer(owner_q, owner_hits)
        assert forbidden in owner_answer.answer_text
        owner_items = evaluate(owner_q, owner_exp, owner_answer, ctx)
        assert _gate(owner_items, "value").status is GateStatus.PASS
        assert _gate(owner_items, "no_leak").status is not GateStatus.FAIL
    finally:
        adapter.cleanup()


def test_wired_run_records_state_and_withholds_through_runner(
    corpus: Path, tmp_path: Path
) -> None:
    """The runner threads query personas and records governance_state=wired."""

    result = execute_run(
        RunSpec(
            corpus_dir=corpus,
            adapter=ExomemLocalAdapter(mode="leaf", governance="wired"),
            profile=lexical_profile(),
            runs_root=tmp_path / "runs",
            top_k=10,
        )
    )
    assert not result.invalid, result.invalid_reason
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["governance_state"] == "wired"
    scores = json.loads((result.run_dir / "deterministic-scores.json").read_text())
    assert scores["governance_state"] == "wired"

    queries, expected = _load_records(corpus)
    _, dropped_affected = _dropped_rule_expectations(corpus)
    withhold_ids = {
        q.query_id
        for q in queries
        if expected[q.query_id].abstain and expected[q.query_id].forbidden_disclosures
    }
    assert withhold_ids
    forbidden_by_id = {
        query_id: expected[query_id].forbidden_disclosures for query_id in withhold_ids
    }
    for raw in (result.run_dir / "retrieval.jsonl").read_text().splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        if row["query_id"] not in withhold_ids:
            continue
        for hit in row["hits"]:
            for forbidden in forbidden_by_id[row["query_id"]]:
                assert forbidden not in (hit.get("excerpt") or "")
                assert forbidden not in (hit.get("title") or "")
    per_query = {row.get("query_id"): row for row in scores["per_query"]}
    enforceable = withhold_ids - set(dropped_affected)
    assert enforceable, "every withhold query traced to a dropped rule"
    for query_id in enforceable:
        gates = {g["gate"]: g["status"] for g in per_query[query_id]["gates"]}
        assert gates["no_leak"] == "pass", (query_id, gates)
        assert gates["abstention"] == "pass", (query_id, gates)


def test_wired_run_reports_dropped_rules_and_marks_them_unsupported(
    corpus: Path, tmp_path: Path
) -> None:
    """Time-conditioned corpus rules are a reported divergence, never a score.

    Exomem policy v1 has no time-conditioned rules, so a rule declassified
    inside the knowledge horizon is dropped by the translation. The drop must
    be disclosed in a translation report inside the run dir, and every query
    whose withhold expectation traces to a dropped rule must have its
    no_leak/abstention gate items UNSUPPORTED (unsupported-never-zero) with
    evidence naming the rule — never scored pass or fail against a vault the
    translation could not make faithful.
    """

    dropped_ids, affected = _dropped_rule_expectations(corpus)
    assert dropped_ids, "t16 must carry a declassify_at rule inside the horizon"
    assert affected, "t16 must query the pre-declassification withhold"

    result = execute_run(
        RunSpec(
            corpus_dir=corpus,
            adapter=ExomemLocalAdapter(mode="leaf", governance="wired"),
            profile=lexical_profile(),
            runs_root=tmp_path / "runs",
            top_k=10,
        )
    )
    assert not result.invalid, result.invalid_reason

    translation = json.loads(
        (result.run_dir / "governance-translation.json").read_text(encoding="utf-8")
    )
    assert translation["documents_authored"], "wired run authored no policy documents"
    reported = {entry["rule_id"]: entry for entry in translation["dropped_rules"]}
    assert set(reported) == dropped_ids
    for entry in reported.values():
        assert entry["declassify_at"]
        assert entry["target_sources"] or entry["target_claims"]
        assert "no time-conditioned rules" in entry["reason"]

    scores = json.loads((result.run_dir / "deterministic-scores.json").read_text())
    per_query = {row.get("query_id"): row for row in scores["per_query"]}
    for query_id, rule_ids in affected.items():
        gates = {g["gate"]: g for g in per_query[query_id]["gates"]}
        for gate_name in ("no_leak", "abstention"):
            item = gates[gate_name]
            assert item["status"] == "unsupported", (query_id, gate_name, item)
            assert any(rule_id in (item["evidence"] or "") for rule_id in rule_ids), item


def test_malformed_policy_set_is_an_environment_fault(
    corpus: Path, tmp_path: Path
) -> None:
    """A malformed policies.yaml invalidates the run — never a raw crash and
    never a silently ungoverned 'wired' measurement."""

    broken = tmp_path / "broken-corpus"
    shutil.copytree(corpus, broken)
    (broken / "policies.yaml").write_text("audiences: 5\n", encoding="utf-8")
    view = load_corpus_view(broken)
    native_dir = tmp_path / "native"
    exomem_kb.render(view, native_dir)

    adapter = ExomemLocalAdapter(mode="leaf", governance="wired")
    adapter.setup(tmp_path / "provider", lexical_profile())
    try:
        with pytest.raises(AdapterEnvironmentError):
            adapter.ingest(broken, native_dir)
    finally:
        adapter.cleanup()


def test_cli_run_exposes_reproducible_governance_switch(
    corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`membench run --governance wired` is the documented wired entry point;
    providers other than exomem-local never receive the kwarg."""

    runs_root = tmp_path / "runs"
    rc = cli_main(
        [
            "run",
            "--corpus",
            str(corpus),
            "--runs-root",
            str(runs_root),
            "--governance",
            "wired",
            "--label",
            "cli-wired",
        ]
    )
    assert rc == 0, capsys.readouterr().out
    run_dirs = [p for p in runs_root.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    manifest = json.loads((run_dirs[0] / "manifest.json").read_text())
    assert manifest["governance_state"] == "wired"

    rc = cli_main(
        [
            "run",
            "--corpus",
            str(corpus),
            "--runs-root",
            str(tmp_path / "runs-other"),
            "--provider",
            "basic-memory-local",
            "--governance",
            "wired",
        ]
    )
    assert rc == 2, "wired governance for a non-exomem provider must refuse"


def test_ungoverned_run_is_labelled_default_open_and_excluded(
    corpus: Path, tmp_path: Path
) -> None:
    """Scenario: ungoverned measurement is labelled.

    Without wiring, the run manifest and deterministic scores carry the
    default-open label and the comparison report excludes the governance
    dimension from comparative counts for that run.
    """

    result = execute_run(
        RunSpec(
            corpus_dir=corpus,
            adapter=ExomemLocalAdapter(mode="leaf"),
            profile=lexical_profile(),
            runs_root=tmp_path / "runs",
            top_k=10,
        )
    )
    assert not result.invalid, result.invalid_reason
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["governance_state"] == "default_open"
    scores = json.loads((result.run_dir / "deterministic-scores.json").read_text())
    assert scores["governance_state"] == "default_open"
    assert "governance" in scores["dimensions"], "t16 must exercise the governance gate"

    # Every per-query row is family-tagged so reporting can exclude
    # governance-FAMILY rows (all their gate items) from comparative tables.
    assert scores["per_query"]
    for row in scores["per_query"]:
        assert row["family"] == "governance", row

    report_path = build_comparison_report([result.run_dir], tmp_path / "comparison.md")
    report = report_path.read_text(encoding="utf-8")
    governance_rows = [
        line
        for line in report.splitlines()
        if line.startswith("| governance |")
    ]
    assert governance_rows, "governance dimension row missing from the report"
    for row in governance_rows:
        assert "default-open" in row, row
        assert "pass=" not in row, f"labelled run leaked comparative counts: {row}"
    # Abstention-escape guard: on a governance-only corpus, EVERY dimension's
    # items come from governance-family rows, so a non-wired run renders the
    # default-open label across the board — its vacuous abstention/temporal
    # passes never sit in a comparative cell.
    for dimension in ("abstention", "factual_qa", "temporal"):
        rows = [
            line
            for line in report.splitlines()
            if line.startswith(f"| {dimension} |")
        ]
        assert rows, f"{dimension} row missing"
        for row in rows:
            assert "default-open" in row, row
            assert "pass=" not in row, (
                f"governance-family rows leaked into comparative {dimension}: {row}"
            )


def test_wire_mode_cannot_claim_governance_wiring() -> None:
    """Persona threading has no public identity seam on the in-process wire
    surface; requesting wiring there is a configuration error, never a silent
    default-open downgrade."""

    with pytest.raises(ValueError):
        ExomemLocalAdapter(mode="wire", governance="wired")
