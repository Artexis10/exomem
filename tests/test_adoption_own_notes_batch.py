"""Own-note import commits a chunk of draft notes as one canonical batch, note by note answerable."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_adoption_collection import TODAY, _import, _vault
from test_adoption_own_notes import JOURNAL_RULE, _notes

from exomem import adopt, adoption_run, vault, vocabulary_auxiliaries

COLLECTION = "Coll"
KB = "Knowledge Base"


def _chunk(tmp_path: Path, count: int = 3) -> tuple[Path, list[adopt.DraftNote], str]:
    """A vault whose chunk has its Sources on disk and no note yet, as an import leaves it.

    The notes are held back through the run's own per-note step, so the Sources and the
    plan are the real ones; nothing else is faked.
    """
    files = {f"Coll/Journal/entry-{n}.md": f"# Entry {n}\n\nText {n}.\n" for n in range(count)}
    root = _vault(tmp_path, files)
    run_id, planned = _import(root, path="Coll", include=[JOURNAL_RULE])
    held_back = pytest.MonkeyPatch()
    try:
        held_back.setattr(adopt, "create_draft_note", _refuse_every_note)
        applied = adoption_run.apply(
            root, run_id=run_id, plan_id=planned["plan"]["plan_id"], today=TODAY
        )
    finally:
        held_back.undo()
    assert _notes(root) == []
    notes = [
        adopt.DraftNote(
            item,
            (root / item["original_path"]).read_text(encoding="utf-8"),
            applied["outcomes"][item["original_path"]]["target_path"],
        )
        for item in planned["plan"]["items"]
    ]
    return root, notes, planned["plan"]["date"]


def _refuse_every_note(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("held back by the test")


def _recent_activity(root: Path) -> list[str]:
    text = (root / KB / "index.md").read_text(encoding="utf-8")
    return [line for line in text.split("## Recent activity")[1].splitlines() if line.startswith("- ")]


def _log_entries(root: Path) -> list[str]:
    text = (root / KB / "log.md").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.startswith("## [")]


def test_a_chunk_of_notes_commits_with_one_bullet_and_one_log_entry_per_note(
    tmp_path: Path,
) -> None:
    root, notes, date_iso = _chunk(tmp_path)

    outcomes = adopt.create_draft_notes(root, notes, collection=COLLECTION, date_iso=date_iso)

    assert [outcome.error for outcome in outcomes] == [None, None, None]
    assert [outcome.created.path for outcome in outcomes] == [
        item.planned["note"]["target_path"] for item in notes
    ]
    assert len(_notes(root)) == 3
    recent = _recent_activity(root)
    entries = _log_entries(root)
    for n in range(3):
        stem = f"Notes/Insights/entry-{n}"
        assert sum(stem in line for line in recent) == 1
        assert sum(line.endswith(f"note | {stem}") for line in entries) == 1
    # Newest first, as after three single commits; the Sources bullet stays below them.
    assert [stem for line in recent[:3] for stem in ("entry-2", "entry-1", "entry-0") if stem in line] == [
        "entry-2", "entry-1", "entry-0",
    ]
    assert "- Notes: 3" in (root / KB / "index.md").read_text(encoding="utf-8")


def test_a_target_taken_by_another_writer_refuses_that_note_and_commits_the_others(
    tmp_path: Path,
) -> None:
    root, notes, date_iso = _chunk(tmp_path)
    squatter = root / notes[1].planned["note"]["target_path"]
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("someone else's page\n", encoding="utf-8")

    outcomes = adopt.create_draft_notes(root, notes, collection=COLLECTION, date_iso=date_iso)

    assert outcomes[0].error is None and outcomes[2].error is None
    assert isinstance(outcomes[1].error, adopt.AdoptError)
    assert outcomes[1].error.code == "TARGET_OCCUPIED"
    assert squatter.read_text(encoding="utf-8") == "someone else's page\n"
    assert len(_notes(root)) == 3  # the two new notes and the squatter
    assert sum("Notes/Insights/entry-1" in line for line in _recent_activity(root)) == 0
    assert sum("Notes/Insights/entry-0" in line for line in _recent_activity(root)) == 1
    assert sum("Notes/Insights/entry-2" in line for line in _recent_activity(root)) == 1


def test_two_notes_that_plan_one_file_give_the_outcomes_of_creating_them_one_by_one(
    tmp_path: Path,
) -> None:
    root, notes, date_iso = _chunk(tmp_path, count=1)

    outcomes = adopt.create_draft_notes(
        root, [notes[0], notes[0]], collection=COLLECTION, date_iso=date_iso
    )

    assert outcomes[0].error is None
    assert isinstance(outcomes[1].error, adopt.AdoptError)
    assert outcomes[1].error.code == "TARGET_OCCUPIED"
    assert len(_notes(root)) == 1
    assert len(_log_entries(root)) == 2  # the Sources entry and the one note's entry
    assert sum("Notes/Insights/entry-0" in line for line in _recent_activity(root)) == 1


def test_a_failure_while_the_batch_installs_leaves_no_note_and_each_is_then_made_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, notes, date_iso = _chunk(tmp_path)
    last_page = root / notes[2].planned["note"]["target_path"]
    failures: list[Path] = []

    def disk_error_once(path: Path, *, restoring: bool) -> None:
        if path == last_page and not restoring and not failures:
            failures.append(path)
            raise OSError("injected disk error")

    monkeypatch.setattr(vault, "_before_batch_destination_published", disk_error_once)

    outcomes = adopt.create_draft_notes(root, notes, collection=COLLECTION, date_iso=date_iso)

    # A batch that left its first two notes behind would make their second try TARGET_OCCUPIED.
    assert failures == [last_page]
    assert [outcome.error for outcome in outcomes] == [None, None, None]
    assert len(_notes(root)) == 3
    assert sum("Notes/Insights/" in line for line in _recent_activity(root)) == 3
    assert sum(" note | Notes/Insights/" in line for line in _log_entries(root)) == 3


def _sealed(root: Path, *, primary: str, backref: str, backref_content: str):
    """One manifest binding a new page to the rewrite of an existing Source."""
    source = root / backref
    guard = vault.PathGuard.capture(
        root, backref, leaf_policy="content",
        expected_content_hash=vault.content_hash(source.read_text(encoding="utf-8")),
    )
    page = vault.PlannedWrite(
        root / primary, "page\n", create_only=True,
        guard=vault.PathGuard.capture(root, primary, leaf_policy="absent"),
    )
    claim = vault.PlannedWrite(source, backref_content, guard=guard)
    return page, claim, vocabulary_auxiliaries.seal(
        root, primary=page, derived=(("source-backref", claim),)
    )


def test_a_tuple_of_manifests_names_every_derived_write_once(tmp_path: Path) -> None:
    for name in ("one", "two"):
        (tmp_path / f"{name}.md").write_text(f"{name} source\n", encoding="utf-8")
    first_page, first_claim, first = _sealed(
        tmp_path, primary="a.md", backref="one.md", backref_content="one cited\n"
    )
    second_page, second_claim, second = _sealed(
        tmp_path, primary="b.md", backref="two.md", backref_content="two cited\n"
    )
    images = vocabulary_auxiliaries.planned_images(
        tmp_path, (first_page, first_claim, second_page, second_claim)
    )

    roles = vocabulary_auxiliaries.validate(tmp_path, images, (first, second))

    assert roles == {"one.md": "source-backref", "two.md": "source-backref"}


def test_two_manifests_claiming_one_path_are_refused(tmp_path: Path) -> None:
    (tmp_path / "shared.md").write_text("shared source\n", encoding="utf-8")
    first_page, claim, first = _sealed(
        tmp_path, primary="a.md", backref="shared.md", backref_content="cited\n"
    )
    second_page, _same_claim, second = _sealed(
        tmp_path, primary="b.md", backref="shared.md", backref_content="cited\n"
    )
    images = vocabulary_auxiliaries.planned_images(tmp_path, (first_page, claim, second_page))

    with pytest.raises(ValueError, match="VOCABULARY_AUXILIARY_CHANGED"):
        vocabulary_auxiliaries.validate(tmp_path, images, (first, second))


def test_a_manifest_in_a_tuple_refuses_a_write_whose_bytes_differ_from_its_claim(
    tmp_path: Path,
) -> None:
    for name in ("one", "two"):
        (tmp_path / f"{name}.md").write_text(f"{name} source\n", encoding="utf-8")
    first_page, first_claim, first = _sealed(
        tmp_path, primary="a.md", backref="one.md", backref_content="one cited\n"
    )
    second_page, second_claim, second = _sealed(
        tmp_path, primary="b.md", backref="two.md", backref_content="two cited\n"
    )
    swapped = vault.PlannedWrite(
        second_claim.path, "two cited and more\n", guard=second_claim.guard
    )
    images = vocabulary_auxiliaries.planned_images(
        tmp_path, (first_page, first_claim, second_page, swapped)
    )

    with pytest.raises(ValueError, match="VOCABULARY_AUXILIARY_CHANGED"):
        vocabulary_auxiliaries.validate(tmp_path, images, (first, second))
