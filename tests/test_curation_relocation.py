"""A curation plan relocates pages: moves with lineage, one link rewrite, recovery and undo.

OpenSpec add-curation-relocation-protocol, groups 4 and 2.5. The vault is the canonical fixture
in file mode. Each test names the failure it catches.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from exomem import commands, curation, move_file, writer_lease
from exomem.vault import content_hash, parse_frontmatter

INSIGHTS = "Knowledge Base/Notes/Insights"
NOTES = (
    f"{INSIGHTS}/autovacuum-thresholds-prevent-table-bloat.md",
    f"{INSIGHTS}/percentage-based-feature-flag-rollout.md",
    f"{INSIGHTS}/rrf-fusion-beats-score-normalization.md",
)
SOURCE = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md"
OTHER_SOURCE = "Knowledge Base/Sources/Articles/2026-06-02-postgres-autovacuum-tuning.md"
MAINTAIN = next(c for c in commands.PRODUCT_COMMANDS if c.name == "maintain_memory")


def _moved(rel: str) -> str:
    return rel.replace(f"{INSIGHTS}/", f"{INSIGHTS}/alpha/")


def _link(rel: str) -> str:
    return f"[[{rel.removesuffix('.md')}]]"


def _with_identity(vault: Path, rel: str) -> None:
    path = vault / rel
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("---\n", f"---\nexomem_id: {uuid.uuid4()}\n", 1), encoding="utf-8")


def _append(vault: Path, rel: str, line: str) -> Path:
    path = vault / rel
    path.write_text(path.read_text(encoding="utf-8") + f"\n{line}\n", encoding="utf-8")
    return path


def _hash(vault: Path, rel: str) -> str:
    return content_hash((vault / rel).read_text(encoding="utf-8"))


def _move_step(rel: str, **fields: object) -> dict[str, object]:
    args: dict[str, object] = {"old_path": rel, "new_path": _moved(rel)}
    if fields:
        args["fields"] = fields
    return {"step_id": f"move-{Path(rel).stem[:24]}", "kind": "move", "args": args}


REPOINT = {"step_id": "repoint", "kind": "repoint-links", "args": {}}


def _pointer_edit(vault: Path, rel: str, field: str, value: object) -> dict[str, object]:
    return {
        "step_id": f"fix-{Path(rel).stem[:24]}",
        "kind": "edit",
        "args": {
            "path": rel,
            "why": "The capture belongs to the project it was gathered for.",
            "operation": {
                "kind": "patch_frontmatter",
                "field": field,
                "value": value,
                "expected_hash": _hash(vault, rel),
            },
        },
    }


def _propose(vault: Path, *steps: dict[str, object]) -> dict[str, object]:
    return curation.propose(vault, {"version": 1, "title": "Rehome", "steps": list(steps)})


def _run(vault: Path, proposed: dict[str, object]) -> dict[str, object]:
    """Approve the plan and resume it until it stops."""
    result = curation.apply(
        vault,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved rehome.",
    )
    while result["phase"] == "executing":
        result = curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    return result


def _compensate(vault: Path, run_id: str) -> dict[str, object]:
    proposed = curation.propose_compensation(vault, run_id=run_id)
    result = curation.apply_compensation(
        vault,
        run_id=run_id,
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Undo the rehome.",
    )
    while result["phase"] not in {"compensated", "failed", "blocked", "partial"}:
        result = curation.resume(vault, run_id=run_id, plan_id=proposed["plan_id"])
    return result


def _lineage(vault: Path, rel: str) -> list[str]:
    fields, _body, _raw = parse_frontmatter((vault / rel).read_text(encoding="utf-8"))
    return [entry["path"] for entry in fields.get("moved_from") or []]


def test_a_rehome_through_the_dispatcher_moves_each_note_once_and_repoints_its_source(
    vault: Path,
) -> None:
    """A rehome that leaves a note without its lineage entry, records one twice, or changes a
    Source beyond the link span that names the moved note."""
    for rel in NOTES:
        _with_identity(vault, rel)
    source = _append(vault, SOURCE, f"See {_link(NOTES[0])} for tuning.")
    captured = source.read_text(encoding="utf-8")
    plan = {
        "version": 1,
        "title": "Rehome the tuning notes",
        "steps": [*(_move_step(rel, project="project-alpha") for rel in NOTES), REPOINT],
    }

    def maintain(action: str, **arguments: object) -> dict[str, object]:
        return writer_lease.invoke_command(
            MAINTAIN, vault, mode="curation", curation_action=action, **arguments
        )

    proposed = maintain("propose", plan=plan)
    result = maintain(
        "apply",
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved rehome.",
    )
    while result["phase"] == "executing":
        result = maintain("resume", run_id=proposed["run_id"], plan_id=proposed["plan_id"])

    assert result["phase"] == "completed"
    for rel in NOTES:
        assert not (vault / rel).exists()
        assert _lineage(vault, _moved(rel)) == [rel]
        fields, _body, _raw = parse_frontmatter((vault / _moved(rel)).read_text(encoding="utf-8"))
        assert fields["project"] == "project-alpha"
    assert source.read_text(encoding="utf-8") == captured.replace(
        _link(NOTES[0]), _link(_moved(NOTES[0]))
    )


def test_a_move_fields_patch_sets_only_pointer_fields(vault: Path) -> None:
    """A rehome that slips a title or body change past the page's review as a pointer patch."""
    with pytest.raises(curation.CurationError, match="CURATION_STEP_INVALID"):
        _propose(vault, _move_step(NOTES[0], title="Another title"))

    assert (vault / NOTES[0]).is_file()


