"""On-box proof that a case-insensitive filesystem can no longer split one
governed page into two identity owners.

The incident: a client addressed `Knowledge Base/Notes/Research/Polly/…` while
the directory on disk is `POLLY`. NTFS opens the same single file either way,
but the stable-identity census recorded the real on-disk casing while the write
pipeline carried the caller's casing, and the ownership guard compared them
byte-for-byte — so one physical file read as having "another corpus owner" and
every edit was refused with `SEMANTIC_IDENTITY_DUPLICATE` /
`SEMANTIC_CONTRACT_BLOCKED`.

`test_case_insensitive_identity.py` pins the policy layers platform-free (it
models the fold via `EXOMEM_CASEFOLD_PATHS`, so Linux CI covers both branches).
This module is the complement: it uses a REAL case-folding filesystem, with no
env override and no monkeypatching, so it proves the canonicalization actually
lands on the real on-disk spelling rather than a modelled one.

GATED: skips unless running on Windows AND the volume backing the temp vault
really folds case. Windows supports per-directory case sensitivity
(`fsutil file setCaseSensitiveInfo`), so probe rather than assume — skip
gracefully there instead of false-failing.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from exomem import activation_manifest, semantic_contract
from exomem import append_to_file as append_module
from exomem import create_file as create_file_module
from exomem import edit as edit_module
from exomem import multi_edit as multi_edit_module
from exomem import set_frontmatter_field as set_frontmatter_module
from exomem import vault as vault_module

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="case-insensitive path aliasing only reproduces on Windows",
)

TODAY = dt.date(2026, 7, 24)

_ID = "35494c6a-da47-4694-ab0c-47552571d73f"
_LEAF = "provisional-commercial-terms.md"
# The on-disk directory (what the identity census walks) vs. the spelling the
# blocked client sent. Both open the same directory on NTFS.
_REAL_DIR = "Knowledge Base/Notes/Research/POLLY"
_PHANTOM_DIR = "Knowledge Base/Notes/Research/Polly"
_REAL_REL = f"{_REAL_DIR}/{_LEAF}"
_PHANTOM_REL = f"{_PHANTOM_DIR}/{_LEAF}"


def _note_source(*, rate: str, title: str = "Provisional commercial terms") -> str:
    """A fully contract-compliant governed page: a semantic unit and a
    qualifying relation, so nothing but the path casing is ever in question."""
    return (
        "---\n"
        f"title: {title}\n"
        # Notes/Research/ is a canonical compiled destination and pins the type.
        "type: research-note\n"
        "status: active\n"
        f"exomem_id: {_ID}\n"
        "---\n\n"
        "## Observations\n\n"
        f"- [commercial term] The provisional rate is {rate} per hour #polly\n\n"
        "## Relations\n\n"
        "- relates_to [[Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization]]\n"
    )


def _filesystem_casefolds(directory: Path) -> bool:
    """Whether `directory`'s volume really opens one file under two spellings."""
    probe = directory / "_exomem_case_probe.tmp"
    probe.write_text("probe", encoding="utf-8")
    try:
        swapped = directory / "_EXOMEM_CASE_PROBE.TMP"
        return swapped.is_file() and os.path.samefile(probe, swapped)
    finally:
        probe.unlink()


