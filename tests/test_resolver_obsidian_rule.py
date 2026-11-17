"""Step 6 of the link resolver: Obsidian's rule, in a `nested` and in a `root` vault.

The rule reads a link text as a case-insensitive basename or as the end of a path. It runs after every
earlier step, so a link that resolved before keeps its result, and it resolves a link only when a single
file satisfies it. The resolver's corpus leaves the machinery folders out, so a link into them stays unresolved.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from exomem import kbdir
from exomem.vault import WikilinkResolver, normalize_wikilink


@pytest.fixture(params=[kbdir.Layout.NESTED, kbdir.Layout.ROOT])
def prefix(request: pytest.FixtureRequest) -> str:
    """The folder that a vault-relative page path starts with: the wrapper folder or none."""
    kbdir.bind(kbdir.Managed(request.param))
    return "Knowledge Base/" if request.param is kbdir.Layout.NESTED else ""


def _resolve(
    root: Path,
    prefix: str,
    link: str,
    pages: list[str],
    titles: dict[str, str] | None = None,
    **options: object,
) -> tuple[str, str | None]:
    entries = [(prefix + page, (titles or {}).get(page)) for page in pages]
    resolver = WikilinkResolver.from_entries(root, entries)
    return normalize_wikilink(link, root, resolver=resolver, retired=lambda _path: None, **options)


RECEIPTS = ["Evidence/alpha/receipts/Scan.md", "Evidence/beta/ledgers/Scan.md", "Notes/Plan.md"]


def test_a_unique_trailing_path_resolves(tmp_path: Path, prefix: str) -> None:
    resolved, warning = _resolve(tmp_path, prefix, "receipts/Scan", RECEIPTS)

    assert warning is None
    assert resolved == f"{prefix}Evidence/alpha/receipts/Scan"


def test_an_ambiguous_trailing_path_stays_unresolved(tmp_path: Path, prefix: str) -> None:
    pages = [*RECEIPTS, "Evidence/gamma/receipts/Scan.md"]

    resolved, warning = _resolve(tmp_path, prefix, "receipts/Scan", pages)

    assert warning is not None and "does not resolve" in warning
    assert resolved.endswith("receipts/Scan")


def test_a_name_that_differs_only_in_letter_case_resolves(tmp_path: Path, prefix: str) -> None:
    resolved, warning = _resolve(tmp_path, prefix, "release checklist", ["Notes/Release Checklist.md"])

    assert warning is None
    assert resolved == f"{prefix}Notes/Release Checklist"


def test_an_nfd_spelling_resolves_to_the_nfc_file(tmp_path: Path, prefix: str) -> None:
    stored = unicodedata.normalize("NFC", "Notes/Café menu.md")
    typed = unicodedata.normalize("NFD", "Café menu")
    assert stored != typed

    resolved, warning = _resolve(tmp_path, prefix, typed, [stored])

    assert warning is None
    assert resolved == f"{prefix}{stored.removesuffix('.md')}"


def test_a_homonym_that_differs_in_letter_case_is_ambiguous(tmp_path: Path, prefix: str) -> None:
    resolved, warning = _resolve(tmp_path, prefix, "SCAN", RECEIPTS)

    assert warning is not None and "does not resolve" in warning


def test_a_page_that_a_writer_may_not_see_is_neither_resolved_nor_counted(
    tmp_path: Path, prefix: str
) -> None:
    withheld = f"{prefix}Evidence/beta/ledgers/Scan.md"

    resolved, warning = _resolve(
        tmp_path, prefix, "scan", RECEIPTS, visible=lambda path: path != withheld
    )
    only_withheld, withheld_warning = _resolve(
        tmp_path, prefix, "scan", ["Evidence/beta/ledgers/Scan.md"], visible=lambda _path: False
    )

    assert (warning, resolved) == (None, f"{prefix}Evidence/alpha/receipts/Scan")
    assert withheld_warning is not None and "Evidence/beta" not in only_withheld


def test_earlier_steps_keep_their_results(tmp_path: Path, prefix: str) -> None:
    pages = ["Evidence/alpha/receipts/Scan.md", "Evidence/beta/receipts/scan.md", "Notes/Plan.md"]
    titles = {"Notes/Plan.md": "Receipts"}

    exact, _ = _resolve(tmp_path, prefix, "Evidence/alpha/receipts/Scan", pages)
    stem, _ = _resolve(tmp_path, prefix, "Scan", pages)
    title, _ = _resolve(tmp_path, prefix, "Receipts", pages, titles)

    assert exact == f"{prefix}Evidence/alpha/receipts/Scan"
    # Two files satisfy the rule for `Scan`, yet the case-exact stem step resolves it to a single file.
    assert stem == f"{prefix}Evidence/alpha/receipts/Scan"
    assert title == f"{prefix}Notes/Plan"


def test_a_retired_collection_path_keeps_precedence(tmp_path: Path, prefix: str) -> None:
    resolver = WikilinkResolver.from_entries(tmp_path, [(prefix + page, None) for page in RECEIPTS])

    resolved, warning = normalize_wikilink(
        "receipts/Scan",
        tmp_path,
        resolver=resolver,
        retired=lambda path: "Records/Plans/item-9.md" if path == "receipts/Scan" else None,
    )

    assert (resolved, warning) == ("Records/Plans/item-9", None)


def test_a_link_into_a_machinery_folder_stays_unresolved(tmp_path: Path, prefix: str) -> None:
    (tmp_path / prefix / "_Schema").mkdir(parents=True)
    (tmp_path / prefix / "_Schema" / "writing.md").write_text("Guide\n", encoding="utf-8")
    (tmp_path / prefix / "Notes").mkdir(parents=True)
    (tmp_path / prefix / "Notes" / "Plan.md").write_text("Plan\n", encoding="utf-8")
    resolver = WikilinkResolver(tmp_path)

    unresolved, warning = normalize_wikilink(
        "writing", tmp_path, resolver=resolver, retired=lambda _path: None
    )
    plan, plan_warning = normalize_wikilink(
        "plan", tmp_path, resolver=resolver, retired=lambda _path: None
    )

    assert warning is not None and "does not resolve" in warning
    assert unresolved == "writing"
    assert (plan, plan_warning) == (f"{prefix}Notes/Plan", None)
