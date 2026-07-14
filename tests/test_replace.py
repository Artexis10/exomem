"""replace tool tests — supersession chain integrity per SKILL rule 6."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import os
import threading
from pathlib import Path

import pytest
import yaml

from exomem import note as note_module
from exomem import replace as replace_module
from exomem import vault as vault_module

TODAY = dt.date(2026, 5, 25)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fm(p: Path) -> dict:
    fm = _read(p).split("\n---\n")[0].removeprefix("---\n")
    return yaml.safe_load(fm)


def _make_insight(vault: Path, title: str) -> str:
    """Create a fresh insight via note() so the supersession chain has a real target."""
    kwargs = {
        "content": f"# {title}\n\nbody.\n",
        "note_type": "insight",
        "title": title,
        "today": TODAY,
    }
    validation = note_module.note(
        vault,
        validate_only=True,
        **kwargs,
    )
    result = note_module.note(
        vault,
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="Fixture predecessor has no honest relation.",
        **kwargs,
    )
    return result.path


def _markdown_snapshot(vault: Path) -> dict[str, str]:
    return {
        path.relative_to(vault).as_posix(): _read(path)
        for path in sorted((vault / "Knowledge Base").rglob("*.md"))
    }


def _preflight_replace(vault: Path, **kwargs):  # noqa: ANN003, ANN202
    validation = replace_module.replace(vault, validate_only=True, **kwargs)
    return {
        **kwargs,
        "draft_id": validation.draft_id,
        "draft_hash": validation.draft_hash,
        "draft_token": validation.draft_token,
    }


def _commit_replace(vault: Path, **kwargs):  # noqa: ANN003, ANN202
    return replace_module.replace(vault, **_preflight_replace(vault, **kwargs))


def _assert_cause_contains(error: BaseException, text: str) -> None:
    current: BaseException | None = error
    while current is not None:
        if text in str(current):
            return
        current = current.__cause__
    raise AssertionError(f"{text!r} missing from exception cause chain")


def test_replace_writes_new_and_flips_old(vault: Path) -> None:
    old_rel = _make_insight(vault, "Old Insight v1")
    result = replace_module.replace(
        vault,
        old_path=old_rel,
        content="# Old Insight v2\n\nrevised body.\n",
        note_type="insight",
        title="Old Insight v2",
        today=TODAY,
    )
    new_abs = vault / result.new_path
    old_abs = vault / result.old_path
    assert new_abs.exists()
    assert old_abs.exists()  # old stays; never deleted

    new_fm = _fm(new_abs)
    old_fm = _fm(old_abs)
    assert old_fm["status"] == "superseded"
    # superseded_by: list-of-string-wikilinks (parsed by YAML)
    superseded_by = old_fm["superseded_by"]
    assert isinstance(superseded_by, list)
    assert any(result.new_path.removesuffix(".md") in s for s in superseded_by)
    # New page declares supersedes:
    supersedes = new_fm["supersedes"]
    assert old_rel.removesuffix(".md") in str(supersedes)


def test_replace_renders_supersession_links_for_kb_rooted_obsidian(vault: Path) -> None:
    (vault / "Knowledge Base" / ".obsidian").mkdir()
    old_rel = _make_insight(vault, "Nested Root Old")

    result = replace_module.replace(
        vault,
        old_path=old_rel,
        content="# Nested Root New\n\nrevised.\n",
        note_type="insight",
        title="Nested Root New",
        today=TODAY,
    )

    old_text = _read(vault / result.old_path)
    new_text = _read(vault / result.new_path)
    assert "[[Notes/Insights/nested-root-new]]" in old_text
    assert "[[Notes/Insights/nested-root-old]]" in new_text
    assert "[[Knowledge Base/" not in old_text + new_text


def test_replace_bumps_old_updated_date(vault: Path) -> None:
    old_rel = _make_insight(vault, "Bumped Insight")
    later = dt.date(2026, 6, 1)
    replace_module.replace(
        vault,
        old_path=old_rel,
        content="# Bumped v2\n\nbody.\n",
        note_type="insight",
        title="Bumped v2",
        today=later,
    )
    old_fm = _fm(vault / old_rel)
    assert old_fm["updated"] == later


def test_replace_refuses_when_old_in_sources(vault: Path) -> None:
    """SKILL rule 2: Sources/ is append-only — can't be superseded."""
    src_rel = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md"
    with pytest.raises(replace_module.ReplaceError) as exc:
        replace_module.replace(
            vault,
            old_path=src_rel,
            content="# x\n",
            note_type="insight",
            title="x",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_REPLACE"


def test_replace_refuses_when_old_already_superseded(vault: Path) -> None:
    old_rel = _make_insight(vault, "Double Superseded")
    replace_module.replace(
        vault,
        old_path=old_rel,
        content="# v2\n",
        note_type="insight",
        title="Double Superseded v2",
        today=TODAY,
    )
    with pytest.raises(replace_module.ReplaceError) as exc:
        replace_module.replace(
            vault,
            old_path=old_rel,  # already superseded
            content="# v3\n",
            note_type="insight",
            title="Double Superseded v3",
            today=TODAY,
        )
    assert exc.value.code == "ALREADY_SUPERSEDED"


def test_replace_refuses_when_old_not_found(vault: Path) -> None:
    with pytest.raises(replace_module.ReplaceError) as exc:
        replace_module.replace(
            vault,
            old_path="Knowledge Base/Notes/Insights/nope.md",
            content="# x\n",
            note_type="insight",
            title="x",
            today=TODAY,
        )
    assert exc.value.code == "OLD_NOT_FOUND"


def test_replace_logs_with_reason(vault: Path) -> None:
    old_rel = _make_insight(vault, "Reasoned Replace")
    replace_module.replace(
        vault,
        old_path=old_rel,
        content="# v2\n",
        note_type="insight",
        title="Reasoned Replace v2",
        reason="Old framing was too narrow; broader scope here.",
        today=TODAY,
    )
    log = _read(vault / "Knowledge Base" / "log.md")
    assert "## [2026-05-25] replace |" in log
    assert "Old framing was too narrow" in log


def test_replace_does_not_retarget_inbound_wikilinks(vault: Path) -> None:
    """Rule 6: readers follow the supersession chain; inbound links stay on old."""
    old_rel = _make_insight(vault, "Linked Insight")
    referrer = vault / "Knowledge Base" / "Notes" / "Insights" / "links-to-old.md"
    old_no_ext = old_rel.removesuffix(".md")
    referrer.write_text(
        f"---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n"
        f"# Linker\n\nSee [[{old_no_ext}]].\n",
        encoding="utf-8",
    )
    replace_module.replace(
        vault,
        old_path=old_rel,
        content="# v2\n",
        note_type="insight",
        title="Linked Insight v2",
        today=TODAY,
    )
    referrer_text = _read(referrer)
    # Wikilink to old still points at old (unchanged).
    assert f"[[{old_no_ext}]]" in referrer_text


def test_replace_accepts_novel_type(vault: Path) -> None:
    """Regression: replace used to refuse any type not in a hardcoded set.

    A page with `type: identity` (or any novel type) outside Sources/Evidence
    should be supersedable. The new page is still constructed via note() so
    it lands in the standard typed folder routing; the only thing the
    type-allowlist removal changes is whether the OLD page can be the
    target of the supersession.
    """
    rel = "Knowledge Base/Identity/Products.md"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: identity\nscope: products\ncreated: 2026-05-24\n"
        "updated: 2026-05-24\ntags: []\n---\n"
        "# Products\n\nold facts.\n",
        encoding="utf-8",
    )
    kwargs = {
        "old_path": rel,
        "content": "# Products v2\n\nupdated facts.\n",
        "note_type": "insight",  # new page goes to Notes/Insights/
        "title": "Identity Products v2",
        "today": TODAY,
    }
    validation = replace_module.replace(vault, validate_only=True, **kwargs)
    result = replace_module.replace(
        vault,
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="Legacy identity predecessor is outside the compiled corpus.",
        **kwargs,
    )
    # Old page flipped to superseded
    old_fm = _fm(vault / rel)
    assert old_fm["status"] == "superseded"
    assert any(
        result.new_path.removesuffix(".md") in str(s)
        for s in old_fm["superseded_by"]
    )
    # New page exists with supersedes pointer
    new_fm = _fm(vault / result.new_path)
    assert rel.removesuffix(".md") in str(new_fm["supersedes"])