@pytest.fixture
def folding_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The conftest vault, guaranteed to sit on a case-folding filesystem.

    No `EXOMEM_CASEFOLD_PATHS` override: this module's whole point is that the
    real probe answers correctly against a real volume.
    """
    if not _filesystem_casefolds(vault):
        pytest.skip(
            "the volume backing tmp_path is case-sensitive (per-directory case "
            "sensitivity is enabled) — nothing to reproduce here"
        )
    monkeypatch.delenv("EXOMEM_CASEFOLD_PATHS", raising=False)
    vault_module.reset_casefold_probe_cache()
    yield vault
    vault_module.reset_casefold_probe_cache()


def _seed_governed_note(root: Path, *, rate: str = "37.50 EUR") -> Path:
    """Write the governed note under the REAL on-disk casing (`POLLY`).

    The activation manifest is installed deliberately. Without it the page is
    grandfathered, and grandfathering downgrades any error the *before* state
    already carries — which includes this very identity finding, since the
    before state is mis-keyed too. A live vault long past activation is the
    condition the incident actually occurred under, so pin it here or the guard
    never gets to speak.
    """
    directory = root / _REAL_DIR
    directory.mkdir(parents=True, exist_ok=True)
    page = directory / _LEAF
    page.write_text(_note_source(rate=rate), encoding="utf-8", newline="\n")
    activation_manifest.ensure_manifest(root)
    return page


# ---------------- 1. the incident ----------------


def test_edit_addressed_with_phantom_casing_commits_to_the_one_on_disk_page(
    folding_vault: Path,
) -> None:
    """The exact production failure: ONE file on disk under `POLLY/`, addressed
    by the caller as `Polly/`. Before the fix the census recorded `POLLY` while
    the write pipeline carried `Polly`, the ownership guard compared them
    case-sensitively, and the edit was refused as a duplicate identity. It must
    now commit, and every path it records must be the real on-disk spelling.
    """
    page = _seed_governed_note(folding_vault)
    # Test invariant: the two spellings are one file, not two.
    phantom = folding_vault / _PHANTOM_REL
    assert phantom.is_file()
    assert os.path.samefile(page, phantom)

    try:
        result = edit_module.edit(
            folding_vault,
            path=_PHANTOM_REL,
            why="land the blocked commercial-terms update",
            old_string="37.50 EUR",
            new_string="100.00 EUR",
            today=TODAY,
        )
    except edit_module.EditError as error:  # pragma: no cover - regression path
        pytest.fail(
            f"a differently-cased address for the single on-disk page was refused: "
            f"{error.code}: {error.reason}"
        )

    assert result.path == _REAL_REL
    assert result.semantic is not None
    assert result.semantic["path"] == _REAL_REL
    assert result.semantic["mutated"] is True
    assert _PHANTOM_REL not in result.semantic["written_paths"]
    assert _REAL_REL in result.semantic["written_paths"]
    assert "100.00 EUR" in page.read_text(encoding="utf-8")
    # Still exactly one physical file — the fix must not have created a sibling.
    assert sorted(p.name for p in (folding_vault / _REAL_DIR).iterdir()) == [_LEAF]


# ---------------- 2. create into an existing differently-cased directory ----------------


def test_create_into_differently_cased_directory_canonicalizes_only_the_parent(
    folding_vault: Path,
) -> None:
    """`POLLY/` exists on disk; the caller creates into `Polly/`. The parent
    prefix must be re-spelled to the on-disk casing, while the brand-new leaf —
    which has no on-disk spelling yet — keeps the casing the author chose."""
    (folding_vault / _REAL_DIR).mkdir(parents=True, exist_ok=True)
    leaf = "Provisional-Commercial-Terms-Addendum.md"
    payload = {
        "path": f"{_PHANTOM_DIR}/{leaf}",
        "content": (
            "# Addendum\n\n"
            "## Observations\n\n"
            "- [commercial term] The weekly cap is 25 hours #polly\n"
        ),
        # Notes/Research/ is a canonical compiled destination, so the contract
        # pins the type; unrelated to the casing behavior under test.
        "frontmatter": {"type": "research-note", "status": "active"},
        "today": TODAY,
    }

    draft = create_file_module.create_file(folding_vault, validate_only=True, **payload)
    # The draft token binds the destination it was minted for; a phantom-cased
    # second call must resolve to that same canonical destination or the commit
    # is refused outright.
    assert draft.destination == f"{_REAL_DIR}/{leaf}"

    result = create_file_module.create_file(
        folding_vault,
        draft_id=draft.draft_id,
        draft_hash=draft.draft_hash,
        draft_token=draft.draft_token,
        relation_disposition="reviewed_none",
        relation_review_hash=draft.draft_hash,
        relation_review_reason="No honest relation exists in this fixture corpus.",
        **payload,
    )

    assert result.path == f"{_REAL_DIR}/{leaf}"
    written = folding_vault / _REAL_DIR / leaf
    assert written.is_file()
    # The leaf landed with the authored casing, not a folded or re-spelled one.
    assert [p.name for p in (folding_vault / _REAL_DIR).iterdir()] == [leaf]


# ---------------- 3. the two resolution boundaries ----------------


def test_resolve_under_vault_returns_the_on_disk_casing(folding_vault: Path) -> None:
    _seed_governed_note(folding_vault)

    abs_path, rel = vault_module.resolve_under_vault(
        folding_vault, _PHANTOM_REL, must_exist=True, must_be_file=True, must_be_under_kb=True
    )

    assert rel == _REAL_REL
    assert os.path.samefile(abs_path, folding_vault / _REAL_REL)


def test_resolve_under_vault_canonicalizes_the_parent_of_a_missing_leaf(
    folding_vault: Path,
) -> None:
    """The create path: the parent exists under another casing, the leaf does
    not exist at all. Non-strict resolution must re-spell the former and leave
    the latter verbatim."""
    (folding_vault / _REAL_DIR).mkdir(parents=True, exist_ok=True)

    _, rel = vault_module.resolve_under_vault(
        folding_vault, f"{_PHANTOM_DIR}/Not-Yet-Written.md", must_be_under_kb=True
    )

    assert rel == f"{_REAL_DIR}/Not-Yet-Written.md"


def test_edit_resolve_returns_the_on_disk_casing(folding_vault: Path) -> None:
    _seed_governed_note(folding_vault)

    candidate, rel = edit_module._resolve(folding_vault, _PHANTOM_REL)

    assert rel == _REAL_REL
    assert os.path.samefile(candidate, folding_vault / _REAL_REL)


def test_edit_resolve_canonicalizes_a_kb_prefixless_address(folding_vault: Path) -> None:
    """`edit` accepts a KB-relative address and re-roots it. The re-rooted form
    must still come back in the on-disk casing."""
    _seed_governed_note(folding_vault)

    _, rel = edit_module._resolve(folding_vault, f"Notes/Research/Polly/{_LEAF}")

    assert rel == _REAL_REL


# ---------------- 4. warm-corpus delta keying ----------------


def test_warm_corpus_delta_keys_a_phantom_cased_event_under_the_on_disk_path(
    folding_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A files-changed event carrying a caller-cased RELATIVE path must patch
    the page the census already knows, not manufacture a second entry keyed
    under the phantom spelling. (The absolute branch already resolved; the
    relative branch did not.)"""
    # This suite's conftest defaults the corpus cache off; the delta patch only
    # runs against a warm entry, so opt back in for this test.
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.reset_corpus_context_cache()

    page = _seed_governed_note(folding_vault)
    warm = semantic_contract.build_corpus_context(folding_vault)
    assert _REAL_REL in warm.pages, "test setup: the census must see the on-disk page"

    page.write_text(
        _note_source(rate="100.00 EUR", title="Event patched terms"),
        encoding="utf-8",
        newline="\n",
    )
    semantic_contract.on_corpus_files_changed(folding_vault, changed=(Path(_PHANTOM_REL),))

    cache_key = semantic_contract._corpus_cache_key(folding_vault)
    entry = semantic_contract._CORPUS_CONTEXT_CACHE.get(cache_key)
    assert entry is not None, "test setup: the warm entry should still be cached"
    patched = entry[1]

    assert _PHANTOM_REL not in patched.pages
    assert _REAL_REL in patched.pages
    census_paths = {item.path for item in patched.identity_census.entries}
    assert _PHANTOM_REL not in census_paths
    assert _REAL_REL in census_paths
    # The delta really landed on the canonical key rather than being dropped.
    assert patched.identity_census.paths_by_identity[_ID] == (_REAL_REL,)
    assert patched.pages[_REAL_REL].title == "Event patched terms"


