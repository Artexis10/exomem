"""The Source backlog in the maintained due-state projection.

A write to a note refreshes the rows of the Sources that note cites, so the served
count falls when a live note compiles a Source and holds when a draft does. Dismissing
items lowers what is visible but never what is outstanding.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from exomem import commands, freshness, set_frontmatter_field
from exomem import delete_file as delete_file_module
from exomem import due_state as due_state_module
from exomem import find as find_module
from exomem import note as note_module
from exomem.capabilities import ActiveSurfaceDescriptor, active_surface
from exomem.vault import WikilinkResolver

pytestmark = pytest.mark.usefixtures("obligations_only")

#: The real rebuild, kept before a fixture disables it, so a test can compare the
#: served count with what a full pass over the vault finds.
_RECOMPUTE = due_state_module.recompute
_RECONCILE = due_state_module.reconcile


def _import_sources(vault: Path, count: int) -> list[str]:
    paths = []
    for index in range(count):
        rel = f"Knowledge Base/Sources/Articles/document-{index}.md"
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\ntype: source\ningested_into: []\ncaptured: 2026-01-01\n---\n\n"
            f"# Document {index}\n\nImported text {index}.\n",
            encoding="utf-8",
        )
        paths.append(rel)
    return paths


def _compile(vault: Path, title: str, *, status: str, source: str) -> str:
    """Write a note that cites `source` through the governed writer."""
    arguments = {
        "content": "## Claim\n\nCompiled from the import.\n",
        "note_type": "insight",
        "title": title,
        "status": status,
        "sources": [source.removesuffix(".md")],
    }
    validation = note_module.note(vault, validate_only=True, **arguments)
    return note_module.note(
        vault,
        **arguments,
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="No relation exists in this fixture.",
    ).path


def _write_prediction(vault: Path, source: str, *, verdict: str | None = None) -> str:
    """An overdue prediction that cites `source`, and the Source's citation edge to it.

    Written to disk with no projection delta run, as a writer leaves it just
    before its post-commit delta.
    """
    rel = "Knowledge Base/Notes/Insights/compiled-claim.md"
    rows = ["- id: p1", "- check_by: 2026-01-01"] + ([f"- verdict: {verdict}"] if verdict else [])
    (vault / rel).write_text(
        "---\ntype: insight\ntitle: Compiled claim\nstatus: active\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
        f'sources:\n  - "[[{source.removesuffix(".md")}]]"\n---\n\n'
        "## Prediction\n\n" + "\n".join(rows) + "\n\nThe backlog clears within a week.\n",
        encoding="utf-8",
    )
    cited = vault / source
    cited.write_text(
        cited.read_text(encoding="utf-8").replace(
            "ingested_into: []", f'ingested_into:\n  - "[[{rel.removesuffix(".md")}]]"'
        ),
        encoding="utf-8",
    )
    return rel


def _due_predictions(vault: Path) -> int:
    return _block(vault).get("categories", {}).get("prediction_window", 0)


def _block(vault: Path) -> dict:
    return due_state_module.served(vault) or {}


def _outstanding_sources(vault: Path) -> int:
    return _block(vault).get("categories", {}).get("unprocessed_source", 0)


def _recomputed_sources(vault: Path) -> int:
    return len(_RECOMPUTE(vault)["categories"]["unprocessed_source"])


def _no_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_rebuild(*_args, **_kwargs):
        raise AssertionError("a write must refresh its Sources without a vault-wide rebuild")

    monkeypatch.setattr(due_state_module, "reconcile", no_rebuild)
    monkeypatch.setattr(due_state_module, "recompute", no_rebuild)


@pytest.fixture
def imported(obligations_only: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[str]]:
    """Three outstanding Sources in a built projection, with every full rebuild disabled."""
    vault = obligations_only
    sources = _import_sources(vault, 3)
    due_state_module.reconcile(vault)
    _no_rebuild(monkeypatch)
    return vault, sources


def test_a_live_note_citing_a_source_lowers_the_served_count(
    imported: tuple[Path, list[str]],
) -> None:
    vault, sources = imported
    assert _outstanding_sources(vault) == 3

    _compile(vault, "Compiled claim", status="active", source=sources[0])

    assert _outstanding_sources(vault) == 2
    assert _block(vault)["outstanding"]["unprocessed_source"] == 2


def test_a_draft_note_citing_a_source_leaves_the_count_unchanged_until_promoted(
    imported: tuple[Path, list[str]],
) -> None:
    vault, sources = imported
    draft = _compile(vault, "Draft claim", status="draft", source=sources[0])
    assert _outstanding_sources(vault) == 3

    arguments = {"path": draft, "field": "status", "value": "active", "why": "reviewed and promoted"}
    preview = set_frontmatter_field.set_frontmatter_field(vault, validate_only=True, **arguments)
    set_frontmatter_field.set_frontmatter_field(
        vault,
        **arguments,
        semantic_transition_token=preview.semantic["transition_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=preview.semantic["relation_review_hash"],
        relation_review_reason="No relation exists in this fixture.",
    )

    assert _outstanding_sources(vault) == 2


def test_trashing_the_live_note_returns_its_source_to_the_count(
    imported: tuple[Path, list[str]],
) -> None:
    vault, sources = imported
    note = _compile(vault, "Compiled claim", status="active", source=sources[0])
    assert _outstanding_sources(vault) == 2

    # The leaf itself, as curation calls it; the Source still names the note.
    delete_file_module.delete_file(vault, path=note, confirm=True, force_orphan=True)

    assert _outstanding_sources(vault) == _recomputed_sources(vault) == 3


def test_capturing_a_source_adds_it_to_the_served_count(
    imported: tuple[Path, list[str]], source_schema: object,
) -> None:
    vault, _sources = imported
    (vault / "Knowledge Base/Sources/index.md").write_text(
        "# Sources — Index\n\n## By type\n\n## Recent captures\n\n", encoding="utf-8"
    )

    commands.op_capture_source(
        vault, source_schema, title="A new capture", content="Captured text.", source_kind="session"
    )

    assert _outstanding_sources(vault) == _recomputed_sources(vault) == 4


def test_trashing_a_folder_returns_the_sources_its_live_notes_compiled(
    obligations_only: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = obligations_only
    folder = "Knowledge Base/Notes/Batch"
    (vault / folder).mkdir(parents=True)
    for index, source in enumerate(_import_sources(vault, 2)):
        page = vault / source
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                "ingested_into: []", f"ingested_into: ['[[batch-note-{index}]]']"
            ),
            encoding="utf-8",
        )
        (vault / folder / f"batch-note-{index}.md").write_text(
            f"---\ntype: insight\nstatus: active\nsources: ['[[document-{index}]]']\n---\n\n"
            f"# Batch note {index}\n",
            encoding="utf-8",
        )
    _RECONCILE(vault)
    assert _outstanding_sources(vault) == 0
    _no_rebuild(monkeypatch)

    commands.op_delete(vault, folder, confirm=True, recursive=True, force_orphan=True)

    assert _outstanding_sources(vault) == _recomputed_sources(vault) == 2


def test_dismissing_items_lowers_what_is_visible_and_keeps_outstanding(
    imported: tuple[Path, list[str]],
) -> None:
    vault, _sources = imported
    refs = [row["ref"] for row in due_state_module.served_entries(vault)]
    assert len(refs) == 3

    for ref in refs[:2]:
        commands.op_triage_memory(vault, ref=ref, action="dismiss", why="grouped later")

    block = _block(vault)
    assert block["total"] == 1
    assert block["categories"] == {"unprocessed_source": 1}
    assert block["outstanding_total"] == 3
    assert block["outstanding"] == {"unprocessed_source": 3}


def test_a_compact_bootstrap_points_at_the_next_batch_and_keeps_dismissals_outstanding(
    imported: tuple[Path, list[str]],
) -> None:
    vault, _sources = imported
    for row in due_state_module.served_entries(vault)[:2]:
        commands.op_triage_memory(vault, ref=row["ref"], action="dismiss", why="grouped later")

    summary = commands.op_bootstrap(vault)["due_state"]

    assert summary["total"] == 1
    assert summary["visible_outstanding"] == {"unprocessed_source": [1, 3]}
    assert summary["next"] == (
        "review_memory(mode='attention', categories=['unprocessed_source'], limit=10)"
    )


def test_dismissing_every_item_still_serves_the_outstanding_backlog(
    imported: tuple[Path, list[str]],
) -> None:
    vault, _sources = imported
    for row in due_state_module.served_entries(vault):
        commands.op_triage_memory(vault, ref=row["ref"], action="dismiss", why="grouped later")

    block = _block(vault)
    assert block["total"] == 0
    assert block["categories"] == {}
    assert block["outstanding_total"] == 3
    assert block["outstanding"] == {"unprocessed_source": 3}

    summary = commands.op_bootstrap(vault)["due_state"]
    assert summary["total"] == 0
    assert summary["visible_outstanding"] == {"unprocessed_source": [0, 3]}
    assert "next" not in summary


def test_a_surface_without_review_gets_the_counts_but_no_route_to_a_missing_tool(
    imported: tuple[Path, list[str]],
) -> None:
    vault, _sources = imported
    descriptor = ActiveSurfaceDescriptor(
        surface="test",
        profile="tier-one-only",
        tier2_enabled=False,
        product_commands=("bootstrap", "search_memory"),
    )

    with active_surface(descriptor):
        summary = commands.op_bootstrap(vault)["due_state"]

    assert summary["visible_outstanding"] == {"unprocessed_source": [3, 3]}
    assert "next" not in summary


@pytest.fixture
def resolver_builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The id of the thread that ran each full vault walk of a `WikilinkResolver`."""
    builders: list[int] = []
    original = WikilinkResolver._build

    def recording(self: WikilinkResolver) -> None:
        builders.append(threading.get_ident())
        original(self)

    monkeypatch.setattr(WikilinkResolver, "_build", recording)
    return builders