def test_a_move_its_contract_would_refuse_is_refused_at_proposal(vault: Path) -> None:
    """A plan that seals a move whose commit must refuse, so its run stops part way through, after
    the earlier moves committed."""
    misfiled = {
        "step_id": "misfiled",
        "kind": "move",
        "args": {"old_path": NOTES[1], "new_path": f"Knowledge Base/Filed/{Path(NOTES[1]).name}"},
    }

    with pytest.raises(curation.CurationError, match="SEMANTIC_CONTRACT_BLOCKED"):
        _propose(vault, _move_step(NOTES[0]), misfiled)

    assert (vault / NOTES[0]).is_file() and (vault / NOTES[1]).is_file()


def test_two_moved_pages_that_link_to_each_other_propose_and_land_both_rewrites(
    vault: Path,
) -> None:
    """A plan refused as a conflict because the link rewrite binds a page that an earlier move
    wrote, or a rewrite that misses the link inside a moved page."""
    first, second = NOTES[0], NOTES[1]
    _append(vault, first, f"Rolled out with {_link(second)}.")
    _append(vault, second, f"Tuned by {_link(first)}.")

    result = _run(vault, _propose(vault, _move_step(first), _move_step(second), REPOINT))

    assert result["phase"] == "completed"
    assert _link(_moved(second)) in (vault / _moved(first)).read_text(encoding="utf-8")
    assert _link(_moved(first)) in (vault / _moved(second)).read_text(encoding="utf-8")


def test_a_referrer_changed_after_approval_stops_the_rewrite_and_writes_nothing(
    vault: Path,
) -> None:
    """A rewrite computed from the live page instead of the reviewed bytes, which would land a
    change nobody approved, or a partial rewrite that leaves some referrers changed."""
    referrer = _append(vault, NOTES[1], f"Rolled out with {_link(NOTES[0])}.")
    other = _append(vault, NOTES[2], f"Fused with {_link(NOTES[0])}.")
    proposed = _propose(vault, _move_step(NOTES[0]), REPOINT)
    curation.apply(
        vault,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved rehome.",
    )
    _append(vault, NOTES[1], "A later edit.")
    edited, untouched = referrer.read_text(encoding="utf-8"), other.read_text(encoding="utf-8")

    with pytest.raises(curation.CurationError, match="CURATION_BINDING_STALE"):
        curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])

    assert referrer.read_text(encoding="utf-8") == edited
    assert other.read_text(encoding="utf-8") == untouched


