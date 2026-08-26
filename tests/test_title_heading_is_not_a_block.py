"""A page title that names a block type must not swallow the page.

Two individually reasonable rules composed into a destructive one. Blocks are
typed from any ATX heading whose normalized text matches the vocabulary, at any
level; a block ends at the next heading at its own level or shallower. At level
1 that closing condition essentially never fires, because a page has one title
heading — so a matching H1 opened a block that ran to end-of-file and absorbed
every `##` block in the page, silently and with no finding.
"""

from __future__ import annotations

import pytest

from exomem import semantic_blocks

_BODY = (
    "\n\nSome prose about the page.\n\n"
    "## Decision\n\nWe chose the bounded retry.\n\n"
    "## Open Question\n\nWhether the bound is right.\n"
)


def _types(markdown: str) -> list[tuple[str, int]]:
    parsed = semantic_blocks.parse_semantic_blocks(markdown)
    blocks = getattr(parsed, "blocks", parsed)
    return [(block.type, block.level) for block in blocks]


@pytest.mark.parametrize("title", ["Source", "Decision", "Open Question", "Claim", "Procedure"])
def test_a_title_naming_a_block_type_does_not_swallow_the_page(title: str) -> None:
    assert _types(f"# {title}{_BODY}") == [("decision", 2), ("open_question", 2)]


@pytest.mark.parametrize("title", ["Riverside council minutes", "Evidence: shot.png"])
def test_an_ordinary_title_parses_exactly_as_before(title: str) -> None:
    """The control. A title with a colon and a filename never normalized to a
    bare block name, so pages written by the preserve path were never at risk;
    pinning that bounds what this change corrects."""
    assert _types(f"# {title}{_BODY}") == [("decision", 2), ("open_question", 2)]


def test_deeper_nesting_keeps_its_current_meaning() -> None:
    """A `### Decision` inside a `## Claim` stays part of the claim's body."""
    markdown = (
        "# A page\n\n## Claim\n\nThe claim.\n\n### Decision\n\nA nested detail.\n"
    )

    assert _types(markdown) == [("claim", 2)]
    parsed = semantic_blocks.parse_semantic_blocks(markdown)
    blocks = getattr(parsed, "blocks", parsed)
    assert "### Decision" in blocks[0].body


def test_a_title_heading_still_closes_an_open_block() -> None:
    """Not starting a block is not the same as being invisible to the scanner."""
    markdown = (
        "# First\n\n## Claim\n\nThe claim.\n\n# Second\n\nProse after the title.\n"
    )

    parsed = semantic_blocks.parse_semantic_blocks(markdown)
    blocks = getattr(parsed, "blocks", parsed)

    assert [(block.type, block.level) for block in blocks] == [("claim", 2)]
    assert "Prose after the title." not in blocks[0].body
