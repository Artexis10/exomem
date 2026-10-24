"""The free-text artifact census is published, so a read request does not walk the vault.

Gating released text resolves artifact names against a census of the vault's
files. A request reads a published base plus an add-only overlay of the files
written since. These tests run the real gate against real files and count the
directory walks that the request makes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import create_file, move_file, preserve, reserved_paths
from exomem.file_watcher import FileWatcher
from exomem.governance import artifact_census, egress, lifecycle, membership, policy
from exomem.governance.principal import RequestPrincipal, request_scope

KB = "Knowledge Base"
WITHHELD_DIR = f"{KB}/Notes/Patterns"
RELEASED_DIR = f"{KB}/Notes/Insights"


@pytest.fixture(autouse=True)
def _fresh_census():
    artifact_census.clear()
    yield
    artifact_census.clear()


def _withhold_from_external(vault: Path, *globs: str, ceiling: int = 0) -> None:
    governance = vault / KB / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    paths = ", ".join(f'"{glob}"' for glob in globs)
    (governance / "scopes" / "withheld.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        f"name: Withheld\npaths: [{paths}]\n",
        encoding="utf-8",
    )
    (governance / "rules" / "withheld.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\n'
        f"ceiling: {ceiling}\n",
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()


def _gate(vault: Path, text: str) -> str:
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        return egress.redact_withheld_references(vault, text)


def _write(vault: Path, rel: str, data: bytes = b"data") -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _publish(vault: Path) -> None:
    """What the running service does: a request wants the census, then the owner builds it."""
    assert artifact_census.published_view(vault) is None
    watcher = FileWatcher(vault)
    watcher.arm_optional_recovery()
    watcher._publish_census_if_needed()
    assert artifact_census.published_view(vault) is not None


@pytest.fixture
def walks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every directory the census lists or walks from now on, as the calls that made them."""
    calls: list[str] = []
    for name in ("_walk_listings", "_list_directory"):
        real = getattr(artifact_census, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(artifact_census, name, spy)
    return calls


def test_a_service_written_artifact_is_withheld_at_once_without_the_owner(
    vault: Path, walks: list[str]
) -> None:
    """The highest-level check: the real preserve write path, no owner pass since."""
    _withhold_from_external(vault, "Notes/Patterns/**", "Evidence/**")
    _publish(vault)
    walks.clear()

    result = preserve.preserve_bytes(
        vault,
        scope="Rental",
        category="clips",
        filename="orchid-quarterly-terms.pdf",
        data=b"%PDF-1.4",
        text="terms",
    )

    bare = _gate(vault, "See orchid-quarterly-terms.pdf for the terms.")
    by_path = _gate(vault, f"See {result.path} for the terms.")
    linked = _gate(vault, f"See [[{result.path}]] for the terms.")
    for released in (bare, by_path, linked):
        assert egress.WITHHELD_REFERENCE in released
        assert "orchid-quarterly-terms" not in released
    assert walks == [], "the request walked the vault although a census was published"


def test_a_service_created_text_file_is_withheld_at_once(vault: Path, walks: list[str]) -> None:
    _withhold_from_external(vault, "Notes/Patterns/**")
    _publish(vault)
    walks.clear()

    create_file.create_file(
        vault, path=f"{WITHHELD_DIR}/orchid-service-note.txt", content="Internal note.\n"
    )

    released = _gate(vault, "See orchid-service-note.txt for the note.")
    assert egress.WITHHELD_REFERENCE in released
    assert walks == []


def test_a_file_created_outside_the_service_is_withheld_after_its_event_without_a_walk(
    vault: Path, walks: list[str]
) -> None:
    _withhold_from_external(vault, "Notes/Patterns/**")
    _publish(vault)
    walks.clear()

    created = _write(vault, f"{WITHHELD_DIR}/orchid-external-memo.txt")
    FileWatcher(vault)._record(created, deleted=False)

    released = _gate(vault, "See orchid-external-memo.txt for the memo.")
    assert egress.WITHHELD_REFERENCE in released
    assert "orchid-external-memo" not in released
    assert walks == []


def test_a_path_shaped_reference_to_an_unindexed_file_is_checked_on_disk(
    vault: Path, walks: list[str]
) -> None:
    _withhold_from_external(vault, "Notes/Patterns/**")
    _publish(vault)
    walks.clear()
    _write(vault, f"{WITHHELD_DIR}/orchid-direct-memo.txt")  # no event reached the watcher

    by_path = _gate(vault, f"See {WITHHELD_DIR}/orchid-direct-memo.txt for the memo.")
    by_folder_path = _gate(vault, "See Notes/Patterns/orchid-direct-memo.txt for the memo.")
    assert egress.WITHHELD_REFERENCE in by_path
    assert egress.WITHHELD_REFERENCE in by_folder_path
    assert "orchid-direct-memo" not in by_path + by_folder_path
    # The check found the file, so the next request resolves its bare name too.
    assert egress.WITHHELD_REFERENCE in _gate(vault, "See orchid-direct-memo.txt for the memo.")
    assert walks == []


def test_a_path_shaped_reference_that_names_no_file_or_leaves_the_vault_passes_unchanged(
    vault: Path,
) -> None:
    _withhold_from_external(vault, "Notes/Patterns/**")
    _publish(vault)
    outside = vault.parent / "outside-the-vault.txt"
    outside.write_text("secret", encoding="utf-8")

    for text in (
        f"See {WITHHELD_DIR}/never-written.txt for it.",
        "See ../outside-the-vault.txt for it.",
        f"See {vault.parent}/outside-the-vault.txt for it.",
    ):
        assert _gate(vault, text) == text


def test_a_removed_name_stays_in_the_census_until_a_rebuild_proves_the_removal(
    vault: Path, walks: list[str]
) -> None:
    """The released `orchid.md` is a prefix of the withheld `orchid.md.bak`.

    Dropping the longer name at the removal event would let the shorter name
    claim the text, and the removed file's name would read as released.
    """
    _withhold_from_external(vault, "Notes/Patterns/**")
    _write(vault, f"{RELEASED_DIR}/orchid.md", b"# Orchid\n")
    removed = _write(vault, f"{WITHHELD_DIR}/orchid.md.bak")
    text = "See orchid.md.bak for the old copy."
    _publish(vault)
    assert egress.WITHHELD_REFERENCE in _gate(vault, text)
    walks.clear()

    removed.unlink()
    FileWatcher(vault)._record(removed, deleted=True)

    held = _gate(vault, text)
    assert egress.WITHHELD_REFERENCE in held
    assert "orchid.md.bak" not in held
    assert walks == []

    watcher = FileWatcher(vault)
    watcher.arm_optional_recovery()
    watcher._publish_census_if_needed()
    assert _gate(vault, text) == text, "the rebuilt base still holds the removed name"


def test_an_unpublished_census_takes_the_walk_and_is_never_empty(
    vault: Path, walks: list[str]
) -> None:
    _withhold_from_external(vault, "Notes/Patterns/**")
    _write(vault, f"{WITHHELD_DIR}/orchid-unpublished-memo.txt")

    released = _gate(vault, "See orchid-unpublished-memo.txt for the memo.")

    assert egress.WITHHELD_REFERENCE in released
    assert "_walk_listings" in walks, "no census was published, yet the request did not walk"
    assert artifact_census.published_view(vault) is None


def test_a_burst_of_new_files_withdraws_the_census_to_the_walk(
    vault: Path, walks: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _withhold_from_external(vault, "Notes/Patterns/**")
    _publish(vault)
    monkeypatch.setattr(artifact_census, "_OVERLAY_LIMIT", 2)
    burst = [_write(vault, f"{WITHHELD_DIR}/orchid-burst-{n}.txt") for n in range(3)]
    watcher = FileWatcher(vault)
    for path in burst[:2]:
        watcher._record(path, deleted=False)
    assert artifact_census.published_view(vault) is not None
    watcher._record(burst[2], deleted=False)
    walks.clear()

    assert artifact_census.published_view(vault) is None
    released = _gate(vault, "See orchid-burst-2.txt for the memo.")

    assert egress.WITHHELD_REFERENCE in released
    assert "_walk_listings" in walks


def test_a_changed_release_decision_applies_on_the_next_request(
    vault: Path, walks: list[str]
) -> None:
    _withhold_from_external(vault, "Notes/Patterns/**")
    _write(vault, f"{WITHHELD_DIR}/orchid-decided-memo.txt")
    text = "See orchid-decided-memo.txt for the memo."
    _publish(vault)
    assert egress.WITHHELD_REFERENCE in _gate(vault, text)
    base = artifact_census.published_view(vault).base

    _withhold_from_external(vault, "Notes/Elsewhere/**")

    assert _gate(vault, text) == text
    assert artifact_census.published_view(vault).base is base, "the decision change rebuilt the census"


def test_one_tombstone_set_builds_its_aliases_once_and_a_new_deletion_adds_its_own(
    vault: Path,
) -> None:
    # A restricting rule governs the page, so deleting it writes a tombstone.
    _withhold_from_external(vault, "Notes/Patterns/**", ceiling=egress.RELEASE_FLOOR)
    page = f"{WITHHELD_DIR}/orchid-deleted-page.md"
    _write(vault, page, b"# Page\n")
    first = artifact_census.tombstone_aliases(lifecycle.tombstoned_paths(vault), KB)

    assert artifact_census.tombstone_aliases(lifecycle.tombstoned_paths(vault), KB) is first
    lifecycle.begin_deletion(vault, source_rel=page, trash_rel=f"{KB}/_trash/deleted.md")
    after = artifact_census.tombstone_aliases(lifecycle.tombstoned_paths(vault), KB)
    assert after is not first
    assert page in after[0]["orchid-deleted-page.md"]


def test_a_service_rename_into_a_withheld_folder_is_withheld_at_once(
    vault: Path, walks: list[str]
) -> None:
    _withhold_from_external(vault, "Notes/Patterns/**")
    source = f"{RELEASED_DIR}/orchid-draft-chart.pdf"
    _write(vault, source)
    _publish(vault)
    walks.clear()

    move_file.move_file(
        vault, old_path=source, new_path=f"{WITHHELD_DIR}/orchid-final-chart.pdf"
    )

    released = _gate(vault, "See orchid-final-chart.pdf for the chart.")
    assert egress.WITHHELD_REFERENCE in released
    assert walks == []


def test_a_generic_directory_move_adds_the_files_it_brings_to_the_census(
    vault: Path, walks: list[str]
) -> None:
    _withhold_from_external(vault, "Notes/Patterns/**")
    _write(vault, f"{RELEASED_DIR}/Batch/orchid-batch-chart.pdf")
    _publish(vault)
    walks.clear()

    reserved_paths.move_generic_path(
        vault, f"{RELEASED_DIR}/Batch", f"{WITHHELD_DIR}/Lot", source_kind="directory"
    )

    arrived = f"{WITHHELD_DIR}/Lot/orchid-batch-chart.pdf"
    assert arrived in artifact_census.published_view(vault).paths(arrived.casefold())
    assert walks == []
