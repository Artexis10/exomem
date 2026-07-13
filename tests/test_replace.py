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
    result = note_module.note(
        vault,
        content=f"# {title}\n\nbody.\n",
        note_type="insight",
        title=title,
        today=TODAY,
    )
    return result.path


def _markdown_snapshot(vault: Path) -> dict[str, str]:
    return {
        path.relative_to(vault).as_posix(): _read(path)
        for path in sorted((vault / "Knowledge Base").rglob("*.md"))
    }


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
        "---\ntype: identity\nscope: products\ncreated: 2026-05-24\nupdated: 2026-05-24\ntags: []\n---\n"
        "# Products\n\nold facts.\n",
        encoding="utf-8",
    )
    result = replace_module.replace(
        vault,
        old_path=rel,
        content="# Products v2\n\nupdated facts.\n",
        note_type="insight",  # new page goes to Notes/Insights/
        title="Identity Products v2",
        today=TODAY,
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
    real_note = note_module.note
    both_eligible = threading.Barrier(2)

    def note_after_both_eligibility_reads(*args, **kwargs):
        both_eligible.wait(timeout=5)
        return real_note(*args, **kwargs)

    monkeypatch.setattr(replace_module.note_module, "note", note_after_both_eligibility_reads)

    def supersede(label: str):
        title = f"Concurrent Successor {label}"
        try:
            return label, replace_module.replace(
                vault,
                old_path=old_rel,
                content=f"# {title}\n\nrevised by {label}.\n",
                note_type="insight",
                title=title,
                today=TODAY,
            )
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
    assert losers[0][1].code == "STALE_SUPERSEDE"

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
    assert f"Concurrent Successor {winner_label}" in log_text
    assert f"Concurrent Successor {loser_label}" not in log_text


def test_replace_rolls_back_entire_note_plan_on_mid_commit_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_rel = _make_insight(vault, "Atomic Supersession Source")
    before = _markdown_snapshot(vault)
    real_batch = replace_module.batch_atomic_write
    real_os_replace = os.replace

    def fail_second_destination(writes, *, vault_root):
        replacements = 0

        def injected_replace(src, dst):
            nonlocal replacements
            if str(src).endswith(".tmp"):
                replacements += 1
                if replacements == 2:
                    raise OSError("injected supersession commit failure")
            return real_os_replace(src, dst)

        with monkeypatch.context() as patch:
            patch.setattr(vault_module.os, "replace", injected_replace)
            return real_batch(writes, vault_root=vault_root)

    monkeypatch.setattr(replace_module, "batch_atomic_write", fail_second_destination)

    with pytest.raises(OSError, match="injected supersession commit failure"):
        replace_module.replace(
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

    assert _markdown_snapshot(vault) == before
    resolver = replace_module.find_module._get_query_resolver(vault)
    assert "Knowledge Base/Notes/Insights/atomic-successor" not in resolver.full_paths