def test_replace_propagates_new_note_validation_errors(vault: Path) -> None:
    """If the new-page args are invalid, the supersession is aborted."""
    old_rel = _make_insight(vault, "Validation Source")
    with pytest.raises((note_module.NoteError, ValueError)):
        replace_module.replace(
            vault,
            old_path=old_rel,
            content="# x\n",
            note_type="research-note",
            title="needs-project",
            # missing required `project` for research-note → NoteError
            today=TODAY,
        )
    # Old should be untouched (no half-state)
    old_fm = _fm(vault / old_rel)
    assert old_fm.get("status") != "superseded"


def test_concurrent_replace_has_exactly_one_winner(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_rel = _make_insight(vault, "Concurrent Supersession Source")
    prepared = {
        label: _preflight_replace(
            vault,
            old_path=old_rel,
            content=f"# Concurrent Successor {label}\n\nrevised by {label}.\n",
            note_type="insight",
            title=f"Concurrent Successor {label}",
            today=TODAY,
        )
        for label in ("A", "B")
    }
    real_note = note_module.note
    both_eligible = threading.Barrier(2)

    def note_after_both_eligibility_reads(*args, **kwargs):
        both_eligible.wait(timeout=5)
        return real_note(*args, **kwargs)

    monkeypatch.setattr(replace_module.note_module, "note", note_after_both_eligibility_reads)

    def supersede(label: str):
        try:
            return label, replace_module.replace(vault, **prepared[label])
        except replace_module.ReplaceError as error:
            return label, error

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(supersede, ("A", "B")))

    winners = [
        (label, result)
        for label, result in outcomes
        if isinstance(result, replace_module.ReplaceResult)
    ]
    losers = [
        (label, result)
        for label, result in outcomes
        if isinstance(result, replace_module.ReplaceError)
    ]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0][1].code in {
        "ALREADY_SUPERSEDED",
        "PATH_GUARD_CHANGED",
        "SEMANTIC_CONTRACT_BLOCKED",
    }

    winner_label, winner = winners[0]
    loser_label = losers[0][0]
    old_fm = _fm(vault / old_rel)
    assert old_fm["status"] == "superseded"
    assert old_fm["superseded_by"] == [
        f"[[{winner.new_path.removesuffix('.md')}]]"
    ]
    assert (vault / winner.new_path).exists()
    loser_path = (
        vault
        / "Knowledge Base"
        / "Notes"
        / "Insights"
        / f"concurrent-successor-{loser_label.lower()}.md"
    )
    assert not loser_path.exists()
    log_text = _read(vault / "Knowledge Base" / "log.md")
    winner_log_target = winner.new_path.removeprefix("Knowledge Base/").removesuffix(".md")
    assert winner_log_target in log_text
    assert f"concurrent-successor-{loser_label.lower()}" not in log_text


