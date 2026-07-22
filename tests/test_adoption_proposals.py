"""Adoption Studio agent contract (Lane B): work-item, propose, review surfacing,
apply-proposal. Mirrors `tests/test_relation_queue.py` / `tests/test_adoption_run.py`
patterns; `vault` fixture unused here (tests build their own legacy vault + KB, like
`test_adoption_run.py`, since they need a durable run + governed pages to bind to)."""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest

from exomem import adoption_proposals, adoption_run, commands, find, get_page, relation_review

TODAY = dt.date(2026, 7, 14)


def _semantic_content(title: str, statement: str) -> str:
    return (
        f"# {title}\n\n"
        "## Observations\n\n"
        f"- [adoption finding] {statement} #adoption\n"
    )


def _snapshot_md(root: Path) -> dict[str, bytes]:
    """Full-tree byte snapshot of every markdown file (propose must touch none)."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*.md")
        if p.is_file()
    }


def _snapshot_all(root: Path) -> dict[str, bytes]:
    """Full-tree byte snapshot of EVERY file (pack assembly must write nothing)."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def _legacy_vault(root: Path) -> Path:
    vault = root / "vault"
    old = vault / "Old Notes"
    old.mkdir(parents=True)
    (old / "a.md").write_text("# A\n\nFirst imported note about widgets.\n", encoding="utf-8")
    (old / "b.md").write_text("# B\n\nSecond imported note about gadgets.\n", encoding="utf-8")
    kb = vault / "Knowledge Base"
    (kb / "Notes").mkdir(parents=True)
    sources = kb / "Sources"
    sources.mkdir(parents=True)
    (sources / "index.md").write_text(
        "# Sources - Index\n\n## By type\n\n## Recent captures\n\n", encoding="utf-8"
    )
    (kb / "index.md").write_text(
        "# Knowledge Base\n\n## Counts\n\n- Sources: 0\n\n## Recent activity\n\n",
        encoding="utf-8",
    )
    (kb / "log.md").write_text("# Log\n\n---\n", encoding="utf-8")
    find.clear_cache()
    return vault


def _applied_run(vault: Path) -> dict:
    run = adoption_run.start(vault, path="Old Notes", today=TODAY)
    run_id = run["run_id"]
    adoption_run.select(vault, run_id=run_id, include=["Old Notes"])
    plan = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    applied = adoption_run.apply(
        vault, run_id=run_id, plan_id=plan["plan"]["plan_id"], today=TODAY
    )
    find.clear_cache()
    return applied


def _imported_paths(applied: dict) -> list[str]:
    return sorted(
        o["target_path"] for o in applied["outcomes"].values() if o.get("status") == "applied"
    )


def _write_page(vault: Path, rel: str, *, title: str = "Target", body: str = "Body text.\n") -> str:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: insight\ntitle: {title}\n---\n\n# {title}\n\n{body}",
        encoding="utf-8",
        newline="\n",
    )
    return path.read_text(encoding="utf-8")


def _content_hash(vault: Path, rel: str) -> str:
    return hashlib.sha256((vault / rel).read_bytes()).hexdigest()