def test_a_source_repoint_the_approved_preview_did_not_list_refuses_and_changes_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan approval taken as covering a Source repoint that its preview never showed, so a
    captured page the plan did not name changes without anyone having seen the change."""
    from exomem import pointer_write

    _with_identity(vault, NOTES[0])
    source = _append(vault, SOURCE, f"See {_link(NOTES[0])} for tuning.")
    captured = source.read_text(encoding="utf-8")
    listed = pointer_write.preview_digest
    with monkeypatch.context() as preview:
        # The approved preview lists no repoint, as a preview taken before the link existed did.
        preview.setattr(pointer_write, "preview_digest", lambda moves, _writes: listed(moves, []))
        proposed = _propose(vault, _move_step(NOTES[0]), REPOINT)

    with pytest.raises(curation.CurationError, match="POINTER_PREVIEW_REQUIRED"):
        _run(vault, proposed)

    assert source.read_text(encoding="utf-8") == captured


def test_a_planned_source_repoint_is_listed_in_the_sealed_preview(vault: Path) -> None:
    """A plan whose preview hides that it changes a Source, which the approval then covers."""
    _with_identity(vault, NOTES[0])
    _append(vault, SOURCE, f"See {_link(NOTES[0])} for tuning.")

    proposed = _propose(vault, _move_step(NOTES[0]), REPOINT)

    shown = curation.preview(vault, run_id=proposed["run_id"])["binding_manifest"][-1]
    outcomes = {row["path"]: row["outcome"] for row in shown["prepared"]["referrers"]}
    assert outcomes[SOURCE] == move_file.REPOINTED


def test_an_outside_rename_back_after_the_intent_blocks_without_a_second_rename(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume that renames again after someone put the page back, losing whatever happened in
    between, or that guesses the rename never ran."""
    from exomem import reserved_paths

    proposed = _propose(vault, _move_step(NOTES[0]))
    original = (vault / NOTES[0]).read_bytes()

    def cut(name: str) -> None:
        if name == "move-after-rename":
            raise _ProcessCut(name)

    monkeypatch.setattr(curation, "_fault_barrier", cut)
    with pytest.raises(_ProcessCut):
        _run(vault, proposed)
    monkeypatch.setattr(curation, "_fault_barrier", lambda _name: None)
    (vault / _moved(NOTES[0])).rename(vault / NOTES[0])
    renames: list[object] = []
    monkeypatch.setattr(reserved_paths, "move_generic_path", lambda *a, **k: renames.append(a))

    with pytest.raises(curation.CurationError, match="CURATION_RENAME_HISTORY_UNPROVABLE"):
        curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])

    assert renames == []
    assert (vault / NOTES[0]).read_bytes() == original
    assert curation.status(vault, run_id=proposed["run_id"])["error_code"] == (
        "CURATION_RENAME_HISTORY_UNPROVABLE"
    )


class _ProcessCut(BaseException):  # noqa: N818 - a process cut is not an error: nothing in the step may catch it
    """A stop that no `except Exception` in the step can turn into a recorded failure."""


@pytest.mark.parametrize(
    ("barrier", "renamed"),
    [
        ("move-before-intent", True),
        # The page sits at its old path with the sealed bytes after its intent record, which
        # an outside rename back also leaves, so the run blocks (the design's recovery table).
        ("move-after-intent", False),
        ("move-after-rename", True),
        ("move-after-batch", True),
    ],
)
def test_a_move_cut_at_any_barrier_resumes_to_one_rename_and_one_lineage_entry(
    vault: Path, monkeypatch: pytest.MonkeyPatch, barrier: str, renamed: bool
) -> None:
    """A cut that a resume answers with a second rename, a second lineage entry, a lost entry,
    or a page that the reference index or the graph still places at its old path."""
    from exomem import epistemic_graph, index_sync, memory_refs, reserved_paths

    linker = f"{INSIGHTS}/harbour-log.md"
    _with_identity(vault, NOTES[0])
    (vault / linker).write_text(
        f"---\ntitle: Harbour log\ntype: note\n---\n# Harbour log\n\n- supports {_link(NOTES[0])}\n",
        encoding="utf-8",
    )
    memory_refs.ReferenceIndex(vault).rebuild_all()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    proposed = _propose(vault, _move_step(NOTES[0], project="project-alpha"))
    renames = 0
    rename = reserved_paths.move_generic_path

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal renames
        renames += 1
        return rename(*args, **kwargs)

    def cut(name: str) -> None:
        if name == barrier:
            raise _ProcessCut(name)

    monkeypatch.setattr(reserved_paths, "move_generic_path", counted)
    monkeypatch.setattr(curation, "_fault_barrier", cut)
    with pytest.raises(_ProcessCut):
        _run(vault, proposed)
    monkeypatch.setattr(curation, "_fault_barrier", lambda _name: None)
    if barrier == "move-after-rename":
        assert curation.status(vault, run_id=proposed["run_id"])["recovery"] == (
            "roll-forward-required"
        )
    if not renamed:
        original = (vault / NOTES[0]).read_bytes()
        with pytest.raises(curation.CurationError, match="CURATION_RENAME_HISTORY_UNPROVABLE"):
            _run_resume(vault, proposed)
        assert renames == 0
        assert (vault / NOTES[0]).read_bytes() == original
        assert not (vault / _moved(NOTES[0])).exists()
        return

    result = _run_resume(vault, proposed)

    moved = _moved(NOTES[0])
    assert result["phase"] == "completed"
    assert renames == 1
    assert not (vault / NOTES[0]).exists()
    assert _lineage(vault, moved) == [NOTES[0]]
    incremental = _prior_rows(vault)
    memory_refs.ReferenceIndex(vault).rebuild_all()
    assert incremental == _prior_rows(vault) == [(NOTES[0], moved)]
    # The graph repairs from its queue, as the service's graph drain does within a second.
    index_sync.drain_graph_work(vault)
    with closing(sqlite3.connect(epistemic_graph.sidecar_path(vault))) as conn:
        edges = conn.execute(
            "SELECT DISTINCT dst_key FROM graph_edges WHERE source_path = ?", (linker,)
        ).fetchall()
    assert edges == [(f"file:{moved}",)]


