from __future__ import annotations

from pathlib import Path

from exomem import find as find_module
from exomem import freshness, recall_policy


def test_seed_retries_projection_when_access_policy_changes_during_admission(
    tmp_path: Path, monkeypatch
) -> None:
    page = tmp_path / "Knowledge Base" / "Notes" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("page", encoding="utf-8")
    state = {"fingerprint": "before", "switch": True}

    monkeypatch.setattr(
        recall_policy,
        "recall_policy_identity",
        lambda _root: ("test-policy", state["fingerprint"]),
    )

    def candidate(_root: Path, _path: Path) -> bool:
        eligible = state["fingerprint"] == "before"
        if state["switch"]:
            state["fingerprint"] = "after"
            state["switch"] = False
        return eligible

    monkeypatch.setattr(recall_policy, "is_recall_candidate", candidate)

    freshness.seed(tmp_path, "kb", [(str(page), freshness.stat_signature(page))])

    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")
    assert checkpoint.access_policy_fingerprint == "after"
    assert freshness.live_recall_entries(tmp_path, "kb") == {}
    assert checkpoint.triple == freshness.triple_from_entries(())
    delta = freshness.recall_delta_since(tmp_path, "kb", checkpoint)
    assert delta.complete
    assert delta.to == checkpoint


def test_reconcile_retries_projection_when_access_policy_changes_during_admission(
    tmp_path: Path, monkeypatch
) -> None:
    page = tmp_path / "Knowledge Base" / "Notes" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("page", encoding="utf-8")
    state = {"fingerprint": "before", "switch": False}

    monkeypatch.setattr(
        recall_policy,
        "recall_policy_identity",
        lambda _root: ("test-policy", state["fingerprint"]),
    )

    def candidate(_root: Path, _path: Path) -> bool:
        eligible = state["fingerprint"] == "before"
        if state["switch"]:
            state["fingerprint"] = "after"
            state["switch"] = False
        return eligible

    monkeypatch.setattr(recall_policy, "is_recall_candidate", candidate)
    entries = [(str(page), freshness.stat_signature(page))]
    freshness.seed(tmp_path, "kb", entries)
    before = freshness.recall_checkpoint(tmp_path, "kb")

    state["switch"] = True
    freshness.reconcile(tmp_path, "kb", entries)

    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")
    assert checkpoint.access_policy_fingerprint == "after"
    assert freshness.live_recall_entries(tmp_path, "kb") == {}
    assert checkpoint.triple == freshness.triple_from_entries(())
    delta = freshness.recall_delta_since(tmp_path, "kb", before)
    assert not delta.complete
    assert delta.to == checkpoint


def test_live_recall_checkpoint_reuses_projected_map_without_rewalking(
    tmp_path: Path, monkeypatch
) -> None:
    page = tmp_path / "Knowledge Base" / "Notes" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("page", encoding="utf-8")
    freshness.seed(tmp_path, "kb", [(str(page), freshness.stat_signature(page))])
    first = freshness.recall_checkpoint(tmp_path, "kb")

    monkeypatch.setattr(
        recall_policy,
        "is_recall_candidate",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not walk or read")),
    )

    assert freshness.recall_checkpoint(tmp_path, "kb") == first


def test_request_snapshots_reuse_live_relative_path_projection_until_checkpoint_moves(
    tmp_path: Path, monkeypatch
) -> None:
    find_module.clear_cache()
    page = tmp_path / "Knowledge Base" / "Notes" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("page", encoding="utf-8")
    freshness.seed(tmp_path, "vault", [(str(page), freshness.stat_signature(page))])

    calls = 0
    original = freshness.recall_projection_snapshot

    def counted(root: Path, scope: str):
        nonlocal calls
        calls += 1
        return original(root, scope)

    monkeypatch.setattr(freshness, "recall_projection_snapshot", counted)

    first = find_module.FreshnessSnapshot(tmp_path).recall_paths("vault")
    second = find_module.FreshnessSnapshot(tmp_path).recall_paths("vault")

    assert first == second == {"Knowledge Base/Notes/page.md"}
    assert calls == 1

    added = page.with_name("added.md")
    added.write_text("added", encoding="utf-8")
    freshness.on_files_changed(tmp_path, changed=[added])

    third = find_module.FreshnessSnapshot(tmp_path).recall_paths("vault")
    assert third == {"Knowledge Base/Notes/added.md", "Knowledge Base/Notes/page.md"}
    assert calls == 2
    find_module.clear_cache()


def test_raw_event_moves_broad_cursor_not_live_recall_projection(tmp_path: Path) -> None:
    raw = tmp_path / "Knowledge Base" / "Records" / "raw.md"
    raw.parent.mkdir(parents=True)
    raw.write_text("raw", encoding="utf-8")
    freshness.seed(tmp_path, "kb", [(str(raw), freshness.stat_signature(raw))])
    before = freshness.recall_checkpoint(tmp_path, "kb")
    broad_before = freshness.consumer_checkpoint(tmp_path, "kb")

    raw.write_text("changed", encoding="utf-8")
    freshness.on_files_changed(tmp_path, changed=[raw])

    assert freshness.consumer_checkpoint(tmp_path, "kb").generation > broad_before.generation
    after = freshness.recall_checkpoint(tmp_path, "kb")
    assert (after.triple, after.generation) == (before.triple, before.generation)


def test_raw_history_overflow_does_not_overflow_the_projected_delta(tmp_path: Path) -> None:
    note = tmp_path / "Knowledge Base" / "Notes" / "note.md"
    raw = tmp_path / "Knowledge Base" / "Records" / "raw.md"
    note.parent.mkdir(parents=True)
    raw.parent.mkdir(parents=True)
    note.write_text("note", encoding="utf-8")
    raw.write_text("raw", encoding="utf-8")
    entries = [(str(note), freshness.stat_signature(note)), (str(raw), freshness.stat_signature(raw))]
    freshness.seed(tmp_path, "kb", entries)
    before = freshness.recall_checkpoint(tmp_path, "kb")

    for index in range(freshness.DELTA_HISTORY_LIMIT + 2):
        raw.write_text(f"raw {index}", encoding="utf-8")
        freshness.on_files_changed(tmp_path, changed=[raw])

    assert freshness.recall_checkpoint(tmp_path, "kb") == before
    note.write_text("changed note", encoding="utf-8")
    freshness.on_files_changed(tmp_path, changed=[note])
    delta = freshness.recall_delta_since(tmp_path, "kb", before)
    assert delta.complete
    assert delta.changed == frozenset({str(note)})
