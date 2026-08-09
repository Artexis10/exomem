"""4b.42 — a compiled note's declared basis must resolve back to source ids.

The compiled altitude exists to measure whether a store preserves the chain it
declares. `_declared_basis` reads the `sources:` frontmatter that `remember`
writes and maps each wiki-link back to the oracle source id it names.

It resolved nothing. `capture_source` returns a vault path ending in `.md`,
while the wiki-link `remember` writes omits the extension, so an exact-string
map lookup missed on every link. Measured cost in the 2026-08-08 native
compiled run: **356 of 356 answers carried zero citations**, reported as
provenance 0/272 -- a number that says nothing about exomem and everything
about a string mismatch.

The trap is that the failure is silent and total. `_declared_basis` returns an
empty list for "no basis declared" and for "every lookup missed", so a broken
reader is indistinguishable from a store that cites nothing.
"""

from __future__ import annotations

import pytest
from membench.adapters import create_adapter
from membench.adapters.base import Profile


@pytest.fixture()
def adapter(tmp_path):
    a = create_adapter("exomem-local", altitude="compiled")
    a.setup(tmp_path / "wd", Profile(name="test"))
    return a


def _seed(adapter, vault, path: str, source_id: str) -> None:
    adapter._vault = vault
    adapter._register_source_path(source_id, path)


def test_a_wikilink_without_the_extension_resolves_to_its_source(adapter, tmp_path):
    """The exact shape the runs produced: map keyed with `.md`, link without."""

    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "Notes").mkdir(parents=True)
    _seed(adapter, vault, "Knowledge Base/Sources/Other/2026-08-08-runbook.md", "SRC-AAAA0001")

    note = vault / "Knowledge Base" / "Notes" / "conclusion.md"
    note.write_text(
        "---\n"
        "type: insight\n"
        "sources:\n"
        '  - "[[Knowledge Base/Sources/Other/2026-08-08-runbook]]"\n'
        "tags: []\n"
        "---\n\n"
        "# Conclusion\n",
        encoding="utf-8",
    )
    assert adapter._declared_basis("Knowledge Base/Notes/conclusion.md") == ["SRC-AAAA0001"]


def test_a_wikilink_carrying_the_extension_still_resolves(adapter, tmp_path):
    """Normalisation must not break the form that would have worked."""

    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "Notes").mkdir(parents=True)
    _seed(adapter, vault, "Knowledge Base/Sources/Other/2026-08-08-runbook.md", "SRC-AAAA0001")

    note = vault / "Knowledge Base" / "Notes" / "conclusion.md"
    note.write_text(
        "---\nsources:\n"
        '  - "[[Knowledge Base/Sources/Other/2026-08-08-runbook.md]]"\n'
        "---\n\n# Conclusion\n",
        encoding="utf-8",
    )
    assert adapter._declared_basis("Knowledge Base/Notes/conclusion.md") == ["SRC-AAAA0001"]


def test_a_link_naming_nothing_known_resolves_to_nothing(adapter, tmp_path):
    """The guard against 'fixing' this by matching too loosely: an unknown link
    must still resolve to nothing, or citation precision becomes unfalsifiable."""

    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "Notes").mkdir(parents=True)
    _seed(adapter, vault, "Knowledge Base/Sources/Other/2026-08-08-runbook.md", "SRC-AAAA0001")

    note = vault / "Knowledge Base" / "Notes" / "conclusion.md"
    note.write_text(
        '---\nsources:\n  - "[[Knowledge Base/Sources/Other/never-captured]]"\n---\n\n# C\n',
        encoding="utf-8",
    )
    assert adapter._declared_basis("Knowledge Base/Notes/conclusion.md") == []