# ---------------- 5. the other write paths ----------------


def _multi_edit(root: Path) -> str:
    return multi_edit_module.multi_edit(
        root,
        path=_PHANTOM_REL,
        why="batch the phantom-cased address",
        edits=[{"old_string": "37.50 EUR", "new_string": "100.00 EUR"}],
        today=TODAY,
    ).path


def _append(root: Path) -> str:
    return append_module.append_to_file(
        root,
        path=_PHANTOM_REL,
        # Opens a new section: the seeded page ends with `## Relations`, and a
        # bare bullet appended there would parse as a malformed relation.
        content="\n## Addendum\n\n- [commercial term] Weekly cap is 25 hours #polly\n",
        today=TODAY,
    ).path


def _set_frontmatter(root: Path) -> str:
    return set_frontmatter_module.set_frontmatter_field(
        root,
        path=_PHANTOM_REL,
        field="domain",
        value="commercial",
        why="record the domain on the phantom-cased address",
        today=TODAY,
    ).path


@pytest.mark.parametrize(
    "writer",
    [
        pytest.param(_multi_edit, id="multi_edit"),
        pytest.param(_append, id="append_to_file"),
        pytest.param(_set_frontmatter, id="set_frontmatter_field"),
    ],
)
def test_other_writers_accept_a_phantom_cased_address(
    folding_vault: Path, writer: Callable[[Path], str]
) -> None:
    """Every Tier 2 writer reaches the same identity guard, so each must resolve
    the caller's casing to the one on-disk page and report it back canonically."""
    _seed_governed_note(folding_vault)

    assert writer(folding_vault) == _REAL_REL
