"""reconcile: heal index-count + embedding drift from out-of-band edits.

reconcile is the focused "I edited around the system, fix it" command —
recompute index counts + incrementally refresh stale embeddings + report
remaining drift, without audit_fix's wikilink/frontmatter rewrites.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    activation_manifest,
    commands,
    index_sync,
    relation_review,
    semantic_contract,
)
from exomem import audit as audit_module
from exomem import reconcile as reconcile_module
from exomem import (
    vault as vault_module,
)

_LIFECYCLE_IDS = {
    "pending": "00000000-0000-4000-8000-000000000211",
    "committed": "00000000-0000-4000-8000-000000000212",
    "stale": "00000000-0000-4000-8000-000000000213",
    "trashed_committed": "00000000-0000-4000-8000-000000000214",
}


def _semantic_page(page_id: str, *, status: str = "active") -> str:
    return (
        "---\n"
        "title: Reconcile semantic\n"
        "type: insight\n"
        f"status: {status}\n"
        f"exomem_id: {page_id}\n"
        "---\n\n"
        "# Reconcile semantic\n\n"
        "- [config] Direct editor content.\n\n"
        "## Relations\n"
    )


def _install_lifecycle_slot(
    root: Path,
    *,
    state: str,
    page_id: str,
    path: str,
) -> tuple[Path, Path, str, str]:
    before = _semantic_page(page_id).replace(
        "Direct editor content.", "Before lifecycle content."
    )
    after = _semantic_page(page_id).replace(
        "Direct editor content.", "After lifecycle content."
    )
    current = {
        "pending": before,
        "committed": after,
        "stale": _semantic_page(page_id).replace(
            "Direct editor content.", "Unrelated lifecycle content."
        ),
        "trashed_committed": after,
    }[state]
    page = root / path
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(current, encoding="utf-8")
    decision = relation_review.build_lifecycle_decision(
        page_identity=page_id,
        after_fingerprint=semantic_contract.review_content_fingerprint(
            page_id, after
        ),
        reason="No honest relation exists",
    )
    prepared = relation_review.build_lifecycle_prepared_transition(
        transition_id=page_id[:-3] + "9" + page_id[-2:],
        operation="edit",
        page_identity=page_id,
        before_path=path,
        before_source_hash=vault_module.content_hash(before),
        after_path=path,
        after_source_hash=vault_module.content_hash(after),
        after_fingerprint=decision.after_fingerprint,
        decision=decision,
        transition_token=f"reconcile-{state}",
        auxiliary_hash=hashlib.sha256(b"reconcile auxiliaries").hexdigest(),
    )
    decision_path = relation_review.lifecycle_decision_path(
        root, page_id, decision.after_fingerprint
    )
    prepared_path = relation_review.lifecycle_prepared_path(root, page_id)
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(
        relation_review.serialize_lifecycle_decision(decision), encoding="utf-8"
    )
    prepared_path.write_text(
        relation_review.serialize_lifecycle_prepared(prepared), encoding="utf-8"
    )
    return page, prepared_path, before, after


def _trash_exact_committed_page(
    root: Path,
    *,
    page: Path,
    original_path: str,
    page_id: str,
) -> tuple[Path, Path]:
    trash = root / "Knowledge Base/_trash/2026-07-15/120000-lifecycle.md"
    trash.parent.mkdir(parents=True, exist_ok=True)
    page.replace(trash)
    sidecar = trash.with_name(f"{trash.name}.meta.json")
    sidecar.write_text(
        json.dumps(
            {
                "original_path": original_path,
                "frontmatter_snapshot": {"exomem_id": page_id},
            }
        ),
        encoding="utf-8",
    )
    return trash, sidecar


def test_reconcile_dry_run_reports_semantic_drift_without_writing_manifest_or_markdown(
    tmp_path: Path,
) -> None:
    page = tmp_path / "Knowledge Base/Notes/Insights/direct-current.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        _semantic_page("00000000-0000-4000-8000-000000000201"),
        encoding="utf-8",
    )
    original = page.read_bytes()
    manifest = activation_manifest.manifest_path(tmp_path)

    report = reconcile_module.reconcile(tmp_path, dry_run=True)
    payload = report.as_dict()

    assert page.read_bytes() == original
    assert not manifest.exists()
    assert payload["semantic_activation"] == "prospective"
    assert {
        finding["code"] for finding in payload["semantic_contract_findings"]
    } == {"RELATION_DISPOSITION_MISSING"}
    assert payload["semantic_contract_summary"] == {
        "RELATION_DISPOSITION_MISSING": 1
    }
    assert "semantic_contract_findings" not in {
        finding.get("category") for finding in payload["remaining_drift"]
    }
    assert payload["semantic_contract_omitted_counts"] == {
        "evaluated_paths": 0,
        "semantic_contract_findings": 0,
        "semantic_contract_summary": 0,
    }
    assert payload["semantic_contract_truncation"]["byte_budget"] == 120 * 1024


def test_reconcile_marks_direct_post_activation_page_current_and_not_grandfathered(
    tmp_path: Path,
) -> None:
    (tmp_path / "Knowledge Base").mkdir(parents=True)
    activation_manifest.ensure_manifest(tmp_path)
    page = tmp_path / "Knowledge Base/Notes/Insights/direct-new.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        _semantic_page("00000000-0000-4000-8000-000000000202"),
        encoding="utf-8",
    )

    payload = reconcile_module.reconcile(tmp_path, dry_run=True).as_dict()

    assert payload["semantic_activation"] == "current"
    assert payload["semantic_contract_findings"][0]["grandfathered"] is False


def test_reconcile_does_not_report_relation_disposition_for_inactive_external_draft(
    tmp_path: Path,
) -> None:
    page = tmp_path / "Knowledge Base/Notes/Insights/direct-draft.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        _semantic_page(
            "00000000-0000-4000-8000-000000000203", status="draft"
        ),
        encoding="utf-8",
    )

    payload = reconcile_module.reconcile(tmp_path, dry_run=True).as_dict()

    assert payload["semantic_contract_findings"] == []


def test_reconcile_recomputes_and_clears_repaired_semantic_finding(
    tmp_path: Path,
) -> None:
    page = tmp_path / "Knowledge Base/Notes/Insights/repaired.md"
    target = tmp_path / "Knowledge Base/Notes/Insights/reconcile-target.md"
    anchor = tmp_path / "Knowledge Base/Notes/Insights/reconcile-anchor.md"
    page.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    source = _semantic_page("00000000-0000-4000-8000-000000000204")
    page.write_text(source, encoding="utf-8")
    target.write_text(
        _semantic_page("00000000-0000-4000-8000-000000000205").replace(
            "## Relations\n",
            "## Relations\n"
            "- supports [[Knowledge Base/Notes/Insights/reconcile-anchor]]\n",
        ),
        encoding="utf-8",
    )
    anchor.write_text(
        _semantic_page("00000000-0000-4000-8000-000000000206").replace(
            "## Relations\n",
            "## Relations\n"
            "- supports [[Knowledge Base/Notes/Insights/reconcile-target]]\n",
        ),
        encoding="utf-8",
    )

    before = reconcile_module.reconcile(tmp_path, dry_run=True).as_dict()
    page.write_text(
        source.replace(
            "## Relations\n",
            "## Relations\n"
            "- supports [[Knowledge Base/Notes/Insights/reconcile-target]]\n",
        ),
        encoding="utf-8",
    )
    after = reconcile_module.reconcile(tmp_path, dry_run=True).as_dict()

    relative = page.relative_to(tmp_path).as_posix()
    assert any(
        finding["path"] == relative
        and finding["code"] == "RELATION_DISPOSITION_MISSING"
        for finding in before["semantic_contract_findings"]
    )
    assert not any(
        finding["path"] == relative
        for finding in after["semantic_contract_findings"]
    )


def test_reconcile_classifies_prepared_slots_and_only_cleans_stale_in_write_mode(
    tmp_path: Path,
) -> None:
    installed: dict[str, tuple[Path, Path, bytes, bytes]] = {}
    for state in ("pending", "committed", "stale"):
        page_id = _LIFECYCLE_IDS[state]
        page, prepared, _, _ = _install_lifecycle_slot(
            tmp_path,
            state=state,
            page_id=page_id,
            path=f"Knowledge Base/Notes/Insights/{state}.md",
        )
        decision = next(
            child for child in prepared.parent.iterdir() if child != prepared
        )
        installed[state] = (
            page,
            prepared,
            page.read_bytes(),
            decision.read_bytes(),
        )

    dry = reconcile_module.reconcile(tmp_path, dry_run=True).as_dict()

    assert {
        item["page_identity"]: item["state"]
        for item in dry["lifecycle_prepared"]
    } == {
        _LIFECYCLE_IDS["pending"]: "pending",
        _LIFECYCLE_IDS["committed"]: "committed",
        _LIFECYCLE_IDS["stale"]: "stale",
    }
    assert dry["lifecycle_prepared_summary"] == {
        "committed": 1,
        "pending": 1,
        "stale": 1,
        "trashed_committed": 0,
    }
    assert dry["lifecycle_prepared_cleaned"] == []
    for page, prepared, page_bytes, decision_bytes in installed.values():
        assert page.read_bytes() == page_bytes
        assert prepared.exists()
        decision = next(child for child in prepared.parent.iterdir() if child != prepared)
        assert decision.read_bytes() == decision_bytes

    written = reconcile_module.reconcile(tmp_path).as_dict()

    assert written["lifecycle_prepared_cleaned"] == [
        relation_review.lifecycle_prepared_path(
            tmp_path, _LIFECYCLE_IDS["stale"]
        ).relative_to(tmp_path).as_posix()
    ]
    for state, (page, prepared, page_bytes, decision_bytes) in installed.items():
        assert page.read_bytes() == page_bytes
        assert prepared.exists() is (state != "stale")
        decision = next(
            child for child in prepared.parent.iterdir() if child.name != "prepared.json"
        )
        assert decision.read_bytes() == decision_bytes

    repeated = reconcile_module.reconcile(tmp_path).as_dict()
    assert repeated["lifecycle_prepared_cleaned"] == []
    assert not any(
        item["page_identity"] == _LIFECYCLE_IDS["stale"]
        for item in repeated["lifecycle_prepared"]
    )


def test_reconcile_preserves_exact_trashed_committed_slot(tmp_path: Path) -> None:
    page_id = _LIFECYCLE_IDS["trashed_committed"]
    original_path = "Knowledge Base/Notes/Insights/trashed-committed.md"
    page, prepared, _, _ = _install_lifecycle_slot(
        tmp_path,
        state="trashed_committed",
        page_id=page_id,
        path=original_path,
    )
    trash, sidecar = _trash_exact_committed_page(
        tmp_path,
        page=page,
        original_path=original_path,
        page_id=page_id,
    )
    prepared_bytes = prepared.read_bytes()
    trash_bytes = trash.read_bytes()
    sidecar_bytes = sidecar.read_bytes()

    dry = reconcile_module.reconcile(tmp_path, dry_run=True).as_dict()
    written = reconcile_module.reconcile(tmp_path).as_dict()

    assert dry["lifecycle_prepared"] == [
        {
            "page_identity": page_id,
            "state": "trashed_committed",
            "reference": prepared.relative_to(tmp_path).as_posix(),
            "cleanup_eligible": False,
        }
    ]
    assert written["lifecycle_prepared_cleaned"] == []
    assert prepared.read_bytes() == prepared_bytes
    assert trash.read_bytes() == trash_bytes
    assert sidecar.read_bytes() == sidecar_bytes


def test_reconcile_recognizes_directory_trash_root_suffix_proof(
    tmp_path: Path,
) -> None:
    page_id = "00000000-0000-4000-8000-000000000215"
    original_path = "Knowledge Base/Notes/Insights/group/nested.md"
    page, prepared, _, _ = _install_lifecycle_slot(
        tmp_path,
        state="trashed_committed",
        page_id=page_id,
        path=original_path,
    )
    trash_root = tmp_path / "Knowledge Base/_trash/2026-07-15/120001-group"
    trash = trash_root / "nested.md"
    trash.parent.mkdir(parents=True, exist_ok=True)
    page.replace(trash)
    sidecar = trash_root.with_name(f"{trash_root.name}.meta.json")
    sidecar.write_text(
        json.dumps(
            {
                "original_path": "Knowledge Base/Notes/Insights/group",
                "frontmatter_snapshot": {},
            }
        ),
        encoding="utf-8",
    )

    payload = reconcile_module.reconcile(tmp_path, dry_run=True).as_dict()

    assert payload["lifecycle_prepared"][0]["state"] == "trashed_committed"
    assert prepared.exists()


@pytest.mark.parametrize(
    "race",
    [
        "prepared_replacement",
        "primary_change",
        "trash_appearance",
        "sidecar_change",
        "trash_target_change",
    ],
)
def test_reconcile_blocks_prepared_primary_and_trash_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    page_id = _LIFECYCLE_IDS["stale"]
    original_path = "Knowledge Base/Notes/Insights/stale-race.md"
    page, prepared_path, _, after = _install_lifecycle_slot(
        tmp_path,
        state="stale",
        page_id=page_id,
        path=original_path,
    )
    sidecar: Path | None = None
    race_trash: Path | None = None
    if race in {"sidecar_change", "trash_target_change"}:
        race_trash = tmp_path / "Knowledge Base/_trash/2026-07-15/120002-race.md"
        race_trash.parent.mkdir(parents=True, exist_ok=True)
        race_trash.write_text(
            after
            if race == "sidecar_change"
            else _semantic_page(page_id).replace(
                "Direct editor content.", "Different trashed bytes."
            ),
            encoding="utf-8",
        )
        sidecar = race_trash.with_name(f"{race_trash.name}.meta.json")
        sidecar.write_text(
            json.dumps(
                {
                    "original_path": (
                        "Knowledge Base/Notes/Insights/other.md"
                        if race == "sidecar_change"
                        else original_path
                    ),
                    "frontmatter_snapshot": {"exomem_id": page_id},
                }
            ),
            encoding="utf-8",
        )
    real_cleanup = relation_review.cleanup_stale_lifecycle_prepared

    def race_then_cleanup(root: Path, inspection):
        if race == "prepared_replacement":
            current = relation_review.load_lifecycle_prepared(root, page_id)
            assert current is not None
            replacement = replace(
                current,
                transition_id="00000000-0000-4000-8000-000000000299",
            )
            prepared_path.write_text(
                relation_review.serialize_lifecycle_prepared(replacement),
                encoding="utf-8",
            )
        elif race == "primary_change":
            page.write_text(after, encoding="utf-8")
        elif race == "trash_appearance":
            trash = root / "Knowledge Base/_trash/2026-07-15/120003-race.md"
            trash.parent.mkdir(parents=True, exist_ok=True)
            trash.write_text(after, encoding="utf-8")
            trash.with_name(f"{trash.name}.meta.json").write_text(
                json.dumps(
                    {
                        "original_path": original_path,
                        "frontmatter_snapshot": {"exomem_id": page_id},
                    }
                ),
                encoding="utf-8",
            )
        elif race == "sidecar_change":
            assert sidecar is not None
            sidecar.write_text(
                json.dumps(
                    {
                        "original_path": original_path,
                        "frontmatter_snapshot": {"exomem_id": page_id},
                    }
                ),
                encoding="utf-8",
            )
        else:
            assert race_trash is not None
            race_trash.write_text(after, encoding="utf-8")
        return real_cleanup(root, inspection)

    monkeypatch.setattr(
        relation_review,
        "cleanup_stale_lifecycle_prepared",
        race_then_cleanup,
    )

    payload = reconcile_module.reconcile(tmp_path).as_dict()

    assert payload["lifecycle_prepared_cleaned"] == []
    assert payload["lifecycle_prepared_cleanup_blocked"] == [
        {"page_identity": page_id, "code": "LIFECYCLE_RECONCILE_RACE"}
    ]
    assert prepared_path.exists()


def test_reconcile_reports_malformed_state_and_deletes_nothing(tmp_path: Path) -> None:
    malformed_id = "00000000-0000-4000-8000-000000000216"
    malformed = relation_review.lifecycle_prepared_path(tmp_path, malformed_id)
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text("not-json", encoding="utf-8")
    stale_page, stale, _, _ = _install_lifecycle_slot(
        tmp_path,
        state="stale",
        page_id=_LIFECYCLE_IDS["stale"],
        path="Knowledge Base/Notes/Insights/stale-malformed-trash.md",
    )
    stale_bytes = stale.read_bytes()
    page_bytes = stale_page.read_bytes()
    bad_sidecar = tmp_path / "Knowledge Base/_trash/2026-07-15/bad.meta.json"
    bad_sidecar.parent.mkdir(parents=True, exist_ok=True)
    bad_sidecar.write_text("not-json", encoding="utf-8")

    payload = reconcile_module.reconcile(tmp_path).as_dict()

    assert {item["code"] for item in payload["lifecycle_prepared_issues"]} == {
        "LIFECYCLE_TRASH_INVALID",
        "RELATION_REVIEW_INVALID_JSON",
        "LIFECYCLE_TRASH_INDETERMINATE",
    }
    assert payload["lifecycle_prepared_cleaned"] == []
    assert malformed.read_text(encoding="utf-8") == "not-json"
    assert stale.read_bytes() == stale_bytes
    assert stale_page.read_bytes() == page_bytes


@pytest.mark.parametrize("failure", ["decision", "ambiguous_owner"])
def test_reconcile_reports_noncleanable_lifecycle_issues(
    tmp_path: Path,
    failure: str,
) -> None:
    page_id = "00000000-0000-4000-8000-000000000217"
    page, prepared, _, _ = _install_lifecycle_slot(
        tmp_path,
        state="stale",
        page_id=page_id,
        path="Knowledge Base/Notes/Insights/noncleanable.md",
    )
    if failure == "decision":
        decision = next(child for child in prepared.parent.iterdir() if child != prepared)
        decision.write_text("not-json", encoding="utf-8")
        expected = "RELATION_REVIEW_INVALID_JSON"
    else:
        duplicate = tmp_path / "Knowledge Base/Notes/Insights/duplicate-owner.md"
        duplicate.write_text(page.read_text(encoding="utf-8"), encoding="utf-8")
        expected = "LIFECYCLE_PRIMARY_AMBIGUOUS"
    prepared_bytes = prepared.read_bytes()

    payload = reconcile_module.reconcile(tmp_path).as_dict()

    assert {item["code"] for item in payload["lifecycle_prepared_issues"]} == {
        expected
    }
    assert payload["lifecycle_prepared_cleaned"] == []
    assert prepared.read_bytes() == prepared_bytes


def test_reconcile_reports_embeddings_disabled_in_test_env(vault: Path) -> None:
    """The suite runs with EXOMEM_DISABLE_EMBEDDINGS=1, so reconcile reports the
    embedding pass as disabled (no sidecar touched) rather than failing."""
    rep = reconcile_module.reconcile(vault)
    assert rep.embeddings_status == "disabled"
    assert rep.embeddings_refreshed == 0
    assert rep.dry_run is False


def test_reconcile_heals_index_count_drift(vault: Path) -> None:
    """An out-of-band edit that desyncs a count row is detected and restored."""
    top = vault / "Knowledge Base" / "index.md"
    original = top.read_text(encoding="utf-8")
    drifted = original.replace("- Notes (insight): 4", "- Notes (insight): 9")
    assert drifted != original, "fixture index.md changed shape; update the test"
    top.write_text(drifted, encoding="utf-8")

    # Drift is now visible to audit.
    pre = audit_module.audit(vault, categories=["index_drift"])
    assert pre.findings, "expected index_drift after corrupting a count"

    rep = reconcile_module.reconcile(vault)

    assert "Knowledge Base/index.md" in rep.indexes_updated, rep.as_dict()
    assert "- Notes (insight): 4" in top.read_text(encoding="utf-8")
    assert not any(
        f["category"] == "index_drift" for f in rep.remaining_drift
    ), rep.as_dict()


def test_reconcile_via_maintain_memory_heals_out_of_band_count_drift(vault: Path) -> None:
    """`maintain_memory(mode="reconcile")` — the only MCP-exposed entry point for
    reconcile — must actually heal per-type count drift by default, not just
    report it in `remaining_drift` forever. Regression for audit finding 1B-14:
    `op_maintain_memory`'s blanket `dry_run=True` default (shared with the
    riskier `fix`/`backfill-ids` modes) was silently swallowing every write for
    `mode="reconcile"` even though `op_reconcile` itself defaults to writing.
    """
    top = vault / "Knowledge Base" / "index.md"
    notes_dir = vault / "Knowledge Base" / "Notes" / "Insights"
    out_of_band = notes_dir / "manual-oob-note.md"
    out_of_band.write_text(
        "---\ntype: note\npage_type: insight\n---\n\n# Manual OOB\n", encoding="utf-8"
    )

    # Call exactly as an MCP client would: no explicit dry_run.
    res = commands.op_maintain_memory(vault, mode="reconcile")

    assert res["dry_run"] is False, res
    assert "- Notes (insight): 5" in top.read_text(encoding="utf-8")
    assert not any(
        f["category"] == "index_drift" for f in res["remaining_drift"]
    ), res

    # Idempotent: a second call finds nothing left to heal for this drift.
    res2 = commands.op_maintain_memory(vault, mode="reconcile")
    assert not any(
        f["category"] == "index_drift" for f in res2["remaining_drift"]
    ), res2


def test_reconcile_refreshes_source_indexes_and_total_rows(vault: Path) -> None:
    kb = vault / "Knowledge Base"
    extra = kb / "Sources" / "Articles" / "manual-source.md"
    extra.write_text("---\ntype: source\nsource_type: article\n---\n\n# Manual\n", encoding="utf-8")
    top = kb / "index.md"
    top.write_text(
        top.read_text(encoding="utf-8").replace("- Sources: 4", "- Sources: 0"),
        encoding="utf-8",
    )

    rep = reconcile_module.reconcile(vault)

    top_text = top.read_text(encoding="utf-8")
    assert "- Sources: 5" in top_text
    assert "- Notes:" in top_text
    assert "- Entities:" in top_text
    source_index = (kb / "Sources" / "index.md").read_text(encoding="utf-8")
    assert "Articles]]" in source_index and "(3)" in source_index
    assert "Knowledge Base/Sources/index.md" in rep.indexes_updated


def test_reconcile_dry_run_reports_without_writing(vault: Path) -> None:
    """dry_run surfaces the would-be index fix but writes nothing to disk."""
    top = vault / "Knowledge Base" / "index.md"
    top.write_text(
        top.read_text(encoding="utf-8").replace(
            "- Notes (insight): 1", "- Notes (insight): 9"
        ),
        encoding="utf-8",
    )
    drifted = top.read_text(encoding="utf-8")

    rep = reconcile_module.reconcile(vault, dry_run=True)

    assert rep.dry_run is True
    assert "Knowledge Base/index.md" in rep.indexes_updated
    assert top.read_text(encoding="utf-8") == drifted, "dry_run must not write"


def test_reconcile_creates_baseline_only_when_not_dry_run_and_never_refreshes_it(
    vault: Path,
) -> None:
    path = activation_manifest.manifest_path(vault)
    assert not path.exists()
    page = vault / "Knowledge Base/Notes/Insights/legacy-reconcile.md"
    page.write_text("---\ntype: insight\nstatus: active\n---\n\n# Legacy\n", encoding="utf-8")
    before_page = page.read_bytes()

    reconcile_module.reconcile(vault, dry_run=True)
    assert not path.exists()

    reconcile_module.reconcile(vault)
    first_bytes = path.read_bytes()
    assert activation_manifest.is_grandfathered(vault, page)
    assert page.read_bytes() == before_page
    assert "exomem_id:" not in page.read_text(encoding="utf-8")
    assert not (vault / "Knowledge Base/.review-state.json").exists()

    later = vault / "Knowledge Base/Notes/Insights/later-reconcile.md"
    later.write_text("---\ntype: insight\nstatus: active\n---\n\n# Later\n", encoding="utf-8")
    reconcile_module.reconcile(vault)
    assert path.read_bytes() == first_bytes
    assert not activation_manifest.is_grandfathered(vault, later)


def test_reconcile_clears_deferred_semantic_work_after_embedding_refresh(
    vault: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    index_sync.clear_deferred_work(vault)

    from exomem import find, lexstore

    monkeypatch.setattr(lexstore, "upsert_after_write", lambda root, paths: None)
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda root, changed, deleted: None,
    )
    target = vault / "Knowledge Base" / "Notes" / "reconcile-deferred.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# reconcile deferred\n", encoding="utf-8")
    index_sync.upsert_after_write(vault, [target])
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 1

    calls: list[list[Path]] = []
    monkeypatch.setattr(
        "exomem.embeddings.upsert_after_write",
        lambda root, paths: calls.append(list(paths)),
    )
    monkeypatch.setattr(
        audit_module,
        "_check_embedding_drift",
        lambda root: [
            SimpleNamespace(path=Path("Knowledge Base/Notes/reconcile-deferred.md"))
        ],
    )
    monkeypatch.setattr(
        audit_module,
        "audit",
        lambda root, categories: SimpleNamespace(findings=[]),
    )
    monkeypatch.setattr(
        reconcile_module.indexes,
        "compute_subindex_writes",
        lambda root, top_index_text: ([], top_index_text),
    )
    monkeypatch.setattr("exomem.lexstore.ensure_fresh", lambda root: None)

    rep = reconcile_module.reconcile(vault)

    assert rep.embeddings_status == "refreshed"
    assert calls == [[target]]
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 0


def test_reconcile_preserves_deferred_work_after_embedding_failure(
    vault: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    index_sync.clear_deferred_work(vault)

    from exomem import find, lexstore

    monkeypatch.setattr(lexstore, "upsert_after_write", lambda root, paths: None)
    monkeypatch.setattr(find, "on_resolver_files_changed", lambda root, changed, deleted: None)
    target = vault / "Knowledge Base" / "Notes" / "reconcile-retry.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# reconcile retry\n", encoding="utf-8")
    index_sync.upsert_after_write(vault, [target])

    monkeypatch.setattr("exomem.embeddings.upsert_after_write", lambda root, paths: False)
    monkeypatch.setattr(
        audit_module,
        "_check_embedding_drift",
        lambda root: [SimpleNamespace(path=Path("Knowledge Base/Notes/reconcile-retry.md"))],
    )
    monkeypatch.setattr(
        audit_module,
        "audit",
        lambda root, categories: SimpleNamespace(findings=[]),
    )
    monkeypatch.setattr(
        reconcile_module.indexes,
        "compute_subindex_writes",
        lambda root, top_index_text: ([], top_index_text),
    )
    monkeypatch.setattr("exomem.lexstore.ensure_fresh", lambda root: None)

    rep = reconcile_module.reconcile(vault)

    assert rep.embeddings_status == "deferred"
    assert rep.embeddings_refreshed == 0
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 1
