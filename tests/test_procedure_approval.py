"""The approvals ledger, its key and its one verdict (add-approved-procedures 2.1 and 2.2).

Each test runs the real module against a real vault copy in a temporary directory. A page is
eligible through the real semantic contract. All data is invented.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exomem import held_fs, procedure_approval, reserved_paths, state_migration
from exomem.append_to_file import AppendError, append_to_file
from exomem.create_file import CreateFileError, create_file
from exomem.delete_file import DeleteFileError, delete_file
from exomem.edit import EditError, edit
from exomem.kbdir import kb_dirname
from exomem.move_file import MoveFileError, move_file
from exomem.recover_from_trash import RecoverError, recover_from_trash

KB = "Knowledge Base"
INSIGHTS = f"{KB}/Notes/Insights"
REF_ID = "6b1f2c3e-0d4a-4e5f-8a9b-1c2d3e4f5a6b"
REF = f"exomem://memory/{REF_ID}"
OTHER_ID = "0c7d1a52-93be-4f1e-b2a6-7d5e8f9a0b1c"
OTHER_REF = f"exomem://memory/{OTHER_ID}"
METHOD = "- [technique] Tag the release from a clean checkout ^tag\n"


def write_page(
    vault: Path,
    name: str,
    exomem_id: str | None = REF_ID,
    *,
    body: str = METHOD,
    status: str = "active",
    note_type: str = "insight",
    folder: str = INSIGHTS,
) -> str:
    frontmatter = f"type: {note_type}\nstatus: {status}\n"
    if exomem_id:
        frontmatter += f"exomem_id: {exomem_id}\n"
    path = vault / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n\n# {name}\n\n{body}", encoding="utf-8")
    return f"{folder}/{name}.md"


def facts_of(vault: Path, rel: str) -> procedure_approval.PageFacts:
    return procedure_approval.page_facts(vault, rel, (vault / rel).read_bytes())


@contextmanager
def owner_authority():
    with reserved_paths._owner_authority_scope("approve_procedure"):  # noqa: SLF001 - dispatcher scope
        yield


def approve_page(vault: Path, rel: str, ref: str = REF, *, now: datetime | None = None):
    facts = facts_of(vault, rel)
    with owner_authority():
        return procedure_approval.approve(
            vault,
            ref,
            facts,
            expected_version=facts.version,
            principal="owner",
            client="test client",
            now=now,
        )


def verdict_of(
    vault: Path, rel: str, ref: str = REF, *, owner: bool = True, full_release: bool = True
) -> procedure_approval.Verdict:
    return procedure_approval.verdict(
        facts_of(vault, rel),
        procedure_approval.ledger(vault, ref),
        owner=owner,
        full_release=full_release,
    )


def ref_directory(vault: Path, ref: str = REF) -> Path:
    return vault / KB / "_Approvals" / ref.rsplit("/", 1)[1]


def entry_names(vault: Path, ref: str = REF) -> list[str]:
    directory = ref_directory(vault, ref)
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def read_entry(vault: Path, name: str, ref: str = REF) -> dict:
    return json.loads((ref_directory(vault, ref) / name).read_text(encoding="utf-8"))


def write_raw(directory: Path, name: str, data: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


def write_entry(directory: Path, name: str, document: dict) -> Path:
    return write_raw(directory, name, json.dumps(document).encode("utf-8"))


# --- eligibility ---------------------------------------------------------------------------


def test_a_live_compiled_page_with_a_method_unit_and_one_stable_id_is_eligible(vault: Path) -> None:
    rel = write_page(vault, "release-steps")

    facts = facts_of(vault, rel)

    assert facts.eligible and facts.condition is None
    assert facts.ref == REF
    assert facts.version == hashlib.sha256((vault / rel).read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("expected", "page"),
    [
        (
            procedure_approval.NOT_COMPILED,
            {"note_type": "source", "folder": f"{KB}/Sources/Articles"},
        ),
        (
            procedure_approval.NOT_COMPILED,
            {"note_type": "entity", "folder": f"{KB}/Entities/Concepts"},
        ),
        (procedure_approval.NOT_LIVE, {"status": "superseded"}),
        (procedure_approval.NOT_LIVE, {"status": "archived"}),
        (procedure_approval.NO_METHOD_UNIT, {"body": "Tag the release from a clean checkout.\n"}),
        (procedure_approval.NO_STABLE_ID, {"exomem_id": None}),
    ],
)
def test_a_page_that_fails_one_eligibility_test_names_that_test(
    vault: Path, expected: str, page: dict
) -> None:
    rel = write_page(vault, "candidate", **page)

    facts = facts_of(vault, rel)

    assert not facts.eligible
    assert facts.condition == expected


def test_the_id_must_resolve_to_this_one_page(vault: Path) -> None:
    first = write_page(vault, "first-copy")
    assert facts_of(vault, first).eligible
    second = write_page(vault, "second-copy")

    assert facts_of(vault, first).condition == procedure_approval.DUPLICATE_ID
    assert facts_of(vault, second).condition == procedure_approval.DUPLICATE_ID

    data = (vault / first).read_bytes()
    elsewhere = procedure_approval.page_facts(vault, first, data, holders=(second,))
    assert elsewhere.condition == procedure_approval.ID_UNRESOLVED


# --- the verdict -----------------------------------------------------------------------------


def test_an_approved_page_reads_approved_and_a_restricted_reader_sees_no_entry(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    approve_page(vault, rel)

    owner_view = verdict_of(vault, rel)
    reader_view = verdict_of(vault, rel, owner=False)

    assert owner_view.value == procedure_approval.APPROVED
    assert owner_view.entry is not None and owner_view.entry.principal == "owner"
    assert reader_view.value == procedure_approval.APPROVED
    assert reader_view.entry is None and reader_view.findings == ()


def test_an_edit_makes_the_approval_stale_for_the_owner_and_unapproved_for_everyone_else(
    vault: Path,
) -> None:
    rel = write_page(vault, "release-steps")
    approve_page(vault, rel)
    with (vault / rel).open("a", encoding="utf-8") as page:
        page.write("\nAlso run the smoke test.\n")

    assert verdict_of(vault, rel).value == procedure_approval.STALE
    assert verdict_of(vault, rel, owner=False).value == procedure_approval.UNAPPROVED


def test_a_page_that_stops_being_live_reads_unapproved_and_never_stale(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    approve_page(vault, rel)
    write_page(vault, "release-steps", status="superseded")

    assert verdict_of(vault, rel).value == procedure_approval.UNAPPROVED


def test_a_reader_below_full_release_sees_unapproved_and_no_entry(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    approve_page(vault, rel)

    view = verdict_of(vault, rel, full_release=False)

    assert view.value == procedure_approval.UNAPPROVED
    assert view.entry is None


def test_a_move_with_unchanged_bytes_keeps_the_approval(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    approve_page(vault, rel)
    moved = f"{KB}/Notes/Patterns/release-steps.md"
    (vault / moved).parent.mkdir(parents=True, exist_ok=True)
    (vault / rel).rename(vault / moved)

    assert verdict_of(vault, moved).value == procedure_approval.APPROVED


# --- writing entries -----------------------------------------------------------------------------


def test_an_approval_is_one_entry_that_binds_the_ref_and_the_exact_version(vault: Path) -> None:
    rel = write_page(vault, "release-steps")

    outcome = approve_page(vault, rel)

    assert outcome.appended is not None and outcome.existing is None
    assert entry_names(vault) == [outcome.appended.entry_id]
    recorded = read_entry(vault, outcome.appended.entry_id)
    assert recorded["ref"] == REF
    assert recorded["version"] == facts_of(vault, rel).version
    assert recorded["action"] == "approve"
    assert recorded["client"] == "test client" and recorded["principal"] == "owner"
    assert stat.S_IMODE((ref_directory(vault) / outcome.appended.entry_id).stat().st_mode) == 0o600


def test_the_key_is_generated_on_the_first_write_with_mode_0600_outside_the_vault(
    vault: Path,
) -> None:
    rel = write_page(vault, "release-steps")
    key_file = procedure_approval.key_path(vault)
    assert not key_file.exists()

    first = approve_page(vault, rel)
    key = key_file.read_bytes()
    write_page(vault, "release-steps", body=METHOD + "\nAlso run the smoke test.\n")
    second = approve_page(vault, rel)

    assert len(key) == 32
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert vault not in key_file.parents
    assert key_file.read_bytes() == key, "a later write must reuse the key, not replace it"
    for reported in (first, second, procedure_approval.ledger(vault, REF)):
        assert key.hex() not in repr(reported)
        assert str(key_file) not in repr(reported)


def test_a_write_without_the_dispatcher_authority_changes_nothing(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    facts = facts_of(vault, rel)

    with pytest.raises(RuntimeError):
        procedure_approval.approve(
            vault, REF, facts, expected_version=facts.version, principal="x", client="x"
        )

    assert not (vault / KB / "_Approvals").exists()
    assert not procedure_approval.key_path(vault).exists(), "no key from a refused caller"


def test_a_repeated_approve_appends_nothing_and_reports_the_existing_entry(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    first = approve_page(vault, rel)

    again = approve_page(vault, rel)

    assert again.appended is None
    assert again.existing == first.appended
    assert entry_names(vault) == [first.appended.entry_id]
    assert verdict_of(vault, rel).value == procedure_approval.APPROVED


def test_an_approve_that_follows_an_edit_reports_the_version_it_replaces(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    first = approve_page(vault, rel)
    assert first.previous_approved_version is None
    write_page(vault, "release-steps", body=METHOD + "\nAlso run the smoke test.\n")

    second = approve_page(vault, rel)

    assert second.previous_approved_version == first.appended.version
    assert second.appended.version != first.appended.version


def test_a_refused_approve_moves_nothing_and_creates_no_key(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    damaged = write_raw(ref_directory(vault), "1" * 32, b"not json {")
    facts = facts_of(vault, rel)

    with owner_authority():
        with pytest.raises(procedure_approval.ApprovalRefused) as stale:
            procedure_approval.approve(
                vault, REF, facts, expected_version="0" * 64, principal="owner", client="c"
            )
        write_page(vault, "release-steps", body="Prose only.\n")
        prose = facts_of(vault, rel)
        with pytest.raises(procedure_approval.ApprovalRefused) as unfit:
            procedure_approval.approve(
                vault, REF, prose, expected_version=prose.version, principal="owner", client="c"
            )

    assert stale.value.code == "stale_version"
    assert unfit.value.code == "ineligible"
    assert unfit.value.condition == procedure_approval.NO_METHOD_UNIT
    assert damaged.exists(), "a refused call must not run the quarantine step"
    assert not (vault / KB / "_Approvals" / "_quarantine").exists()
    assert not procedure_approval.key_path(vault).exists()


def test_the_writer_time_is_later_than_every_verified_entry_even_if_the_clock_runs_back(
    vault: Path,
) -> None:
    rel = write_page(vault, "release-steps")
    start = datetime(2030, 1, 1, tzinfo=UTC)
    approve_page(vault, rel, now=start)

    with owner_authority():
        revoked = procedure_approval.revoke(
            vault,
            REF,
            current_version=facts_of(vault, rel).version,
            expected_version=None,
            principal="owner",
            client="c",
            now=start - timedelta(hours=1),
        )

    assert (
        revoked.appended.recorded_at > procedure_approval.ledger(vault, REF).verified[0].recorded_at
    )
    assert verdict_of(vault, rel).value == procedure_approval.UNAPPROVED


# --- revoking --------------------------------------------------------------------------------


def test_a_revoke_works_for_a_deleted_page_and_it_stays_unapproved_if_the_bytes_return(
    vault: Path,
) -> None:
    rel = write_page(vault, "release-steps")
    approved = approve_page(vault, rel).appended
    saved = (vault / rel).read_bytes()
    (vault / rel).unlink()

    with owner_authority():
        revoked = procedure_approval.revoke(
            vault, REF, current_version=None, expected_version=None, principal="owner", client="c"
        )
    (vault / rel).write_bytes(saved)

    assert revoked.appended is not None
    assert revoked.appended.action == "revoke" and revoked.appended.version == approved.version
    assert verdict_of(vault, rel).value == procedure_approval.UNAPPROVED
    assert [entry.action for entry in procedure_approval.ledger(vault, REF).verified] == [
        "approve",
        "revoke",
    ]


def test_a_served_read_of_a_revoked_page_gives_the_owner_the_revoke_entry(vault: Path) -> None:
    # The serving shortcut skips the page read on a revoke; it must still report
    # the entry that `verdict` reports, so both answer the owner alike.
    rel = write_page(vault, "release-steps")
    approve_page(vault, rel)
    with owner_authority():
        revoked = procedure_approval.revoke(
            vault, REF, current_version=None, expected_version=None, principal="o", client="c"
        ).appended

    def served(*, owner: bool) -> procedure_approval.Verdict:
        return procedure_approval.served_verdict(
            vault, rel, procedure_approval.ledger(vault, REF), owner=owner, full_release=True,
            read=lambda: (vault / rel).read_bytes(), holders=lambda: (rel,),
        )

    assert served(owner=True) == verdict_of(vault, rel)
    assert served(owner=True).entry == revoked
    assert served(owner=False) == procedure_approval.Verdict(procedure_approval.UNAPPROVED)


def test_a_revoke_without_an_approval_or_a_repeat_of_one_appends_nothing(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    current = facts_of(vault, rel).version

    with owner_authority():
        none_yet = procedure_approval.revoke(
            vault, REF, current_version=current, expected_version=None, principal="o", client="c"
        )
        assert none_yet.appended is None and "No approval exists" in none_yet.outcome
        approve_page(vault, rel)
        first = procedure_approval.revoke(
            vault, REF, current_version=current, expected_version=None, principal="o", client="c"
        )
        repeat = procedure_approval.revoke(
            vault, REF, current_version=current, expected_version=None, principal="o", client="c"
        )

    assert first.appended is not None
    assert repeat.appended is None and repeat.existing == first.appended
    assert len(entry_names(vault)) == 2


def test_a_revoke_with_a_stale_expected_version_refuses_before_any_move(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    approve_page(vault, rel)
    damaged = write_raw(ref_directory(vault), "1" * 32, b"not json {")

    with owner_authority(), pytest.raises(procedure_approval.ApprovalRefused) as refused:
        procedure_approval.revoke(
            vault,
            REF,
            current_version=facts_of(vault, rel).version,
            expected_version="0" * 64,
            principal="o",
            client="c",
        )

    assert refused.value.code == "stale_version"
    assert damaged.exists()
    assert len(procedure_approval.ledger(vault, REF).verified) == 1


# --- foreign and tampered entries -------------------------------------------------------------------


def test_a_vault_copied_to_a_host_with_another_key_reads_unapproved_with_one_finding_per_ref(
    vault: Path,
) -> None:
    first = write_page(vault, "release-steps")
    second = write_page(vault, "rollback-steps", OTHER_ID)
    approve_page(vault, first)
    approve_page(vault, second, OTHER_REF)
    procedure_approval.key_path(vault).write_bytes(os.urandom(32))

    for rel, ref in ((first, REF), (second, OTHER_REF)):
        owner_view = verdict_of(vault, rel, ref)
        reader_view = verdict_of(vault, rel, ref, owner=False)
        assert owner_view.value == procedure_approval.UNAPPROVED
        assert [(f.ref, f.kind) for f in owner_view.findings] == [(ref, procedure_approval.FOREIGN)]
        assert reader_view.value == procedure_approval.UNAPPROVED and reader_view.findings == ()

    approve_page(vault, first)
    assert verdict_of(vault, first).value == procedure_approval.APPROVED


def test_a_vault_copied_to_a_host_without_a_key_reads_unapproved_and_a_re_approval_works(
    vault: Path,
) -> None:
    rel = write_page(vault, "release-steps")
    approve_page(vault, rel)
    procedure_approval.key_path(vault).unlink()

    view = verdict_of(vault, rel)
    assert view.value == procedure_approval.UNAPPROVED
    assert [f.kind for f in view.findings] == [procedure_approval.FOREIGN]

    approve_page(vault, rel)
    assert procedure_approval.key_path(vault).exists()
    assert verdict_of(vault, rel).value == procedure_approval.APPROVED


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("version", "f" * 64),
        ("action", "revoke"),
        ("principal", "someone else"),
        ("client", "another client"),
        ("recorded_at", "2031-01-01T00:00:00.000000Z"),
    ],
)
def test_a_change_to_any_field_fails_the_mac(vault: Path, field: str, changed: str) -> None:
    rel = write_page(vault, "release-steps")
    name = approve_page(vault, rel).appended.entry_id
    document = read_entry(vault, name)
    document[field] = changed
    (ref_directory(vault) / name).write_text(json.dumps(document), encoding="utf-8")

    current = procedure_approval.ledger(vault, REF)

    assert current.verified == () and current.foreign == (name,) and current.unreadable == ()
    assert verdict_of(vault, rel).value == procedure_approval.UNAPPROVED


def test_a_hand_written_entry_does_not_approve_and_stays_where_it_is(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    forged = write_entry(
        ref_directory(vault),
        "4" * 32,
        {
            "schema_version": 1,
            "entry_id": "4" * 32,
            "ref": REF,
            "version": facts_of(vault, rel).version,
            "action": "approve",
            "principal": "intruder",
            "client": "x",
            "recorded_at": "2030-01-01T00:00:00.000000Z",
            "mac": "0" * 64,
        },
    )

    view = verdict_of(vault, rel)

    assert view.value == procedure_approval.UNAPPROVED
    assert [(f.ref, f.kind, f.entries) for f in view.findings] == [
        (REF, procedure_approval.FOREIGN, ("4" * 32,))
    ]
    assert forged.exists()


def test_only_quarantine_with_a_ref_moves_a_foreign_entry_and_only_that_refs(vault: Path) -> None:
    first = write_page(vault, "release-steps")
    second = write_page(vault, "rollback-steps", OTHER_ID)
    approve_page(vault, first)
    approve_page(vault, second, OTHER_REF)
    for ref in (REF, OTHER_REF):
        forged = read_entry(vault, entry_names(vault, ref)[0], ref)
        forged["entry_id"] = "5" * 32
        forged["client"] = "forged"
        write_entry(ref_directory(vault, ref), "5" * 32, forged)

    approve_page(vault, first)
    with owner_authority():
        procedure_approval.revoke(
            vault, REF, current_version=None, expected_version=None, principal="o", client="c"
        )
        assert procedure_approval.quarantine(vault) == ()
        moved = procedure_approval.quarantine(vault, REF)

    assert [(m.ref, m.name, m.kind) for m in moved] == [(REF, "5" * 32, procedure_approval.FOREIGN)]
    assert "5" * 32 not in entry_names(vault, REF)
    assert "5" * 32 in entry_names(vault, OTHER_REF), "another ref's foreign entry stays"
    quarantined = vault / KB / "_Approvals" / "_quarantine" / REF_ID / ("5" * 32)
    assert quarantined.exists()
    assert set(procedure_approval.listed_refs(vault)) == {REF, OTHER_REF}


# --- unreadable entries ----------------------------------------------------------------------------


def plant_unreadable(vault: Path, kind: str) -> str:
    """Plant one damaged entry file, using the first ref's entry as the model, and return its ref."""
    valid = read_entry(vault, entry_names(vault)[0])
    if kind == "garbage":
        write_raw(ref_directory(vault), "1" * 32, b"not json {")
        return REF
    if kind == "newer_schema":
        newer = {**valid, "entry_id": "2" * 32, "schema_version": 2}
        write_entry(ref_directory(vault), "2" * 32, newer)
        return REF
    if kind == "named_for_another_entry":
        write_entry(ref_directory(vault), "3" * 32, valid)
        return REF
    assert kind == "filed_under_another_ref"
    write_entry(ref_directory(vault, OTHER_REF), valid["entry_id"], valid)
    return OTHER_REF


