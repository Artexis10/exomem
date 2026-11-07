"""`text_source_body` reads back exactly the text that `render_text_source` fenced, so that adoption can check a Source before it removes a staged copy."""
from __future__ import annotations

import hashlib

import pytest

from exomem import adopt as adopt_module
from exomem.vault import append_frontmatter_list_item

HOSTILE_TEXTS = {
    "a capture heading inside the text": "intro\n## Capture\n\nnot the real one\n",
    "a copy of the opening line inside the text": "intro\n\n## Capture\n\n```\nlooks like an opening fence\n```\n",
    "a long backtick run": "code: " + "`" * 9 + " end\n",
    "text that ends in backticks": "ends with a run ````",
    "CRLF line endings": "first\r\nsecond\r\n",
    "no trailing newline": "last line without a newline",
    "only a newline": "\n",
    "empty text": "",
    "multi-byte characters and a BOM": "﻿café ☕\n",
}


def _planned(text: str) -> dict:
    data = text.encode("utf-8")
    return {
        "title": "Plan", "original_path": "Notes/plan.md",
        "original_sha256": hashlib.sha256(data).hexdigest(), "original_bytes": len(data),
    }


def _page(text: str) -> str:
    return adopt_module.render_text_source(_planned(text), text, date_iso="2026-10-27", collection=None)


@pytest.mark.parametrize("text", HOSTILE_TEXTS.values(), ids=HOSTILE_TEXTS.keys())
def test_the_text_comes_back_exactly_also_after_a_note_adds_a_back_reference(text: str) -> None:
    page = _page(text)
    cited = append_frontmatter_list_item(page, "ingested_into", "[[Knowledge Base/Notes/plan]]")

    assert cited != page
    assert adopt_module.text_source_body(page) == text
    assert adopt_module.text_source_body(cited) == text


def test_a_page_that_is_not_in_the_format_of_render_text_source_returns_none() -> None:
    page = _page("some text\n")

    assert adopt_module.text_source_body("# A note\n\nplain text\n") is None
    assert adopt_module.text_source_body(page.removesuffix("\n")) is None
    assert adopt_module.text_source_body(page.replace("## Capture", "## Notes")) is None
    # A fence that the text could hold is not a fence our renderer would have chosen.
    assert adopt_module.text_source_body(page.replace("```", "``````")) is None