# --- 13 ---
def test_work_item_is_bounded_and_read_only(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    before = _snapshot_md(vault)

    item = adoption_proposals.work_item(vault, run_id=run_id, max_sources=1, max_chars_per_source=10)

    assert item["run_id"] == run_id
    assert item["constraints"]
    limits = item["limits"]
    assert limits["shown"] == 1
    assert limits["total"] == 2
    assert limits["truncated"] == 1
    assert len(item["sources"]) == 1
    for row in item["sources"]:
        if len(row["excerpt"]) >= 10:
            assert row["excerpt_truncated"] is True
    assert "compilation" in item["proposal_kinds"]
    assert _snapshot_md(vault) == before


# --- 14 ---
def test_propose_validates_each_kind(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]

    result = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "relation",
                "why": "unknown relation type",
                "payload": {"from": imported[0], "to": imported[1], "relation_type": "not_a_real_type"},
                "bindings": {"run_fingerprint": run_fp},
            },
            {
                "kind": "compilation",
                "why": "bad note type",
                "payload": {
                    "sources": imported,
                    "title": "Bad",
                    "note_type": "not-a-real-type",
                    "content": "Some content.",
                },
                "bindings": {"run_fingerprint": run_fp},
            },
            {
                "kind": "compilation",
                "why": "valid compilation",
                "payload": {
                    "sources": imported,
                    "title": "Combined summary",
                    "note_type": "insight",
                    "content": _semantic_content(
                        "Combined", "The imported notes describe related behavior."
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            },
        ],
    )

    statuses = [row["status"] for row in result["proposals"]]
    assert statuses == ["invalid", "invalid", "proposed"]
    assert result["proposals"][0]["findings"][0]["code"] == "UNKNOWN_RELATION_TYPE"
    assert result["proposals"][1]["findings"][0]["code"] == "INVALID_NOTE_TYPE"
    assert result["proposals"][2]["ref"].startswith(adoption_proposals.ADOPTION_REVIEW_PREFIX)
    assert result["proposals"][2]["fingerprint"]


# --- 15 ---
def test_propose_binds_run_fingerprint_and_source_hashes(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]

    # Distinct payload (title) from the wrong-fingerprint case below: `proposal_id`
    # is `sha256(kind + payload)` — bindings are deliberately excluded (design.md
    # Decision 5's dedup contract) — so two DIFFERENT payloads are needed to prove
    # the fingerprint check runs independently of dedup, not because dedup itself
    # is under test here (that's the second half of this test).
    good_proposal = {
        "kind": "compilation",
        "why": "combine",
        "payload": {
            "sources": imported,
            "title": "Combined",
            "note_type": "insight",
            "content": _semantic_content(
                "Combined", "The imported notes support one combined conclusion."
            ),
        },
        "bindings": {"run_fingerprint": run_fp},
    }
    wrong_fp = {
        **good_proposal,
        "payload": {**good_proposal["payload"], "title": "Combined (wrong fp)"},
        "bindings": {"run_fingerprint": "0" * 24},
    }

    wrong_result = adoption_proposals.propose(vault, run_id=run_id, proposals=[wrong_fp])
    assert wrong_result["proposals"][0]["status"] == "invalid"
    assert any(
        f["code"] == "RUN_FINGERPRINT_MISMATCH" for f in wrong_result["proposals"][0]["findings"]
    )

    first = adoption_proposals.propose(vault, run_id=run_id, proposals=[good_proposal])
    assert first["proposals"][0]["status"] == "proposed"
    second = adoption_proposals.propose(vault, run_id=run_id, proposals=[good_proposal])
    assert second["proposals"][0].get("deduplicated") is True
    assert second["proposals"][0]["proposal_id"] == first["proposals"][0]["proposal_id"]

    payload = adoption_run.AdoptionRunStore(vault).load_proposals(run_id)
    # Dedup: the identical resubmission did not create a second record.
    assert len(payload["proposals"]) == 2  # the wrong-fp invalid one + the one valid one