def test_replace_rolls_back_entire_note_plan_on_mid_commit_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_rel = _make_insight(vault, "Atomic Supersession Source")
    before = _markdown_snapshot(vault)
    real_os_replace = os.replace
    replacements = 0

    def injected_replace(src, dst, *args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal replacements
        if str(src).endswith(".tmp"):
            replacements += 1
            if replacements == 2:
                raise OSError("injected supersession commit failure")
        return real_os_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(vault_module.os, "replace", injected_replace)

    with pytest.raises(replace_module.ReplaceError) as failure:
        _commit_replace(
            vault,
            old_path=old_rel,
            content="# Atomic Successor\n\nreplacement body.\n",
            note_type="insight",
            title="Atomic Successor",
            sources=[
                "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements"
            ],
            today=TODAY,
        )
    assert failure.value.code == "SEMANTIC_CREATION_FAILED"
    _assert_cause_contains(failure.value, "injected supersession commit failure")

    assert _markdown_snapshot(vault) == before
    resolver = replace_module.find_module._get_query_resolver(vault)
    assert "Knowledge Base/Notes/Insights/atomic-successor" not in resolver.full_paths


@pytest.mark.parametrize("registry_existed", [False, True])
def test_replace_failure_does_not_leave_project_registration_or_folder(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry_existed: bool,
) -> None:
    old_rel = _make_insight(vault, "Project Registration Supersession Source")
    registry = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    if registry_existed:
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            "projects:\n"
            "  personal:\n"
            "    folder: Personal\n"
            "    category: cross-cutting\n",
            encoding="utf-8",
        )
    project_folder = (
        vault
        / "Knowledge Base"
        / "Notes"
        / "Research"
        / "Atomic Deferred Project"
    )
    registry_before = _read(registry) if registry_existed else None
    folder_existed = project_folder.exists()
    real_os_replace = os.replace
    replacements = 0

    def injected_replace(src, dst, *args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal replacements
        if str(src).endswith(".tmp"):
            replacements += 1
            if replacements == 2:
                raise OSError("injected project supersession failure")
        return real_os_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(vault_module.os, "replace", injected_replace)

    with pytest.raises(replace_module.ReplaceError) as failure:
        _commit_replace(
            vault,
            old_path=old_rel,
            content="# Atomic Project Successor\n\nreplacement body.\n",
            note_type="research-note",
            title="Atomic Project Successor",
            project="atomic-deferred-project",
            today=TODAY,
        )
    assert failure.value.code == "SEMANTIC_CREATION_FAILED"
    _assert_cause_contains(failure.value, "injected project supersession failure")

    assert registry.exists() is registry_existed
    if registry_existed:
        assert _read(registry) == registry_before
    assert project_folder.exists() is folder_existed


def test_replace_staging_failure_cleans_temp_registry_and_project_folder(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_rel = _make_insight(vault, "Staging Failure Supersession Source")
    registry = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    project_folder = (
        vault
        / "Knowledge Base"
        / "Notes"
        / "Research"
        / "Staging Failure Project"
    )
    real_create_artifact = vault_module._BatchWorkspace.create_artifact
    stage_writes = 0

    def fail_second_stage(workspace, name: str, content: bytes):  # noqa: ANN001
        nonlocal stage_writes
        if name.startswith("stage-"):
            stage_writes += 1
            if stage_writes == 2:
                raise OSError("injected supersession staging failure")
        return real_create_artifact(workspace, name, content)

    monkeypatch.setattr(vault_module._BatchWorkspace, "create_artifact", fail_second_stage)

    with pytest.raises(replace_module.ReplaceError) as failure:
        _commit_replace(
            vault,
            old_path=old_rel,
            content="# Staging Failure Successor\n\nreplacement body.\n",
            note_type="research-note",
            title="Staging Failure Successor",
            project="staging-failure-project",
            today=TODAY,
        )
    assert failure.value.code == "SEMANTIC_CREATION_FAILED"
    _assert_cause_contains(failure.value, "injected supersession staging failure")

    assert not registry.exists()
    assert list(vault.rglob(".exomem-batch-*")) == []
    assert not project_folder.exists()


def test_plural_project_registration_creates_folder_only_on_success(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_rel = _make_insight(vault, "Plural Project Supersession Source")
    registry = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    failed_folder = (
        vault / "Knowledge Base" / "Notes" / "Research" / "Failed Plural Project"
    )
    successful_folder = (
        vault
        / "Knowledge Base"
        / "Notes"
        / "Research"
        / "Successful Plural Project"
    )
    real_os_replace = os.replace
    replacements = 0

    def injected_replace(src, dst, *args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal replacements
        if str(src).endswith(".tmp"):
            replacements += 1
            if replacements == 2:
                raise OSError("injected plural project failure")
        return real_os_replace(src, dst, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(vault_module.os, "replace", injected_replace)
        with pytest.raises(replace_module.ReplaceError) as failure:
            _commit_replace(
                vault,
                old_path=old_rel,
                content="# Failed Plural Successor\n\nreplacement body.\n",
                note_type="insight",
                title="Failed Plural Successor",
                projects=["failed-plural-project"],
                today=TODAY,
            )
        assert failure.value.code == "SEMANTIC_CREATION_FAILED"
        _assert_cause_contains(failure.value, "injected plural project failure")

    assert not registry.exists()
    assert not failed_folder.exists()

    _commit_replace(
        vault,
        old_path=old_rel,
        content="# Successful Plural Successor\n\nreplacement body.\n",
        note_type="insight",
        title="Successful Plural Successor",
        projects=["successful-plural-project"],
        today=TODAY,
    )

    assert "successful-plural-project:" in _read(registry)
    assert "failed-plural-project:" not in _read(registry)
    assert successful_folder.is_dir()
