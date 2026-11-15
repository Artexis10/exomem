"""A Source card's pointer to its bytes stays true, or the drift is visible and repairable.

Moving a raw artifact beside a card that carries a different name used to leave the card's
pointer (and any page that cited the path) naming a file that no longer exists, with no check
that noticed. Every fixture here is synthetic.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from exomem import audit as audit_module
from exomem import source_pointers

KB = "Knowledge Base"
SOURCES = f"{KB}/Sources"


def _write(vault: Path, rel: str, data: bytes) -> str:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return rel


def _card(vault: Path, rel: str, front: str = "", body: str = "Captured notes.\n") -> str:
    text = (
        "---\ntype: source\ntitle: Harbour tide export\nsource_type: dataset-export\n"
        f"captured: 2026-05-29\n{front}---\n\n{body}"
    )
    return _write(vault, rel, text.encode("utf-8"))


def _drift(vault: Path) -> list[audit_module.AuditFinding]:
    report = audit_module.audit(vault, categories=["source_pointer_drift"])
    return [f for f in report.findings if f.category == "source_pointer_drift"]


def test_a_pointer_to_a_moved_file_names_the_one_same_named_candidate(vault: Path) -> None:
    """A card left pointing at the pre-move path must say where its file went."""
    _write(vault, f"{SOURCES}/Other/Media/tides.json", b"[1, 2, 3]")
    stale = f"data_file: {SOURCES}/Other/tides.json\n"
    card = _card(vault, f"{SOURCES}/Other/tides-export.md", stale)

    (finding,) = _drift(vault)

    assert finding.path == card
    assert finding.meta == {
        "field": "data_file",
        "recorded_path": f"{SOURCES}/Other/tides.json",
        "problem": "missing",
        "candidate_path": f"{SOURCES}/Other/Media/tides.json",
    }


def test_two_same_named_files_propose_no_candidate(vault: Path) -> None:
    """An ambiguous name must not nominate a file the owner could repoint at by mistake."""
    _write(vault, f"{SOURCES}/Other/Media/tides.json", b"[1]")
    _write(vault, f"{SOURCES}/Reports/Marine/tides.json", b"[2]")
    _card(vault, f"{SOURCES}/Other/tides-export.md", f"data_file: {SOURCES}/Other/tides.json\n")

    (finding,) = _drift(vault)

    assert finding.meta["candidate_path"] is None


@pytest.mark.parametrize(
    "descriptor",
    [
        "governance_companion:\n  artifact_path: {path}\n",
        "raw_protection:\n  artifact_path: {path}\n",
    ],
)
def test_a_descriptor_pointer_is_checked_like_the_scalar_ones(vault: Path, descriptor: str) -> None:
    """A nested `artifact_path` hides from a check that reads only top-level fields."""
    path = f"{SOURCES}/Other/tides.json"
    _card(vault, f"{SOURCES}/Other/tides-export.md", descriptor.format(path=path))

    (finding,) = _drift(vault)

    assert finding.meta["field"].endswith(".artifact_path")
    assert finding.meta["recorded_path"] == path


def test_a_changed_file_is_hashed_only_when_its_stat_signature_changed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check must not re-read every original on every audit, yet must catch edited bytes."""
    rel = _write(vault, f"{SOURCES}/Other/tides.json", b"[1, 2, 3]")
    stat = (vault / rel).stat()
    good = hashlib.sha256(b"[1, 2, 3]").hexdigest()
    front = f"data_file: {rel}\nbinary_sha256: {good}\nbinary_size: {stat.st_size}\n"
    front += f"binary_mtime_ns: {stat.st_mtime_ns}\n"
    _card(vault, f"{SOURCES}/Other/tides-export.md", front)
    reads: list[Path] = []
    real = source_pointers._sha256
    monkeypatch.setattr(source_pointers, "_sha256", lambda path: reads.append(path) or real(path))

    assert _drift(vault) == []
    assert reads == []

    (vault / rel).write_bytes(b"[9, 9, 9]")  # same size, different bytes
    os.utime(vault / rel, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    (finding,) = _drift(vault)

    assert finding.meta["problem"] == "content"
    assert reads == [vault / rel]


def test_a_size_mismatch_is_drift_without_reading_the_file(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = _write(vault, f"{SOURCES}/Other/tides.json", b"[1, 2, 3, 4]")
    front = f"data_file: {rel}\nbinary_sha256: {'0' * 64}\nbinary_size: 3\n"
    _card(vault, f"{SOURCES}/Other/tides-export.md", front)
    monkeypatch.setattr(source_pointers, "_sha256", lambda path: pytest.fail("hashed"))

    (finding,) = _drift(vault)

    assert finding.meta["problem"] == "size"


def test_a_card_with_no_pointer_is_judged_by_the_paths_its_body_cites(vault: Path) -> None:
    """The cards that motivated this check carry only a prose path, and a prose path goes stale."""
    cited = f"{SOURCES}/Other/tides.json"
    body = f"## Raw artifact\n\n`{cited}`\n"
    _card(vault, f"{SOURCES}/Other/tides-export.md", body=body)
    _write(vault, f"{SOURCES}/Other/Media/tides.json", b"[1]")

    (finding,) = _drift(vault)
    assert finding.meta["field"] == "body"
    assert finding.meta["candidate_path"] == f"{SOURCES}/Other/Media/tides.json"

    _write(vault, cited, b"[1]")
    assert _drift(vault) == []


def test_a_folder_or_a_page_name_in_a_code_span_is_not_a_missing_file(vault: Path) -> None:
    """A card that names a folder or a page by name must not raise a drift finding."""
    body = (
        f"Filed under `{SOURCES}/Other/` and `{SOURCES}/Other` "
        f"and `{SOURCES}/Other/tides-export`.\n"
    )
    _card(vault, f"{SOURCES}/Other/tides-export.md", body=body)

    assert _drift(vault) == []


def test_a_healthy_pointer_ends_the_finding_even_when_the_body_still_cites_a_stale_path(
    vault: Path,
) -> None:
    """A Source's body never changes, so repairing the pointer has to be enough."""
    rel = _write(vault, f"{SOURCES}/Other/Media/tides.json", b"[1]")
    _card(
        vault,
        f"{SOURCES}/Other/tides-export.md",
        f"evidence_file: {rel}\n",
        body=f"`{SOURCES}/Other/tides.json`\n",
    )

    assert _drift(vault) == []


def test_moving_a_preserved_original_repoints_its_card_descriptor(vault: Path) -> None:
    """A move that repoints `evidence_file` but not the companion descriptor leaves a card
    whose descriptor names a path that no longer exists."""
    import io

    from exomem import move_file, preserve
    from exomem.vault import parse_frontmatter

    data = b'export-key: "a stable value"\n'
    stored = preserve.preserve_stream(
        vault, scope="Test", category="exports", filename="export.yaml", stream=io.BytesIO(data)
    )
    new_path = f"{KB}/Evidence/Test/archive/export.yaml"

    move_file.move_file(vault, old_path=stored.path, new_path=new_path)

    sidecar = (vault / f"{new_path}.md").read_text(encoding="utf-8")
    descriptor = parse_frontmatter(sidecar, strict=True)[0]["governance_companion"]
    assert descriptor["artifact_path"] == new_path
    assert _drift(vault) == []


def _frontmatter(vault: Path, rel: str) -> dict:
    from exomem.vault import parse_frontmatter

    return parse_frontmatter((vault / rel).read_text(encoding="utf-8"), strict=True)[0]


def test_a_stale_pointer_is_repaired_in_place_and_the_revert_restores_it(vault: Path) -> None:
    """Repair must not move the card or touch its body, and its undo must put the old value back."""
    from exomem import reclassify_source as rc

    old = f"{SOURCES}/Other/tides.json"
    new = _write(vault, f"{SOURCES}/Other/Media/tides.json", b"[1]")
    front = f"data_file: {old}\n"
    card = _card(vault, f"{SOURCES}/Other/tides-export.md", front, body="Prose body.\n")
    body_before = (vault / card).read_text(encoding="utf-8").split("---\n\n", 1)[1]

    repaired = rc.reclassify(vault, path=card, artifact_path=new, reason="the file moved")

    assert (repaired.new_path, repaired.relocated) == (card, False)
    assert repaired.repointed == ("data_file",)
    front = _frontmatter(vault, card)
    assert front["data_file"] == new
    assert front["source_type"] == "dataset-export"
    assert front["reclassified_from"][-1]["pointer"] == {"data_file": old}
    assert front["classification_history"]["events"][-1]["pointer"]["after"] == {"data_file": new}
    assert (vault / card).read_text(encoding="utf-8").split("---\n\n", 1)[1] == body_before
    assert _drift(vault) == []

    reverted = rc.revert(vault, path=card, reason="the repair named the wrong file")

    assert reverted.repointed == ("data_file",)
    assert _frontmatter(vault, card)["data_file"] == old
    assert "reclassified_from" not in _frontmatter(vault, card)
    assert [f.meta["field"] for f in _drift(vault)] == ["data_file"]


def test_a_card_with_only_a_prose_path_gains_a_pointer_and_loses_it_on_revert(vault: Path) -> None:
    """The cards that motivated this repair record no pointer at all."""
    from exomem import reclassify_source as rc

    cited = f"{SOURCES}/Other/tides.json"
    new = _write(vault, f"{SOURCES}/Other/Media/tides.json", b"[1]")
    card = _card(vault, f"{SOURCES}/Other/tides-export.md", body=f"## Raw artifact\n\n`{cited}`\n")
    assert [f.meta["field"] for f in _drift(vault)] == ["body"]

    rc.reclassify(vault, path=card, artifact_path=new, reason="the prose path is stale")

    assert _frontmatter(vault, card)["evidence_file"] == new
    assert _drift(vault) == []

    rc.revert(vault, path=card, reason="undo")

    assert "evidence_file" not in _frontmatter(vault, card)


def test_a_repair_refuses_a_target_that_does_not_exist_and_leaves_the_card_alone(
    vault: Path,
) -> None:
    from exomem import reclassify_source as rc

    stale = f"data_file: {SOURCES}/Other/tides.json\n"
    card = _card(vault, f"{SOURCES}/Other/tides-export.md", stale)
    before = (vault / card).read_bytes()

    with pytest.raises(rc.ReclassifyError) as refused:
        rc.reclassify(
            vault, path=card, artifact_path=f"{SOURCES}/Other/Media/gone.json", reason="repair"
        )

    assert refused.value.code == "POINTER_TARGET_MISSING"
    assert (vault / card).read_bytes() == before


def test_a_repair_refuses_a_file_that_is_not_the_bytes_the_card_recorded(vault: Path) -> None:
    """Repointing at an unrelated file would hide the loss of the card's own bytes."""
    from exomem import reclassify_source as rc

    other = _write(vault, f"{SOURCES}/Other/Media/other.json", b"[1, 2, 3, 4]")
    card = _card(
        vault,
        f"{SOURCES}/Other/tides-export.md",
        f"data_file: {SOURCES}/Other/tides.json\nbinary_sha256: {'0' * 64}\nbinary_size: 3\n",
    )

    with pytest.raises(rc.ReclassifyError) as refused:
        rc.reclassify(vault, path=card, artifact_path=other, reason="repair")

    assert refused.value.code == "POINTER_MISMATCH"


def test_a_repair_refuses_a_card_bound_to_its_file_by_a_descriptor(vault: Path) -> None:
    from exomem import reclassify_source as rc

    target = _write(vault, f"{SOURCES}/Other/Media/tides.json", b"[1]")
    card = _card(
        vault,
        f"{SOURCES}/Other/tides-export.md",
        f"governance_companion:\n  artifact_path: {SOURCES}/Other/tides.json\n",
    )

    with pytest.raises(rc.ReclassifyError) as refused:
        rc.reclassify(vault, path=card, artifact_path=target, reason="repair")

    assert refused.value.code == "POINTER_BOUND"


def _note(vault: Path, rel: str, body: str, front: str = "") -> str:
    text = f"---\ntype: note\ntitle: Harbour note\n{front}---\n\n{body}"
    return _write(vault, rel, text.encode("utf-8"))


def _move(vault: Path, old: str, new: str) -> dict:
    from exomem import commands

    return commands.op_manage_memory_file(vault, operation="move", old_path=old, new_path=new)


def test_moving_raw_bytes_follows_their_path_into_notes_and_audit_reaches_zero_drift(
    vault: Path,
) -> None:
    """The workflow that left four live cards naming a file that had moved.

    An unpaired Source card cites its JSON by vault path and records no pointer. A move of the
    JSON must rename the path in every page that may change, report the captured card it cannot
    change, and leave the audit with one finding that a pointer repair then clears.
    """
    from exomem import reclassify_source as rc

    old = f"{SOURCES}/Other/tides.json"
    new = f"{SOURCES}/Other/Media/tides.json"
    _write(vault, old, b"[1, 2, 3]")
    card = _card(vault, f"{SOURCES}/Other/tides-export.md", body=f"## Raw artifact\n\n`{old}`\n")
    note = _note(vault, f"{KB}/Notes/harbour.md", f"The tide export is stored at `{old}`.\n")
    assert _drift(vault) == []

    moved = _move(vault, old, new)

    assert moved["path_mentions_updated"] == 1
    assert note in moved["files_touched"]
    assert f"`{new}`" in (vault / note).read_text(encoding="utf-8")
    assert f"`{old}`" in (vault / card).read_text(encoding="utf-8")  # a Source body never changes
    assert any("append-only" in warning for warning in moved["warnings"])
    (finding,) = _drift(vault)
    assert (finding.path, finding.meta["candidate_path"]) == (card, new)

    rc.reclassify(vault, path=card, artifact_path=new, reason="the file moved into Media")

    assert _drift(vault) == []


def test_a_move_renames_only_the_exact_path_in_body_prose(vault: Path) -> None:
    """A longer path or file name that merely starts with the moved path must keep its bytes."""
    old = f"{SOURCES}/Other/tides.json"
    new = f"{SOURCES}/Other/Media/tides.json"
    _write(vault, old, b"[1]")
    note = _note(
        vault,
        f"{KB}/Notes/harbour.md",
        f"Exact: {old}.\nBackup: {old}.bak and {old}2 and {SOURCES}/Other/old-tides.json.\n",
        front=f"export: {old}\n",
    )

    moved = _move(vault, old, new)

    text = (vault / note).read_text(encoding="utf-8")
    assert moved["path_mentions_updated"] == 1
    assert f"Exact: {new}." in text
    assert f"Backup: {old}.bak and {old}2 and {SOURCES}/Other/old-tides.json." in text
    assert f"export: {old}\n" in text  # frontmatter pointers are not body prose


def test_a_move_says_so_when_the_catalogue_cannot_name_the_pages_citing_the_path(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unanswered lookup must not read as "no page cites the path"."""
    from exomem import path_mentions

    monkeypatch.setattr(
        path_mentions, "find_pages", lambda *_a, **_k: path_mentions.Lookup((), False)
    )
    old = f"{SOURCES}/Other/tides.json"
    _write(vault, old, b"[1]")

    moved = _move(vault, old, f"{SOURCES}/Other/Media/tides.json")

    assert moved["path_mentions_updated"] == 0
    assert any("path mentions were not checked" in warning for warning in moved["warnings"])
