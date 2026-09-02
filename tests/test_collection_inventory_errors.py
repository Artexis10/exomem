"""One bad manifest must not blind the sweep, and a directory is not "outside the vault".

Two defects in the shared collection abstraction, reproduced here before the fix:

* the inventory sweep aborted on the FIRST mislocated or unparseable
  `_collection.md`, so a single bad file hid every good collection from both
  profiles;
* a collection reference naming a real in-vault directory was refused as
  "outside the governed vault", which sends the caller looking for a path
  problem that does not exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_planning_mutation import _manifest as _planning_manifest

from exomem.cli_ops import OpError

_MISLOCATED_PLANNING_MANIFEST = """---
type: collection
exomem_id: 5c2f4a70-1d63-4c8b-9a9f-2f8b6f0d4e11
title: Misplaced planning work
semantic_profile: planning
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [title]
  fields:
    title:
      type: string
      required: true
---
"""

_UNPARSEABLE_MANIFEST = "---\nthis is: [not valid yaml\n---\n"

_MISLOCATED_PATH = "Knowledge Base/Records/Misplaced/_collection.md"
_UNPARSEABLE_PATH = "Knowledge Base/Records/Corrupt/_collection.md"
_PLANNING_PATH = "Knowledge Base/Planning/Work/_collection.md"
_GENERIC_REMEDIATION = "pass the collection's `_collection.md` manifest path"


def _seed_records_collection(vault: Path) -> str:
    """Seed a governed vault holding one released Records collection."""
    from exomem.record_memory import record_memory

    log = vault / "Knowledge Base" / "log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("# Activity\n", encoding="utf-8")
    example = record_memory(vault, "describe")["examples"]["minimal"]
    record_memory(
        vault,
        "create",
        manifest_path=example["manifest_path"],
        manifest_text=example["manifest_text"],
        scaffold=True,
        why="seed the records collection",
    )
    return str(example["manifest_path"])


def _seed_planning_collection(vault: Path, path: str = _PLANNING_PATH) -> str:
    from exomem.plan_memory import plan_memory

    plan_memory(
        vault,
        "create",
        manifest_path=path,
        manifest_text=_planning_manifest(),
        why="seed the planning collection",
    )
    return path


def _remediation_probe(vault: Path, surface: str, reference: str) -> dict[str, Any]:
    """Run one collection reference through a public surface, return its envelope."""
    if surface == "plan_memory":
        from exomem.plan_memory import plan_memory

        with pytest.raises(OpError) as raised:
            plan_memory(vault, "query", collection=reference)
    else:
        from exomem.record_memory import record_memory

        with pytest.raises(OpError) as raised:
            record_memory(vault, "inspect", collection=reference)
    return raised.value.as_public_dict()


def _write_manifest(vault: Path, relative: str, text: str) -> str:
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return relative


def test_records_inventory_reports_a_mislocated_planning_manifest(tmp_path: Path) -> None:
    from exomem.record_memory import record_memory

    released = _seed_records_collection(tmp_path)
    mislocated = _write_manifest(tmp_path, _MISLOCATED_PATH, _MISLOCATED_PLANNING_MANIFEST)

    inventory = record_memory(tmp_path, "inspect")

    assert [row["manifest_path"] for row in inventory["collections"]] == [released]
    assert inventory["unreadable_manifests"] == [
        {
            "path": mislocated,
            "error_code": "INVALID_COLLECTION_PATH",
            "message": "manifest must stay under Knowledge Base/Planning",
        }
    ]
    assert inventory["truncated"]["unreadable_manifests"] is False


def test_planning_inventory_reports_a_mislocated_planning_manifest(tmp_path: Path) -> None:
    from exomem import record_governance

    _seed_records_collection(tmp_path)
    released = _seed_planning_collection(tmp_path)
    mislocated = _write_manifest(tmp_path, _MISLOCATED_PATH, _MISLOCATED_PLANNING_MANIFEST)

    inventory = record_governance.inventory_collections(tmp_path, semantic_profile="planning")

    assert [row["manifest_path"] for row in inventory["collections"]] == [released]
    assert inventory["unreadable_manifests"] == [
        {
            "path": mislocated,
            "error_code": "INVALID_COLLECTION_PATH",
            "message": "manifest must stay under Knowledge Base/Planning",
        }
    ]
    assert "legacy_trackers" not in inventory


def test_an_unparseable_manifest_is_reported_rather_than_fatal(tmp_path: Path) -> None:
    from exomem.record_memory import record_memory

    released = _seed_records_collection(tmp_path)
    corrupt = _write_manifest(tmp_path, _UNPARSEABLE_PATH, _UNPARSEABLE_MANIFEST)

    inventory = record_memory(tmp_path, "inspect")

    assert [row["manifest_path"] for row in inventory["collections"]] == [released]
    assert [row["path"] for row in inventory["unreadable_manifests"]] == [corrupt]
    assert inventory["unreadable_manifests"][0]["error_code"]
    assert inventory["unreadable_manifests"][0]["message"]


def test_plan_memory_query_with_a_directory_names_the_manifest_requirement(
    tmp_path: Path,
) -> None:
    from exomem.plan_memory import plan_memory

    _seed_records_collection(tmp_path)
    _seed_planning_collection(tmp_path)
    directory = _PLANNING_PATH.rsplit("/", 1)[0]

    with pytest.raises(OpError) as raised:
        plan_memory(tmp_path, "query", collection=directory)

    error = raised.value
    assert error.code == "INVALID_COLLECTION_PATH"
    assert "outside the governed vault" not in error.message
    assert "_collection.md" in error.message
    assert error.remediation == f"{directory}/_collection.md"


def test_record_memory_inspect_with_a_directory_names_the_manifest_requirement(
    tmp_path: Path,
) -> None:
    from exomem.record_memory import record_memory

    released = _seed_records_collection(tmp_path)
    directory = released.rsplit("/", 1)[0]

    with pytest.raises(OpError) as raised:
        record_memory(tmp_path, "inspect", collection=directory)

    error = raised.value
    assert error.code == "INVALID_COLLECTION_PATH"
    assert "outside the governed vault" not in error.message
    assert "_collection.md" in error.message
    assert error.as_public_dict()["remediation"] == f"{directory}/_collection.md"


@pytest.mark.parametrize(
    "reference",
    [
        "../escape/_collection.md",
        "..",
        ".",
        "C:x",
        "seg\x00ment",
    ],
    ids=["traversal", "parent", "current", "drive", "nul"],
)
def test_a_reference_that_escapes_the_vault_still_reports_the_vault_boundary(
    tmp_path: Path, reference: str
) -> None:
    """A traversal spelled as one segment is still a traversal, not a title."""
    from exomem.plan_memory import plan_memory

    _seed_records_collection(tmp_path)

    with pytest.raises(OpError) as raised:
        plan_memory(tmp_path, "query", collection=reference)

    error = raised.value
    assert error.code == "INVALID_COLLECTION_PATH"
    assert error.message == "collection path is outside the governed vault"
    assert error.remediation is None


def test_a_bare_title_is_not_reported_as_a_path_that_escaped_the_vault(
    tmp_path: Path,
) -> None:
    from exomem.plan_memory import plan_memory

    _seed_records_collection(tmp_path)
    _seed_planning_collection(tmp_path)

    with pytest.raises(OpError) as raised:
        plan_memory(tmp_path, "query", collection="Planning work")

    error = raised.value
    assert error.code == "INVALID_COLLECTION_PATH"
    assert error.message == (
        "collection reference must be the collection's `_collection.md` manifest path "
        "(a title is not a reference)"
    )
    assert error.remediation == "pass the manifest path; use the collection's UUID if you have it"


# --- the remediation's gates: it may name a manifest only when that manifest is
# --- authorized for this caller AND safe to open. Each gate gets its own probe,
# --- because dropping either one alone still passes every other test here.


@pytest.mark.parametrize("surface", ["plan_memory", "record_memory"])
def test_an_excluded_directory_never_has_its_manifest_named(
    tmp_path: Path, surface: str
) -> None:
    _seed_records_collection(tmp_path)
    _seed_planning_collection(tmp_path, "Knowledge Base/Planning/Private/_collection.md")
    (tmp_path / "Knowledge Base" / "_access.yaml").write_text(
        "excluded:\n  - Planning/Private\n", encoding="utf-8"
    )

    envelope = _remediation_probe(tmp_path, surface, "Knowledge Base/Planning/Private")

    assert envelope["code"] == "INVALID_COLLECTION_PATH"
    assert envelope["remediation"] == _GENERIC_REMEDIATION
    assert "Private/_collection.md" not in json.dumps(envelope)


@pytest.mark.parametrize("surface", ["plan_memory", "record_memory"])
def test_a_symlinked_manifest_is_never_named_by_the_remediation(
    tmp_path: Path, surface: str
) -> None:
    _seed_records_collection(tmp_path)
    released = _seed_planning_collection(tmp_path)
    linked = tmp_path / "Knowledge Base" / "Planning" / "Linked" / "_collection.md"
    linked.parent.mkdir(parents=True)
    try:
        linked.symlink_to(tmp_path / released)
    except OSError:
        pytest.skip("symlinks unavailable")

    envelope = _remediation_probe(tmp_path, surface, "Knowledge Base/Planning/Linked")

    assert envelope["code"] == "INVALID_COLLECTION_PATH"
    assert envelope["remediation"] == _GENERIC_REMEDIATION
    assert "Linked/_collection.md" not in json.dumps(envelope)


@pytest.mark.parametrize("surface", ["plan_memory", "record_memory"])
def test_an_empty_directory_gets_the_generic_remediation(
    tmp_path: Path, surface: str
) -> None:
    _seed_records_collection(tmp_path)
    (tmp_path / "Knowledge Base" / "Planning" / "Empty").mkdir(parents=True)

    envelope = _remediation_probe(tmp_path, surface, "Knowledge Base/Planning/Empty")

    assert envelope["code"] == "INVALID_COLLECTION_PATH"
    assert envelope["remediation"] == _GENERIC_REMEDIATION
    assert "Empty/_collection.md" not in json.dumps(envelope)


@pytest.mark.parametrize("surface", ["plan_memory", "record_memory"])
def test_an_excluded_directory_is_indistinguishable_from_an_absent_one(
    tmp_path: Path, surface: str
) -> None:
    """The refusal must not leak one bit of existence per guess."""
    _seed_records_collection(tmp_path)
    _seed_planning_collection(tmp_path, "Knowledge Base/Planning/Private/_collection.md")
    (tmp_path / "Knowledge Base" / "_access.yaml").write_text(
        "excluded:\n  - Planning/Private\n", encoding="utf-8"
    )

    excluded = _remediation_probe(tmp_path, surface, "Knowledge Base/Planning/Private")
    absent = _remediation_probe(tmp_path, surface, "Knowledge Base/Planning/Absent")

    assert excluded == absent


@pytest.mark.parametrize("surface", ["plan_memory", "record_memory"])
def test_an_excluded_manifest_path_still_matches_an_absent_one(
    tmp_path: Path, surface: str
) -> None:
    """The manifest-path spelling already agreed; keep it agreeing."""
    _seed_records_collection(tmp_path)
    _seed_planning_collection(tmp_path, "Knowledge Base/Planning/Private/_collection.md")
    (tmp_path / "Knowledge Base" / "_access.yaml").write_text(
        "excluded:\n  - Planning/Private\n", encoding="utf-8"
    )

    excluded = _remediation_probe(
        tmp_path, surface, "Knowledge Base/Planning/Private/_collection.md"
    )
    absent = _remediation_probe(
        tmp_path, surface, "Knowledge Base/Planning/Absent/_collection.md"
    )

    assert excluded == absent
    assert excluded["code"] in {"COLLECTION_NOT_FOUND", "PLAN_NOT_FOUND", "RECORD_NOT_FOUND"}


# --- the sweep's consumers: a tolerant sweep that nobody counts is a silent
# --- skip one layer further out. Both consumers used to catch the abort and
# --- return empty; now that the sweep continues, they must carry the rows.


def test_plan_progress_counts_an_unreadable_manifest_as_unavailable(tmp_path: Path) -> None:
    from exomem import plan_progress

    _seed_records_collection(tmp_path)
    _seed_planning_collection(tmp_path)
    _write_manifest(tmp_path, _MISLOCATED_PATH, _MISLOCATED_PLANNING_MANIFEST)

    result = plan_progress.review(tmp_path)

    assert result["collections_unavailable"] == 1
    assert result["collections_scanned"] == 1
    # A count, never a path: the review surface says how much it could not read
    # without saying what.
    assert _MISLOCATED_PATH not in json.dumps(result)


def test_the_outcome_audit_reports_an_unreadable_manifest_as_unevaluated(
    tmp_path: Path,
) -> None:
    from exomem import audit

    _seed_records_collection(tmp_path)
    _seed_planning_collection(tmp_path)
    mislocated = _write_manifest(tmp_path, _MISLOCATED_PATH, _MISLOCATED_PLANNING_MANIFEST)

    _findings, meta = audit._check_unreflected_outcomes(tmp_path)

    assert meta["unevaluated"] == [
        {
            "collection": mislocated,
            "reason": "unreadable_manifest",
            "error_code": "INVALID_COLLECTION_PATH",
        }
    ]


# --- withheld must be indistinguishable from absent for EVERY spelling shape,
# --- not just the directory one. The answer has to be decided by the spelling
# --- and the exclusion policy, before the filesystem is consulted at all.

_PRIV = "Knowledge Base/Planning/PrivReg"
_ABSENT = "Knowledge Base/Planning/AbsentReg"
_PRIV_LINK = "Knowledge Base/Planning/PrivLink"
_ABSENT_LINK = "Knowledge Base/Planning/AbsentLink"
_PRIV_PARENT = "Knowledge Base/Planning/PrivParent"
_ABSENT_PARENT = "Knowledge Base/Planning/AbsentParent"

_WITHHELD_PAIRS = [
    (f"{_PRIV}/Items/x.md", f"{_ABSENT}/Items/x.md", False),
    (f"{_PRIV}/data.csv", f"{_ABSENT}/data.csv", False),
    (f"{_PRIV}/plainfile", f"{_ABSENT}/plainfile", False),
    (
        _PRIV.replace("/", "\\") + "\\_collection.md",
        _ABSENT.replace("/", "\\") + "\\_collection.md",
        False,
    ),
    (f"{_PRIV_LINK}/_collection.md", f"{_ABSENT_LINK}/_collection.md", True),
    (f"{_PRIV_PARENT}/_collection.md", f"{_ABSENT_PARENT}/_collection.md", True),
    (f"{_PRIV}/_collection.md/", f"{_ABSENT}/_collection.md/", False),
]
_WITHHELD_IDS = [
    "md_file",
    "csv_file",
    "extensionless",
    "backslash_manifest",
    "symlinked_manifest",
    "symlinked_parent",
    "trailing_slash",
]


def _seed_withheld_vault(vault: Path) -> bool:
    """One released Planning collection, later excluded, plus symlinked spellings."""
    _seed_records_collection(vault)
    _seed_planning_collection(vault, f"{_PRIV}/_collection.md")
    for relative, body in (
        (f"{_PRIV}/Items/x.md", "# Item\n"),
        (f"{_PRIV}/data.csv", "name,value\n"),
        (f"{_PRIV}/plainfile", "plain\n"),
    ):
        target = vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    symlinked = True
    try:
        (vault / _PRIV_LINK).mkdir(parents=True)
        (vault / _PRIV_LINK / "_collection.md").symlink_to(vault / _PRIV / "_collection.md")
        (vault / _PRIV_PARENT).symlink_to(vault / _PRIV)
    except OSError:
        symlinked = False
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "excluded:\n  - Planning/PrivReg\n  - Planning/PrivLink\n  - Planning/PrivParent\n",
        encoding="utf-8",
    )
    return symlinked


@pytest.mark.parametrize("surface", ["plan_memory", "record_memory"])
@pytest.mark.parametrize(
    ("withheld", "absent", "needs_symlink"), _WITHHELD_PAIRS, ids=_WITHHELD_IDS
)
def test_a_withheld_reference_answers_exactly_what_an_absent_one_answers(
    tmp_path: Path, surface: str, withheld: str, absent: str, needs_symlink: bool
) -> None:
    symlinked = _seed_withheld_vault(tmp_path)
    if needs_symlink and not symlinked:
        pytest.skip("symlinks unavailable")

    assert _remediation_probe(tmp_path, surface, withheld) == _remediation_probe(
        tmp_path, surface, absent
    )


@pytest.mark.parametrize("surface", ["plan_memory", "record_memory"])
def test_a_directory_whose_manifest_alone_is_withheld_never_names_it(
    tmp_path: Path, surface: str
) -> None:
    """The remediation's authorize gate, reached only when the two differ.

    Excluding the directory refuses the reference outright and never consults
    the gate, so the gate needs a case where the reference IS authorized and the
    manifest inside it is not.
    """
    _seed_records_collection(tmp_path)
    _seed_planning_collection(tmp_path, "Knowledge Base/Planning/Guarded/_collection.md")
    (tmp_path / "Knowledge Base" / "_access.yaml").write_text(
        "excluded:\n  - Planning/Guarded/_collection.md\n", encoding="utf-8"
    )

    envelope = _remediation_probe(tmp_path, surface, "Knowledge Base/Planning/Guarded")

    assert envelope["code"] == "INVALID_COLLECTION_PATH"
    assert envelope["remediation"] == _GENERIC_REMEDIATION
    assert "Guarded/_collection.md" not in json.dumps(envelope)