@pytest.mark.parametrize(
    "kind", ["garbage", "newer_schema", "named_for_another_entry", "filed_under_another_ref"]
)
def test_an_unreadable_entry_fails_only_its_own_ref(vault: Path, kind: str) -> None:
    pages = {
        REF: write_page(vault, "release-steps"),
        OTHER_REF: write_page(vault, "rollback-steps", OTHER_ID),
    }
    for ref, rel in pages.items():
        approve_page(vault, rel, ref)
    failing = plant_unreadable(vault, kind)
    intact = next(ref for ref in pages if ref != failing)

    broken = verdict_of(vault, pages[failing], failing)
    fine = verdict_of(vault, pages[intact], intact)

    assert broken.value == procedure_approval.UNAPPROVED
    assert [(f.ref, f.kind) for f in broken.findings] == [(failing, procedure_approval.UNREADABLE)]
    assert verdict_of(vault, pages[failing], failing, owner=False).findings == ()
    assert fine.value == procedure_approval.APPROVED and fine.findings == ()


def test_a_repeat_after_the_quarantine_step_appends_nothing_and_reads_approved(
    vault: Path,
) -> None:
    first = write_page(vault, "release-steps")
    second = write_page(vault, "rollback-steps", OTHER_ID)
    approve_page(vault, first)
    approve_page(vault, second, OTHER_REF)
    damaged = write_raw(ref_directory(vault), "1" * 32, b"not json {")
    other_damage = write_raw(ref_directory(vault, OTHER_REF), "2" * 32, b"not json {")
    assert verdict_of(vault, first).value == procedure_approval.UNAPPROVED

    again = approve_page(vault, first)

    assert again.appended is None and again.existing is not None
    assert [(m.ref, m.name, m.kind) for m in again.quarantined] == [
        (REF, "1" * 32, procedure_approval.UNREADABLE)
    ]
    assert not damaged.exists()
    assert (vault / KB / "_Approvals" / "_quarantine" / REF_ID / ("1" * 32)).exists()
    assert other_damage.exists(), "the step covers only the ref of the call"
    assert verdict_of(vault, first).value == procedure_approval.APPROVED
    assert verdict_of(vault, second, OTHER_REF).value == procedure_approval.UNAPPROVED