# --- 16 ---
def test_propose_never_touches_markdown(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]
    before = _snapshot_md(vault)

    adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine",
                "payload": {
                    "sources": imported,
                    "title": "Combined",
                    "note_type": "insight",
                    "content": _semantic_content(
                        "Combined", "The imported notes support one combined conclusion."
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )

    after = _snapshot_md(vault)
    assert after == before
    proposals_file = (
        adoption_run.AdoptionRunStore(vault).run_dir(run_id) / "proposals.json"
    )
    assert proposals_file.exists()


# --- 17 ---
def test_review_memory_adoption_mode_lists_open_items(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]

    adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine",
                "payload": {
                    "sources": imported,
                    "title": "Combined",
                    "note_type": "insight",
                    "content": _semantic_content(
                        "Combined", "The imported notes support one combined conclusion."
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            },
            {
                "kind": "relation",
                "why": "bad",
                "payload": {"from": imported[0], "to": imported[1], "relation_type": "nope"},
                "bindings": {"run_fingerprint": run_fp},
            },
        ],
    )

    queue = commands.op_review_memory(vault, mode="adoption", limit=50)
    assert queue["mode"] == "adoption"
    assert queue["filtered"]["invalid"] == 1
    items = [i for group in queue["runs"] for i in group["items"]]
    assert len(items) == 1
    assert items[0]["kind"] == "compilation"
    assert items[0]["state"] == "open"