def _prior_rows(vault: Path) -> list[tuple[str, str]]:
    from exomem import memory_refs

    with closing(sqlite3.connect(memory_refs.ReferenceIndex(vault).path)) as conn:
        return sorted(conn.execute("SELECT prior_path, path FROM prior_paths").fetchall())


def _run_resume(vault: Path, proposed: dict[str, object]) -> dict[str, object]:
    result = curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    while result["phase"] == "executing":
        result = curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    return result


def test_an_undone_rehome_restores_every_page_to_its_pre_step_bytes(vault: Path) -> None:
    """An undo that leaves a moved page at its new path, keeps its lineage entry or `project`
    patch, or leaves a referrer or Source pointing at the path the undo vacates."""
    for rel in NOTES[:2]:
        _with_identity(vault, rel)
    _append(vault, NOTES[0], f"Rolled out with {_link(NOTES[1])}.")
    _append(vault, NOTES[2], f"Fused with {_link(NOTES[0])}.")
    _append(vault, SOURCE, f"See {_link(NOTES[0])} for tuning.")
    pages = (*NOTES, SOURCE)
    before = {rel: _hash(vault, rel) for rel in pages}
    proposed = _propose(
        vault,
        _move_step(NOTES[0], project="project-alpha"),
        _move_step(NOTES[1]),
        REPOINT,
    )
    assert _run(vault, proposed)["phase"] == "completed"
    assert _hash(vault, SOURCE) != before[SOURCE]

    result = _compensate(vault, proposed["run_id"])

    assert result["phase"] == "compensated"
    assert {rel: _hash(vault, rel) for rel in pages} == before
    assert not any((vault / _moved(rel)).exists() for rel in NOTES[:2])


def test_a_sources_project_correction_applies_and_its_undo_restores_each_source(
    vault: Path,
) -> None:
    """A pointer correction that mints `updated`, refuses as an append-only change, binds the
    reviewed bytes loosely, or whose undo cannot find the record it reverts."""
    sources = (SOURCE, OTHER_SOURCE)
    before = {rel: (vault / rel).read_text(encoding="utf-8") for rel in sources}
    proposed = _propose(vault, *(_pointer_edit(vault, rel, "project", "project-alpha") for rel in sources))

    assert _run(vault, proposed)["phase"] == "completed"
    for rel in sources:
        fields, body, _raw = parse_frontmatter((vault / rel).read_text(encoding="utf-8"))
        old_fields, old_body, _old = parse_frontmatter(before[rel])
        assert fields == {**old_fields, "project": "project-alpha"}
        assert body == old_body

    result = _compensate(vault, proposed["run_id"])

    assert result["phase"] == "compensated"
    assert {rel: (vault / rel).read_text(encoding="utf-8") for rel in sources} == before


def test_a_curation_edit_of_a_sources_classification_refuses_at_proposal(vault: Path) -> None:
    """A reviewed plan that changes a Source's `source_type` as a plain pointer correction, which
    skips the classification history and the relocation that reclassification makes."""
    before = (vault / SOURCE).read_bytes()

    with pytest.raises(curation.CurationError) as refused:
        _propose(vault, _pointer_edit(vault, SOURCE, "source_type", "report"))

    assert "POINTER_FIELD_OWNED" in str(refused.value)
    assert "reclassif" in str(refused.value)
    assert (vault / SOURCE).read_bytes() == before