def test_an_approve_of_a_new_version_clears_an_unreadable_entry_and_appends(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    approve_page(vault, rel)
    write_raw(ref_directory(vault), "1" * 32, b"not json {")
    write_page(vault, "release-steps", body=METHOD + "\nAlso run the smoke test.\n")

    outcome = approve_page(vault, rel)

    assert outcome.appended is not None and len(outcome.quarantined) == 1
    assert verdict_of(vault, rel).value == procedure_approval.APPROVED


def test_quarantine_without_a_ref_moves_every_unreadable_file_and_nothing_else(
    vault: Path,
) -> None:
    first = write_page(vault, "release-steps")
    second = write_page(vault, "rollback-steps", OTHER_ID)
    approve_page(vault, first)
    approve_page(vault, second, OTHER_REF)
    for ref in (REF, OTHER_REF):
        write_raw(ref_directory(vault, ref), "1" * 32, b"not json {")
    procedure_approval.key_path(vault).write_bytes(os.urandom(32))
    before = {ref: entry_names(vault, ref) for ref in (REF, OTHER_REF)}

    with owner_authority():
        moved = procedure_approval.quarantine(vault)

    assert sorted((m.ref, m.name, m.kind) for m in moved) == sorted(
        (ref, "1" * 32, procedure_approval.UNREADABLE) for ref in (REF, OTHER_REF)
    )
    for ref in (REF, OTHER_REF):
        assert entry_names(vault, ref) == [n for n in before[ref] if n != "1" * 32]
    assert set(procedure_approval.listed_refs(vault)) == {REF, OTHER_REF}, "quarantine is no ref"


def test_a_quarantined_file_that_returns_is_quarantined_again_under_a_new_name(
    vault: Path,
) -> None:
    rel = write_page(vault, "release-steps")
    approve_page(vault, rel)
    quarantine_directory = vault / KB / "_Approvals" / "_quarantine" / REF_ID
    for _ in range(2):
        write_raw(ref_directory(vault), "1" * 32, b"not json {")
        with owner_authority():
            moved = procedure_approval.quarantine(vault, REF)
        assert [m.name for m in moved] == ["1" * 32]

    assert len(list(quarantine_directory.iterdir())) == 2


# --- crash, missing tree and unreadable stores --------------------------------------------------------


def test_a_leftover_publish_temporary_is_never_an_entry(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    first = approve_page(vault, rel).appended
    # What a crash between the temporary write and the rename leaves behind.
    leftover = ref_directory(vault) / held_fs.publish_temporary_name()
    leftover.write_bytes((ref_directory(vault) / first.entry_id).read_bytes())

    current = procedure_approval.ledger(vault, REF)
    again = approve_page(vault, rel)

    assert current.verified == (first,) and current.unreadable == () and current.foreign == ()
    assert verdict_of(vault, rel).value == procedure_approval.APPROVED
    assert again.appended is None and again.quarantined == ()
    assert leftover.exists()


def test_a_missing_tree_on_a_host_with_a_key_means_no_approvals_and_no_finding(
    vault: Path,
) -> None:
    rel = write_page(vault, "release-steps")
    approve_page(vault, rel)
    shutil.rmtree(vault / KB / "_Approvals")
    assert procedure_approval.key_path(vault).exists()

    view = verdict_of(vault, rel)

    assert view.value == procedure_approval.UNAPPROVED and view.findings == ()
    assert procedure_approval.listed_refs(vault) == ()


def test_a_ref_directory_that_cannot_be_read_fails_only_its_own_ref(vault: Path) -> None:
    first = write_page(vault, "release-steps")
    second = write_page(vault, "rollback-steps", OTHER_ID)
    approve_page(vault, first)
    approve_page(vault, second, OTHER_REF)
    shutil.rmtree(ref_directory(vault))
    ref_directory(vault).write_bytes(b"a file where a directory belongs")

    broken = verdict_of(vault, first)

    assert broken.value == procedure_approval.UNAPPROVED
    assert [f.kind for f in broken.findings] == [procedure_approval.UNAVAILABLE]
    assert verdict_of(vault, second, OTHER_REF).value == procedure_approval.APPROVED


def test_a_damaged_key_makes_refs_with_entries_unavailable_and_is_never_overwritten(
    vault: Path,
) -> None:
    first = write_page(vault, "release-steps")
    second = write_page(vault, "rollback-steps", OTHER_ID)
    approve_page(vault, first)
    key_file = procedure_approval.key_path(vault)
    key_file.write_bytes(b"short")

    view = verdict_of(vault, first)
    with pytest.raises(procedure_approval.ApprovalRefused) as with_entries:
        approve_page(vault, first)
    with pytest.raises(procedure_approval.ApprovalRefused) as without_entries:
        approve_page(vault, second, OTHER_REF)

    assert view.value == procedure_approval.UNAPPROVED
    assert [f.kind for f in view.findings] == [procedure_approval.UNAVAILABLE]
    assert with_entries.value.code == "ledger_unavailable"
    assert without_entries.value.code == "key_unavailable"
    assert key_file.read_bytes() == b"short", "a damaged key is never replaced by a new one"


# --- the reserved tree and the key under the placement rules --------------------------------------------


def test_generic_routes_refuse_the_approvals_tree(vault: Path) -> None:
    rel = write_page(vault, "release-steps")
    approve_page(vault, rel)
    name = entry_names(vault)[0]
    entry = f"{KB}/_Approvals/{REF_ID}/{name}"
    forged = f"{KB}/_Approvals/{REF_ID}/{'7' * 32}"
    before = (vault / entry).read_bytes()

    with pytest.raises(CreateFileError) as created:
        create_file(vault, path=forged, content="{}")
    with pytest.raises(AppendError) as appended:
        append_to_file(vault, path=entry, content="more")
    with pytest.raises(EditError) as edited:
        edit(vault, path=entry, why="forge", old_string="approve", new_string="revoke")
    with pytest.raises(DeleteFileError) as deleted:
        delete_file(vault, path=entry, confirm=True)
    with pytest.raises(MoveFileError) as moved_in:
        move_file(vault, old_path=rel, new_path=forged)
    with pytest.raises(MoveFileError) as moved_out:
        move_file(vault, old_path=entry, new_path=f"{INSIGHTS}/copy.md")
    trash = vault / KB / "_trash" / "2026-08-21"
    trash.mkdir(parents=True)
    (trash / "page.md").write_text("A page.\n", encoding="utf-8")
    with pytest.raises(RecoverError) as recovered:
        recover_from_trash(vault, trash_path=f"{KB}/_trash/2026-08-21/page.md", restore_path=forged)
    with pytest.raises(reserved_paths.ReservedPathLeafError) as adopted:
        # The read behind the adoption and import routes.
        with reserved_paths.open_generic_binary(vault, entry, physical=True):
            pass

    # A route that names an existing entry hears "not found", so it never confirms that the file
    # exists. A route that names a new path inside the tree hears "reserved".
    for error in (created, appended, edited, deleted, moved_in, moved_out, recovered, adopted):
        assert error.value.code in {"NOT_FOUND", "RESERVED_PATH"}
    assert (vault / entry).read_bytes() == before
    assert not (vault / forged).exists()
    assert (trash / "page.md").exists()


def test_the_key_is_not_machine_local_state_so_migration_and_adoption_leave_it_alone(
    tmp_path: Path,
) -> None:
    from exomem import procedure_approval as module

    assert module.KEY_DESCRIPTOR not in state_migration.declared_descriptor_ids()
    vault = tmp_path / "vault"
    (vault / kb_dirname()).mkdir(parents=True)
    authority = state_migration.assert_offline_migration_authority(source="approval key placement")
    state_migration.reset_state_resolution_cache_for_tests()
    state_migration.migrate_vault_state_offline(vault, authority=authority)
    key_file = module.key_path(vault)
    key_file.write_bytes(os.urandom(32))
    before = (key_file.read_bytes(), key_file.stat().st_ino, key_file.stat().st_mtime_ns)

    for keep in ("external", "vault"):
        # Dual state: an older release left a machine-local family inside the vault.
        (vault / kb_dirname() / ".graph-sync.json").write_bytes(b'{"epoch": "stray"}')
        state_migration.reset_state_resolution_cache_for_tests()
        state_migration.migrate_vault_state_offline(vault, authority=authority, adopt=keep)

        assert (
            key_file.read_bytes(),
            key_file.stat().st_ino,
            key_file.stat().st_mtime_ns,
        ) == before