# --- 18 ---
def test_triage_adoption_ref_round_trip_and_resurfacing(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]

    submitted = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine",
                "payload": {
                    "sources": imported,
                    "title": "Combined",
                    "note_type": "insight",
                    "content": _semantic_content(
                        "Combined", "The imported notes support one combined conclusion."
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )
    ref = submitted["proposals"][0]["ref"]
    fingerprint = submitted["proposals"][0]["fingerprint"]

    assert adoption_proposals.is_adoption_ref(ref)
    dismissed = commands.op_triage_memory(
        vault, ref=ref, action="dismiss", expected_fingerprint=fingerprint
    )
    assert dismissed["state"] == "dismissed"

    queue_after_dismiss = commands.op_review_memory(vault, mode="adoption", limit=50)
    open_refs = {i["ref"] for group in queue_after_dismiss["runs"] for i in group["items"]}
    assert ref not in open_refs

    # Edit a bound source so its content hash (and thus fingerprint) changes.
    (vault / imported[0]).write_text(
        (vault / imported[0]).read_text(encoding="utf-8") + "\nExtra line.\n",
        encoding="utf-8",
    )

    queue_after_edit = commands.op_review_memory(vault, mode="adoption", limit=50)
    open_refs_after = {i["ref"] for group in queue_after_edit["runs"] for i in group["items"]}
    assert ref in open_refs_after


# --- 19 ---
def test_review_item_context_adoption_dispatch(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]

    submitted = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine",
                "payload": {
                    "sources": imported,
                    "title": "Combined",
                    "note_type": "insight",
                    "content": _semantic_content(
                        "Combined", "The imported notes support one combined conclusion."
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )
    ref = submitted["proposals"][0]["ref"]

    context = commands.op_review_item_context(vault, ref=ref)
    assert context["mode"] == "adoption"
    assert context["ref"] == ref
    assert context["binding_check"]
    for row in context["binding_check"]:
        assert row["changed"] is False


# --- 20 ---
def test_apply_proposal_compilation_routes_through_remember(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]

    submitted = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine",
                "payload": {
                    "sources": imported,
                    "title": "Combined summary",
                    "note_type": "insight",
                    "content": _semantic_content(
                        "Combined summary", "The imported notes describe related behavior."
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )
    ref = submitted["proposals"][0]["ref"]
    fingerprint = submitted["proposals"][0]["fingerprint"]

    result = adoption_proposals.apply_proposal(
        vault, ref=ref, expected_fingerprint=fingerprint, why="Approved: looks correct"
    )

    assert result["applied"] is True
    assert result["result_path"]
    note_page = get_page.get_page(vault, path=result["result_path"])
    assert "sources:" in note_page.content
    for src in imported:
        # note.note() renders sources as extension-stripped wikilinks.
        assert src.removesuffix(".md") in note_page.content

    # note.note() (the leaf behind `remember`) is create-only and has no
    # `why`/`reason` param to thread into Knowledge Base/log.md — injecting one
    # there would mean adoption_proposals writing Markdown directly, which is
    # forbidden. The approver's audit trail for a compilation lives in the
    # proposal's own `applied.why` record instead.
    reloaded = adoption_run.AdoptionRunStore(vault).load_proposals(run_id)
    stored = reloaded["proposals"][0]
    assert stored["status"] == "applied"
    assert stored["applied"]["why"] == "Approved: looks correct"


# --- 21 ---
def test_apply_proposal_relation_requires_expected_hash_and_refuses_drift(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]

    submitted = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "relation",
                "why": "these relate",
                "payload": {"from": imported[0], "to": imported[1], "relation_type": "relates_to"},
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )
    ref = submitted["proposals"][0]["ref"]
    fingerprint = submitted["proposals"][0]["fingerprint"]

    # Missing expected_hash refuses.
    with pytest.raises(adoption_proposals.AdoptionProposalError) as ei:
        adoption_proposals.apply_proposal(
            vault, ref=ref, expected_fingerprint=fingerprint, why="Approved"
        )
    assert ei.value.code == "INVALID_APPLY"

    # Stale expected_hash (mirrors test_relation_queue_accept_refuses_on_target_drift):
    # edit the target page out from under the approval, then the CAS refuses.
    from_path = imported[0]
    stale_hash = _content_hash(vault, from_path)
    (vault / from_path).write_text(
        (vault / from_path).read_text(encoding="utf-8") + "\nDrifted.\n", encoding="utf-8"
    )
    with pytest.raises((adoption_proposals.AdoptionProposalError, ValueError)):
        adoption_proposals.apply_proposal(
            vault,
            ref=ref,
            expected_fingerprint=fingerprint,
            why="Approved",
            expected_hash=stale_hash,
        )


# --- 22 ---
def test_apply_proposal_supersession_uses_replace_cas(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    old_rel = "Knowledge Base/Notes/Old-Take.md"
    _write_page(vault, old_rel, title="Old Take", body="An earlier conclusion.\n")
    find.clear_cache()
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    run_fp = applied["inventory_fingerprint"]

    submitted = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "supersession",
                "why": "corrected understanding",
                "payload": {
                    "old_path": old_rel,
                    "title": "New Take",
                    "note_type": "insight",
                    "content": _semantic_content(
                        "New Take", "The corrected evidence changes the earlier conclusion."
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )
    ref = submitted["proposals"][0]["ref"]
    fingerprint = submitted["proposals"][0]["fingerprint"]

    result = adoption_proposals.apply_proposal(
        vault, ref=ref, expected_fingerprint=fingerprint, why="Supersede with corrected take"
    )
    assert result["applied"] is True
    old_page = get_page.get_page(vault, path=old_rel)
    assert old_page.frontmatter.get("status") == "superseded"


def test_apply_proposal_supersession_refuses_on_mid_flight_edit(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    old_rel = "Knowledge Base/Notes/Old-Take2.md"
    _write_page(vault, old_rel, title="Old Take Two", body="An earlier conclusion.\n")
    find.clear_cache()
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    run_fp = applied["inventory_fingerprint"]

    submitted = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "supersession",
                "why": "corrected understanding",
                "payload": {
                    "old_path": old_rel,
                    "title": "New Take Two",
                    "note_type": "insight",
                    "content": _semantic_content(
                        "New Take Two", "The corrected evidence changes the earlier conclusion."
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )
    ref = submitted["proposals"][0]["ref"]
    fingerprint = submitted["proposals"][0]["fingerprint"]

    # Mid-flight edit AFTER submission changes the bound old-page hash — the
    # binding re-hash in apply_proposal refuses before ever reaching replace's CAS.
    (vault / old_rel).write_text(
        (vault / old_rel).read_text(encoding="utf-8") + "\nDrifted mid-flight.\n",
        encoding="utf-8",
    )

    with pytest.raises(adoption_proposals.AdoptionProposalError) as ei:
        adoption_proposals.apply_proposal(
            vault, ref=ref, expected_fingerprint=fingerprint, why="Supersede"
        )
    assert ei.value.code == "REVIEW_ITEM_CHANGED"
    reloaded_page = get_page.get_page(vault, path=old_rel)
    assert reloaded_page.frontmatter.get("status") != "superseded"


# --- 23 ---
def test_apply_proposal_stale_binding_refuses(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]

    submitted = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine",
                "payload": {
                    "sources": imported,
                    "title": "Combined",
                    "note_type": "insight",
                    "content": _semantic_content(
                        "Combined", "The imported notes support one combined conclusion."
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )
    ref = submitted["proposals"][0]["ref"]
    fingerprint = submitted["proposals"][0]["fingerprint"]

    before = _snapshot_md(vault)
    # Edit a bound governed Source after submission.
    (vault / imported[0]).write_text(
        (vault / imported[0]).read_text(encoding="utf-8") + "\nBound source drifted.\n",
        encoding="utf-8",
    )

    with pytest.raises(adoption_proposals.AdoptionProposalError) as ei:
        adoption_proposals.apply_proposal(
            vault, ref=ref, expected_fingerprint=fingerprint, why="Approved"
        )
    assert ei.value.code == "REVIEW_ITEM_CHANGED"

    after = _snapshot_md(vault)
    # Nothing else written; only the deliberate drift edit differs.
    before[imported[0]] = after[imported[0]]
    assert after == before


# --- 24 ---
def test_apply_proposal_requires_fingerprint_and_why(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]

    submitted = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine",
                "payload": {
                    "sources": imported,
                    "title": "Combined",
                    "note_type": "insight",
                    "content": _semantic_content(
                        "Combined", "The imported notes support one combined conclusion."
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )
    ref = submitted["proposals"][0]["ref"]
    fingerprint = submitted["proposals"][0]["fingerprint"]

    with pytest.raises(adoption_proposals.AdoptionProposalError) as ei_fp:
        adoption_proposals.apply_proposal(
            vault, ref=ref, expected_fingerprint=None, why="Approved"
        )
    assert ei_fp.value.code == "INVALID_APPLY"

    with pytest.raises(adoption_proposals.AdoptionProposalError) as ei_why:
        adoption_proposals.apply_proposal(
            vault, ref=ref, expected_fingerprint=fingerprint, why=None
        )
    assert ei_why.value.code == "INVALID_APPLY"


def _submit_compilation(vault: Path, run_id: str, run_fp: str, imported: list[str], *, title: str = "Combined", bindings_extra: dict | None = None) -> dict:
    bindings = {"run_fingerprint": run_fp, **(bindings_extra or {})}
    submitted = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine",
                "payload": {
                    "sources": imported,
                    "title": title,
                    "note_type": "insight",
                    "content": _semantic_content(
                        title, "The imported notes describe related behavior."
                    ),
                },
                "bindings": bindings,
            }
        ],
    )
    return submitted["proposals"][0]


def test_propose_refuses_stale_submitted_source_bindings(tmp_path: Path) -> None:
    """A source hash the agent read must be honored: drift => invalid, not silently rebound."""
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    rec = _submit_compilation(
        vault,
        applied["run_id"],
        applied["inventory_fingerprint"],
        _imported_paths(applied),
        bindings_extra={"sources": {_imported_paths(applied)[0]: "0" * 64}},
    )
    assert rec["status"] == "invalid"
    assert any(f.get("code") == "SOURCE_CHANGED" for f in rec.get("findings") or [])


def test_apply_proposal_interrupted_apply_refuses_silent_retry(tmp_path: Path) -> None:
    """A proposal caught mid-apply (crash window) must refuse a blind re-apply."""
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    rec = _submit_compilation(
        vault, run_id, applied["inventory_fingerprint"], _imported_paths(applied)
    )
    store = adoption_run.AdoptionRunStore(vault)
    saved = store.load_proposals(run_id)
    saved["proposals"][0]["status"] = "applying"
    store.save_proposals(run_id, saved)
    with pytest.raises(adoption_proposals.AdoptionProposalError) as excinfo:
        adoption_proposals.apply_proposal(
            vault, ref=rec["ref"], expected_fingerprint=rec["fingerprint"], why="retry"
        )
    assert excinfo.value.code == "APPLY_IN_FLIGHT"


def test_work_item_clamps_source_char_caps(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    negative = adoption_proposals.work_item(vault, run_id=run_id, max_chars_per_source=-5)
    for row in negative["sources"]:
        assert len(row["excerpt"]) <= 1
    huge = adoption_proposals.work_item(vault, run_id=run_id, max_chars_per_source=10**9)
    for row in huge["sources"]:
        assert len(row["excerpt"]) <= 20_000


def test_build_queue_limit_applies_across_runs(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    first = _applied_run(vault)
    extra = vault / "More Notes"
    extra.mkdir()
    (extra / "c.md").write_text("# C\n\nThird note.\n", encoding="utf-8")
    (extra / "d.md").write_text("# D\n\nFourth note.\n", encoding="utf-8")
    run2 = adoption_run.start(vault, path="More Notes", today=TODAY)
    adoption_run.select(vault, run_id=run2["run_id"], include=["More Notes"])
    plan2 = adoption_run.plan(vault, run_id=run2["run_id"], today=TODAY)
    second = adoption_run.apply(
        vault, run_id=run2["run_id"], plan_id=plan2["plan"]["plan_id"], today=TODAY
    )
    for idx, applied_run_doc in enumerate((first, second)):
        for title in (f"Alpha {idx}", f"Beta {idx}"):
            _submit_compilation(
                vault,
                applied_run_doc["run_id"],
                applied_run_doc["inventory_fingerprint"],
                _imported_paths(applied_run_doc),
                title=title,
            )
    queue = adoption_proposals.build_queue(vault, limit=3)
    assert queue["total"] == 4
    assert queue["shown"] == 3
    assert len(queue["items"]) == 3
    assert sum(len(g["items"]) for g in queue["runs"]) == 3


# --------------------------------------------------------------------------- #
# Lane A — adoption × semantic units integration
# --------------------------------------------------------------------------- #
def _seed_governed_insight(vault: Path, imported: list[str], *, title: str = "Seed") -> str:
    """Commit one governed insight so the corpus is non-empty.

    A create into an empty corpus auto-bootstraps (no review needed); once a
    governed page exists, the NEXT create with no honest typed relation needs an
    explicit reviewed-none disposition — which is what the propose-time contract
    validation must surface.
    """
    result = commands.op_remember(
        vault,
        content=_semantic_content(
            title, "The imported notes establish a governed seed conclusion."
        ),
        title=title,
        note_type="insight",
        sources=imported,
        suggestions=False,
    )
    find.clear_cache()
    return result["path"]


def _enrich_source_with_units(vault: Path, imported_path: str) -> None:
    """Append authored semantic units OUTSIDE the fenced capture so the Source has units."""
    page = vault / imported_path
    page.write_text(
        page.read_text(encoding="utf-8")
        + "\n## Observations\n\n"
        "- [config] Widget cache TTL is thirty seconds #runtime ^ttl\n"
        "- [rule] Evict least-recently-used widgets first #runtime ^lru\n",
        encoding="utf-8",
    )
    find.clear_cache()


def test_work_item_rows_include_semantic_unit_packs(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    _enrich_source_with_units(vault, imported[0])

    item = adoption_proposals.work_item(vault, run_id=run_id, max_sources=5)
    by_import = {row["imported_path"]: row for row in item["sources"]}

    enriched = by_import[imported[0]]["semantic_units"]
    assert enriched["available"] is True
    assert len(enriched["units"]) >= 1
    assert {u["kind"] for u in enriched["units"]} <= {"observation", "decision", "rule", "config"}

    plain = by_import[imported[1]]["semantic_units"]
    assert plain == {"units": [], "available": True}


def test_work_item_pack_assembly_is_read_only(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    imported = _imported_paths(applied)
    _enrich_source_with_units(vault, imported[0])

    # Exercise the pack constructor directly: a full-tree byte snapshot proves the
    # shared semantic-unit pack assembly mutates no vault file, index, or unit record.
    before = _snapshot_all(vault)
    pack = adoption_proposals._semantic_unit_pack(vault, imported[0], 2000)
    assert pack["available"] is True
    assert pack["units"], "the enriched source must yield a populated pack"
    assert _snapshot_all(vault) == before


def test_propose_records_contract_findings_for_reviewable_gaps(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]
    _seed_governed_insight(vault, imported)
    before_all = _snapshot_all(vault)

    result = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine the widget notes",
                "payload": {
                    "sources": imported,
                    "title": "Widget synthesis",
                    "note_type": "insight",
                    "content": _semantic_content(
                        "Widget synthesis", "The widget notes support a fresh conclusion."
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )
    row = result["proposals"][0]
    assert row["status"] == "proposed"
    assert row["contract_findings"]
    assert row["contract_findings"][0]["code"] == "RELATION_DISPOSITION_MISSING"
    assert set(row["contract_findings"][0]) == {"code", "severity", "detail"}
    assert row["reviewed_none_required"] is True

    # validate_only must write NOTHING: the only file to change is proposals.json.
    after_all = _snapshot_all(vault)
    changed = {k for k in after_all if before_all.get(k) != after_all[k]}
    assert changed, "propose must persist the proposal"
    assert all("proposals.json" in k for k in changed), changed

    stored = adoption_run.AdoptionRunStore(vault).load_proposals(run_id)["proposals"][-1]
    assert stored["contract_findings"] == row["contract_findings"]
    assert stored["reviewed_none_required"] is True
    assert stored["committable_after_review"] is True


def test_propose_invalidates_on_non_review_blockers(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]

    result = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine with a broken unit",
                "payload": {
                    "sources": imported,
                    "title": "Broken synthesis",
                    "note_type": "insight",
                    # Two units claim the same ^dup anchor — a blocking contract
                    # finding no reviewed-none disposition can ever clear.
                    "content": (
                        "# Broken synthesis\n\n## Observations\n\n"
                        "- [config] First widget note #tag ^dup\n"
                        "- [rule] Second widget note #tag ^dup\n"
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )
    row = result["proposals"][0]
    assert row["status"] == "invalid"
    assert any(f["code"] == "CONTRACT_BLOCKED" for f in row["findings"])

    with pytest.raises(adoption_proposals.AdoptionProposalError) as excinfo:
        adoption_proposals.apply_proposal(
            vault, ref=row["ref"], expected_fingerprint=row["fingerprint"], why="approve"
        )
    assert excinfo.value.code == "PROPOSAL_INVALID"


def test_adoption_missing_unit_is_refused_without_mutating_sources_or_proposal(
    tmp_path: Path,
) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)

    result = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine without authoring a semantic unit",
                "payload": {
                    "sources": imported,
                    "title": "Prose-only synthesis",
                    "note_type": "insight",
                    "content": "# Prose-only synthesis\n\nOrdinary structural prose.\n",
                },
                "bindings": {"run_fingerprint": applied["inventory_fingerprint"]},
            }
        ],
    )
    row = result["proposals"][0]
    assert row["status"] == "invalid"
    assert "missing_semantic_unit" in {
        finding["code"] for finding in row["contract_findings"]
    }

    store = adoption_run.AdoptionRunStore(vault)
    proposal_path = store.run_dir(run_id) / "proposals.json"
    before_markdown = _snapshot_md(vault)
    before_proposal = proposal_path.read_bytes()

    with pytest.raises(adoption_proposals.AdoptionProposalError) as excinfo:
        adoption_proposals.apply_proposal(
            vault,
            ref=row["ref"],
            expected_fingerprint=row["fingerprint"],
            why="approve invalid prose",
        )

    assert excinfo.value.code == "PROPOSAL_INVALID"
    assert _snapshot_md(vault) == before_markdown
    assert proposal_path.read_bytes() == before_proposal


def test_propose_labels_non_contract_validation_errors_distinctly(tmp_path: Path) -> None:
    # A near-miss project key raises PROJECT_KEY_TYPO from the note path — a plain
    # validation error, NOT a semantic-contract block. The proposal must still
    # invalidate, but the finding must be labelled VALIDATION_FAILED rather than
    # masquerading as a contract block (the pre-fix branch mislabelled every
    # exception as CONTRACT_BLOCKED).
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]

    result = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine under a typo'd project key",
                "payload": {
                    "sources": imported,
                    "title": "Typo project synthesis",
                    "note_type": "insight",
                    # 'persynal' is edit-distance 1 from the registered 'personal'
                    # key → PROJECT_KEY_TYPO raised before any contract preflight.
                    "project": "persynal",
                    "content": (
                        "# Typo project synthesis\n\n## Observations\n\n"
                        "- [config] A single clean widget note #tag\n"
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )
    row = result["proposals"][0]
    assert row["status"] == "invalid"
    codes = {f["code"] for f in row["findings"]}
    assert "VALIDATION_FAILED" in codes
    assert "CONTRACT_BLOCKED" not in codes
    detail = next(f["detail"] for f in row["findings"] if f["code"] == "VALIDATION_FAILED")
    assert "proposal validation failed" in detail
    assert "PROJECT_KEY_TYPO" in detail


def test_queue_and_context_carry_contract_findings(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]
    _seed_governed_insight(vault, imported)

    submitted = adoption_proposals.propose(
        vault,
        run_id=run_id,
        proposals=[
            {
                "kind": "compilation",
                "why": "combine",
                "payload": {
                    "sources": imported,
                    "title": "Synthesis two",
                    "note_type": "insight",
                    "content": _semantic_content(
                        "Synthesis two", "The widget notes support another conclusion."
                    ),
                },
                "bindings": {"run_fingerprint": run_fp},
            }
        ],
    )
    row = submitted["proposals"][0]
    assert row["status"] == "proposed"

    queue = adoption_proposals.build_queue(vault, run_id=run_id)
    item = queue["items"][0]
    assert item["contract_findings"]
    assert item["contract_findings"][0]["code"] == "RELATION_DISPOSITION_MISSING"

    ctx = adoption_proposals.assemble_context(vault, ref=row["ref"])
    assert ctx["contract_findings"] == item["contract_findings"]
    assert ctx["reviewed_none_required"] is True
    assert ctx["committable_after_review"] is True
    assert ctx["status"] == "proposed"


def test_reviewed_none_review_reason_is_approver_why(tmp_path: Path) -> None:
    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    run_id = applied["run_id"]
    imported = _imported_paths(applied)
    run_fp = applied["inventory_fingerprint"]
    # A non-empty corpus forces the applied compilation through the reviewed-none path.
    _seed_governed_insight(vault, imported)

    rec = _submit_compilation(vault, run_id, run_fp, imported, title="Reviewed synthesis")
    why = "Approved: no honest typed relation yet, resurface later"
    result = adoption_proposals.apply_proposal(
        vault, ref=rec["ref"], expected_fingerprint=rec["fingerprint"], why=why
    )
    assert result["applied"] is True
    assert get_page.get_page(vault, path=result["result_path"]).path

    reviews = relation_review.load_relation_reviews(vault)
    reviewed_none = [r for r in reviews if r.kind == "reviewed_none" and r.reason == why]
    assert reviewed_none, "the approver's why must be recorded as the reviewed-none reason"