@pytest.mark.parametrize("complete", [True, False], ids=["catalogue-current", "catalogue-stale"])
def test_a_plan_whose_mention_lookup_could_not_answer_warns_at_its_top_level(
    vault: Path, monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    """A preview that buries the unchecked-mentions notice in one step's binding, so the owner
    approves a rehome without knowing that prose mentions of the old paths will stay."""
    from exomem import path_mentions

    monkeypatch.setattr(
        path_mentions, "find_pages", lambda _root, _paths: path_mentions.Lookup((), complete)
    )
    _with_identity(vault, NOTES[0])

    proposed = _propose(vault, _move_step(NOTES[0]), REPOINT)
    previewed = curation.preview(vault, run_id=proposed["run_id"])

    expected = [] if complete else [path_mentions.NOT_CHECKED]
    assert proposed.get("warnings", []) == expected
    assert previewed.get("warnings", []) == expected


def test_a_rename_that_raised_in_this_process_is_retried_by_the_resume(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename that failed before it moved the page and leaves its intent record behind, so the
    resume blocks the run as an unprovable history instead of moving the page."""
    from exomem import reserved_paths

    _with_identity(vault, NOTES[0])
    proposed = _propose(vault, _move_step(NOTES[0]))
    rename = reserved_paths.move_generic_path
    calls = 0

    def busy_once(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("the volume was busy")
        return rename(*args, **kwargs)

    monkeypatch.setattr(reserved_paths, "move_generic_path", busy_once)
    with pytest.raises(curation.CurationError):
        _run(vault, proposed)
    assert (vault / NOTES[0]).exists()

    result = _run_resume(vault, proposed)

    assert result["phase"] == "completed"
    assert calls == 2
    assert _lineage(vault, _moved(NOTES[0])) == [NOTES[0]]


def test_a_link_rewrite_commits_under_the_vault_boundary(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A link rewrite takes no guard of its own. Curation must hold the vault boundary across its
    batch itself, whichever dispatcher called it, or the rewrite races other writers."""
    _with_identity(vault, NOTES[0])
    _append(vault, SOURCE, f"See {_link(NOTES[0])} for tuning.")
    proposed = _propose(vault, _move_step(NOTES[0]), REPOINT)
    held_in_batch: dict[str, bool] = {}
    real_finalize = curation._finalize_witness_batch

    def observe_the_batch(vault_root, step, binding, writes, content):  # noqa: ANN001, ANN202
        held_in_batch[step["kind"]] = writer_lease.active_manager().holds_mutation_boundary(vault_root)
        return real_finalize(vault_root, step, binding, writes, content)

    monkeypatch.setattr(curation, "_finalize_witness_batch", observe_the_batch)

    assert _run(vault, proposed)["phase"] == "completed"
    assert held_in_batch == {"move": True, "repoint-links": True}


def test_a_move_with_a_fields_patch_keeps_another_writers_change_to_that_field(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move's fields patch sets its field whatever the page holds. When another writer changed only
    that field after the seal, the moved bytes still matched the seal and the other write was lost."""
    _with_identity(vault, NOTES[0])
    page = vault / NOTES[0]
    page.write_text(
        page.read_text(encoding="utf-8").replace("---\n", "---\nproject: project-gamma\n", 1), encoding="utf-8"
    )
    proposed = _propose(vault, _move_step(NOTES[0], project="project-alpha"))
    real_dispatch = curation._dispatch_step

    def another_writer_sets_the_field_first(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        def write() -> None:
            with writer_lease.active_manager().mutation_guard(vault, operation="project_write"):
                text = page.read_text(encoding="utf-8")
                page.write_text(text.replace("project: project-gamma", "project: project-beta"), encoding="utf-8")

        writer = threading.Thread(target=write, name="project-writer")
        writer.start()
        writer.join(timeout=30.0)
        return real_dispatch(*args, **kwargs)

    monkeypatch.setattr(curation, "_dispatch_step", another_writer_sets_the_field_first)

    with pytest.raises(curation.CurationError, match="SOURCE_CHANGED"):
        _run(vault, proposed)
    assert "project: project-beta" in page.read_text(encoding="utf-8")
    assert not (vault / _moved(NOTES[0])).exists()
