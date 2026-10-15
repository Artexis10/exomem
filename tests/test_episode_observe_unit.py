"""Episode unit corrections reuse observation guards and same-batch witnesses."""

from pathlib import Path

import pytest
from test_episode_workflow import _candidate, _proposal, _record, _reviewed, _workflow
from test_observe_memory import PAGE, _page_hash, _page_source, _write_page

from exomem import commands, compiled_history, curation, episode_workflow, semantic_index
from exomem.governance.principal import owner_principal, request_scope


def test_episode_corrects_one_claim_without_replacing_its_parent(vault: Path, monkeypatch) -> None:
    tmp_path = vault
    monkeypatch.setenv(episode_workflow.ENABLE_ENV, "1")
    _write_page(
        tmp_path, source=_page_source("## Claim\n- category: rule\n- id: stable\n\nFirst wording.")
    )
    with request_scope(owner_principal(surface="mcp")):
        unit = semantic_index.current_parent_index_state(tmp_path, PAGE).document.units[0]
        _record(tmp_path)
        proposal = _proposal(
            "semantic_unit",
            [
                {
                    "leaf_key": "correct",
                    "effect_revision": 1,
                    "kind": "observe-unit",
                    "args": {
                        "path": PAGE,
                        "why": "The original source narrowed the rule.",
                        "observation": {
                            "unit_ref": unit.unit_ref,
                            "expected_fingerprint": unit.fingerprint,
                            "expected_hash": _page_hash(tmp_path / PAGE),
                            "kind": "claim",
                            "category": "rule",
                            "content": "The rule applies only to calibrated probes.",
                            "verdict": "qualified",
                        },
                    },
                }
            ],
            target=PAGE,
        )
        prepared = _workflow(tmp_path, action="prepare", candidate="correction", proposal=proposal)
        leaf = _candidate(prepared, "correction")["leaves"][0]
        plan = curation.CurationStore(tmp_path).load_plan(leaf["run_id"])
        with pytest.raises(curation.CurationError, match="INVALID_STEP_KIND"):
            curation.validate_forward_plan(
                {"version": 1, "title": plan["title"], "steps": plan["steps"]}
            )
        _workflow(
            tmp_path,
            action="disposition",
            candidate="correction",
            disposition="routed",
            reason="Correct the existing claim.",
        )
        resumed = _workflow(
            tmp_path, action="resume", input_revision=1, journal_digest=_reviewed(tmp_path)
        )
        assert _candidate(resumed, "correction")["leaves"][0]["outcome"] == "committed"
        current = commands.op_read_memory(
            tmp_path, path=PAGE, unit_ref=unit.unit_ref, include_history=True
        )
        assert current["unit"]["verdict"] == "qualified"
        assert current["revision_history"]["returned"] == 1
        replay = _workflow(
            tmp_path, action="resume", input_revision=1, journal_digest=_reviewed(tmp_path)
        )
        assert _candidate(replay, "correction")["leaves"][0]["outcome"] == "committed"
        assert compiled_history.read(tmp_path, path=PAGE)["returned"] == 1


def test_exact_correction_rejects_a_target_that_changed_after_the_agent_read_it(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        source=_page_source(
            "## Claim\n- category: rule\n- id: old\n\nThe probe uses the old interval.\n\n"
            "## Claim\n- category: rule\n- id: corrective\n\nThe probe uses the calibrated interval."
        ),
    )
    with request_scope(owner_principal(surface="mcp")):
        state = semantic_index.current_parent_index_state(tmp_path, PAGE)
        old, corrective = state.document.units
        with pytest.raises(ValueError, match="STALE_UNIT_TARGET"):
            commands.op_observe_memory(
                tmp_path,
                path=PAGE,
                operation="update",
                unit_ref=corrective.unit_ref,
                expected_fingerprint=corrective.fingerprint,
                expected_hash=_page_hash(tmp_path / PAGE),
                kind="claim",
                category="rule",
                content=corrective.content,
                relations=[{"kind": "contradicts", "target": "[[#old]]"}],
                target_fingerprints={old.unit_ref: "0" * 64},
            )
        result = commands.op_observe_memory(
            tmp_path,
            path=PAGE,
            operation="update",
            unit_ref=corrective.unit_ref,
            expected_fingerprint=corrective.fingerprint,
            expected_hash=_page_hash(tmp_path / PAGE),
            kind="claim",
            category="rule",
            content=corrective.content,
            relations=[{"kind": "contradicts", "target": "[[#old]]"}],
            target_fingerprints={old.unit_ref: old.fingerprint},
        )
        assert result["mutated"] is True


def test_cross_page_target_drift_aborts_the_correction_and_its_history(
    tmp_path: Path, monkeypatch
) -> None:
    from exomem import semantic_writes

    target_path = "Knowledge Base/Notes/Insights/probe-source.md"
    target = _write_page(
        tmp_path,
        rel=target_path,
        source=_page_source(
            "## Claim\n- category: rule\n- id: old\n\nThe original interval."
        ).replace("00000000-0000-4000-8000-000000000081", "00000000-0000-4000-8000-000000000084"),
    )
    page = _write_page(
        tmp_path,
        source=_page_source("## Claim\n- category: rule\n- id: corrective\n\nA revised interval."),
    )
    before = page.read_bytes()
    with request_scope(owner_principal(surface="mcp")):
        old = semantic_index.current_parent_index_state(tmp_path, target_path).document.units[0]
        correction = semantic_index.current_parent_index_state(tmp_path, PAGE).document.units[0]
        commit = semantic_writes.commit_existing

        def drift(root, **kwargs):
            target.write_text(
                target.read_text().replace("The original interval.", "A newly measured interval.")
            )
            return commit(root, **kwargs)

        monkeypatch.setattr(semantic_writes, "commit_existing", drift)
        with pytest.raises(ValueError, match="STALE_SEMANTIC_WRITE"):
            commands.op_observe_memory(
                tmp_path,
                path=PAGE,
                operation="update",
                unit_ref=correction.unit_ref,
                expected_fingerprint=correction.fingerprint,
                expected_hash=_page_hash(page),
                kind="claim",
                category="rule",
                content=correction.content,
                relations=[{"kind": "contradicts", "target": f"[[{target_path}#old]]"}],
                target_fingerprints={old.unit_ref: old.fingerprint},
            )
        assert page.read_bytes() == before
        assert compiled_history.read(tmp_path, path=PAGE)["returned"] == 0