SOURCE_WORKER = "exomem-due-state-source-backlog"


def _source_worker() -> threading.Thread | None:
    return next((worker for worker in threading.enumerate() if worker.name == SOURCE_WORKER), None)


def _join_source_worker() -> None:
    if (worker := _source_worker()) is not None:
        worker.join(timeout=60)


def test_a_write_delta_never_builds_the_vault_resolver_from_disk_on_the_calling_thread(
    imported: tuple[Path, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    resolver_builds: list[int],
) -> None:
    monkeypatch.delenv("EXOMEM_SYNC_DUE_STATE_WARM", raising=False)
    vault, sources = imported
    page = _write_prediction(vault, sources[0])
    # Neither cheap route can answer: no resolver is cached, and an out-of-band
    # event fences the vault, so the corpus context declines too.
    find_module.clear_cache()
    epoch = freshness.mark_external_pending(vault)
    try:
        advisory = due_state_module.block_for_write(vault, page)
        _join_source_worker()
    finally:
        freshness.clear_external_pending(vault, through=epoch)

    assert threading.get_ident() not in resolver_builds
    assert resolver_builds, "the deferred Source half never walked the vault"
    # The write's own advisory arrives with the response; only the Source rows wait.
    assert advisory is not None and advisory["categories"]["prediction_window"] == 1
    assert _outstanding_sources(vault) == 2


def test_a_write_delta_with_a_warm_resolver_applies_inline(
    imported: tuple[Path, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    resolver_builds: list[int],
) -> None:
    monkeypatch.delenv("EXOMEM_SYNC_DUE_STATE_WARM", raising=False)
    vault, sources = imported
    page = _write_prediction(vault, sources[0])
    find_module.shared_resolver(vault)
    resolver_builds.clear()

    updated = due_state_module.apply_write_delta(vault, page)

    assert updated is not None
    assert _outstanding_sources(vault) == 2
    assert resolver_builds == []
    assert _source_worker() is None


def test_a_deferred_source_half_does_not_overwrite_a_later_write_of_the_same_page(
    imported: tuple[Path, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_SYNC_DUE_STATE_WARM", raising=False)
    vault, sources = imported
    walking, resume = threading.Event(), threading.Event()
    build = WikilinkResolver._build

    def gated(self: WikilinkResolver) -> None:
        if threading.current_thread().name == SOURCE_WORKER:
            walking.set()
            resume.wait(timeout=60)
        build(self)

    monkeypatch.setattr(WikilinkResolver, "_build", gated)
    page = _write_prediction(vault, sources[0])
    find_module.clear_cache()
    epoch = freshness.mark_external_pending(vault)
    try:
        # The first write defers its Source half to a worker that stops mid-walk.
        due_state_module.apply_write_delta(vault, page)
        assert walking.wait(timeout=30)
        assert _due_predictions(vault) == 1
        worker = _source_worker()
        # A daemon worker dies with a one-shot process (the command line) before it lands.
        assert worker is not None and not worker.daemon

        # A second write resolves the prediction while the worker is still walking.
        _write_prediction(vault, sources[0], verdict="confirmed")
        find_module.shared_resolver(vault)
        due_state_module.apply_write_delta(vault, page)
        assert _due_predictions(vault) == 0
        resume.set()
        _join_source_worker()
    finally:
        resume.set()
        freshness.clear_external_pending(vault, through=epoch)

    # The stored rows, not the served block: a serve re-checks rows against the page.
    stored = due_state_module.load(vault)["categories"]
    assert stored["prediction_window"] == _RECOMPUTE(vault)["categories"]["prediction_window"] == {}
    assert _outstanding_sources(vault) == _recomputed_sources(vault) == 2
