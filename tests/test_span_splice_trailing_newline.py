"""Regression coverage for issue #546.

A single `plan_memory` item update that replaces an existing block-valued
field (dict, list-with-nested-items, or a literal/folded scalar) must not
glue that field's last line directly onto whatever follows it -- another
field's replacement, a brand-new appended field, or the closing `---` fence.
Block-style YAML nodes end their compose span *after* their own trailing
newline (unlike flow-style and plain scalars, whose span stops at the last
content byte), so the swallowed newline has to be reproduced at the splice
site. This happens for ANY replaced block-valued field, whether it is the
last spanned key or sits mid-document, and whether or not a new field is
also being appended -- the two are independent conditions, both covered
below.

Production observed the last-key + new-field combination as
`label: change 2tags:`, which `parse_frontmatter(strict=True)` rejects with
INVALID_FRONTMATTER -- and because the writer commits the malformed page
before any caller gets a chance to notice, the page is left unreadable in a
canonical (non-trash) directory. See also #545, the incident this caused
upstream.
"""

from __future__ import annotations

import pytest

from exomem import record_formats, vault
from exomem import structured_collections as collections

_SOURCE_TEMPLATE = (
    "---\n"
    "type: plan\n"
    "collection_id: 11111111-1111-4111-8111-111111111111\n"
    "plan_id: 22222222-2222-4222-8222-222222222222\n"
    "schema_version: 1\n"
    "status: active\n"
    "label: |\n"
    "  change 1\n"
    "{marker}"
    "---\n"
    "Body text.\n"
)


def test_replacing_trailing_block_field_and_adding_a_field_preserves_newline() -> None:
    """No pre-existing audit marker -- the exact `label: change 2tags:` shape."""
    source = _SOURCE_TEMPLATE.format(marker="")

    updated = record_formats.render_markdown_item_update(
        source,
        {"label": "change 2", "tags": ["urgent"]},
        semantic_profile="planning",
    )

    assert "change 2tags:" not in updated, (
        f"trailing block field glued directly to the new key: {updated!r}"
    )

    parsed, body, fm_text = vault.parse_frontmatter(updated, strict=True)
    assert parsed["label"] == "change 2"
    assert parsed["tags"] == ["urgent"]
    assert parsed["status"] == "active"
    assert body == "Body text.\n"
    assert fm_text is not None


def test_replacing_trailing_block_field_and_adding_a_field_preserves_newline_with_existing_audit_marker() -> None:
    """Same shape, but the item already carries a stale plan-audit marker line."""
    source = _SOURCE_TEMPLATE.format(
        marker="# exomem-plan-audit: 0123456789abcdef01234567\n"
    )

    updated = record_formats.render_markdown_item_update(
        source,
        {"label": "change 2", "tags": ["urgent"]},
        "fedcba9876543210fedcba98",
        semantic_profile="planning",
    )

    assert "change 2#" not in updated, (
        f"trailing block field glued directly to the stale audit marker: {updated!r}"
    )

    parsed, body, fm_text = vault.parse_frontmatter(updated, strict=True)
    assert parsed["label"] == "change 2"
    assert parsed["tags"] == ["urgent"]
    assert body == "Body text.\n"
    assert fm_text is not None
    # The stale marker must be replaced, not duplicated alongside the new one.
    assert updated.count("exomem-plan-audit:") == 1
    assert "# exomem-plan-audit: fedcba9876543210fedcba98" in updated


def test_replacing_a_mid_document_block_dict_field_preserves_the_following_field() -> None:
    """A block DICT field replaced mid-document must not eat the next field."""
    source = (
        "---\n"
        "type: plan\n"
        "collection_id: 11111111-1111-4111-8111-111111111111\n"
        "plan_id: 22222222-2222-4222-8222-222222222222\n"
        "schema_version: 1\n"
        "meta:\n"
        "  a: 1\n"
        "  b: 2\n"
        "status: active\n"
        "---\n"
        "Body text.\n"
    )

    updated = record_formats.render_markdown_item_update(
        source, {"meta": {"a": 9, "b": 8}}, semantic_profile="planning"
    )

    assert "8status:" not in updated, f"mid-document dict field glued to the next key: {updated!r}"
    assert "  b: 8\nstatus: active\n" in updated

    parsed, _body, _fm_text = vault.parse_frontmatter(updated, strict=True)
    assert parsed["meta"] == {"a": 9, "b": 8}
    assert parsed["status"] == "active"


def test_replacing_a_mid_document_block_list_field_preserves_the_following_field() -> None:
    """A block LIST field (indented `- item` style) replaced mid-document must
    not eat the next field either -- the swallowed-newline span behaviour is
    the same for block lists as for block dicts."""
    source = (
        "---\n"
        "type: plan\n"
        "collection_id: 11111111-1111-4111-8111-111111111111\n"
        "plan_id: 22222222-2222-4222-8222-222222222222\n"
        "schema_version: 1\n"
        "tags:\n"
        "  - old1\n"
        "  - old2\n"
        "status: active\n"
        "---\n"
        "Body text.\n"
    )

    updated = record_formats.render_markdown_item_update(
        source, {"tags": ["new1"]}, semantic_profile="planning"
    )

    assert "]status:" not in updated, f"mid-document list field glued to the next key: {updated!r}"
    assert "tags: [new1]\nstatus: active\n" in updated

    parsed, _body, _fm_text = vault.parse_frontmatter(updated, strict=True)
    assert parsed["tags"] == ["new1"]
    assert parsed["status"] == "active"


def test_replacing_the_last_frontmatter_key_as_a_block_field_preserves_the_closing_fence() -> None:
    """A block field replaced as the LAST key, with no new field appended.

    On the pre-fix baseline this produced `label: change 2---\\nBody text.\\n`
    -- the closing fence glued onto the value. `_FM_PATTERN` (which requires
    `---` on its own line) then fails to find the fence at all, so
    `parse_frontmatter` silently reports NO frontmatter (`{}`, no marker)
    instead of raising. That silent, no-exception failure mode is exactly
    what the `self_check_marker is None` guard branch exists to catch.
    """
    source = (
        "---\n"
        "type: plan\n"
        "collection_id: 11111111-1111-4111-8111-111111111111\n"
        "plan_id: 22222222-2222-4222-8222-222222222222\n"
        "schema_version: 1\n"
        "status: active\n"
        "label: |\n"
        "  change 1\n"
        "---\n"
        "Body text.\n"
    )

    updated = record_formats.render_markdown_item_update(
        source, {"label": "change 2"}, semantic_profile="planning"
    )

    assert "change 2---" not in updated, f"trailing block field glued to the closing fence: {updated!r}"
    assert updated.endswith("label: change 2\n---\nBody text.\n")

    parsed, body, fm_text = vault.parse_frontmatter(updated, strict=True)
    assert parsed["label"] == "change 2"
    assert parsed["status"] == "active"
    assert body == "Body text.\n"
    assert fm_text is not None


def test_replacing_two_block_fields_simultaneously_plus_an_appended_field() -> None:
    """Two block-valued fields replaced in the same call, plus a brand-new
    appended field. `sorted(replacements, reverse=True)` applies the
    highest-offset replacement (the later field, `items`) first so that the
    earlier field's (`meta`) offsets are not invalidated by a length change
    from its own replacement.

    The `meta` replacement is deliberately length-changing (its rendered
    block is longer than the original) so this actually exercises that
    ordering: applying `items` before `meta` leaves `meta`'s recorded span
    untouched by `items`'s own splice, so `meta`'s later-applied replacement
    still lands at the right offset. Confirmed by construction: flipping the
    loop to `reverse=False` on this exact input corrupts the document and
    trips the post-write guard (`INVALID_RECORD_ITEM`), while the real,
    reverse=True ordering produces correct output.
    """
    source = (
        "---\n"
        "type: plan\n"
        "collection_id: 11111111-1111-4111-8111-111111111111\n"
        "plan_id: 22222222-2222-4222-8222-222222222222\n"
        "schema_version: 1\n"
        "meta:\n"
        "  a: 1\n"
        "  b: 2\n"
        "items:\n"
        "  - k: 1\n"
        "  - k: 2\n"
        "---\n"
        "Body text.\n"
    )

    updated = record_formats.render_markdown_item_update(
        source,
        {"meta": {"a": 123456789, "b": 987654321}, "items": [{"k": 99}], "tags": ["new"]},
        semantic_profile="planning",
    )

    assert "987654321items:" not in updated, f"meta field glued to items field: {updated!r}"
    assert "99tags:" not in updated, f"items field glued to the appended tags field: {updated!r}"

    parsed, _body, _fm_text = vault.parse_frontmatter(updated, strict=True)
    assert parsed["meta"] == {"a": 123456789, "b": 987654321}
    assert parsed["items"] == [{"k": 99}]
    assert parsed["tags"] == ["new"]


def test_post_write_self_check_rejects_a_splice_that_would_still_be_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The splice/render boundary re-parses its own output and fails loudly.

    This is deliberately independent of the newline fix above: it proves the
    guard itself trips, by forcing `parse_frontmatter` to report the rebuilt
    page as unreadable, the way it would for any future splice regression --
    not just this one.
    """
    source = _SOURCE_TEMPLATE.format(marker="")
    real_parse_frontmatter = vault.parse_frontmatter

    def _always_rejects(text: str, *, strict: bool = False):
        if strict:
            raise vault.FrontmatterError("INVALID_FRONTMATTER", "forced for self-check test")
        return real_parse_frontmatter(text, strict=strict)

    monkeypatch.setattr(vault, "parse_frontmatter", _always_rejects)

    with pytest.raises(collections.CollectionError, match="INVALID_RECORD_ITEM"):
        record_formats.render_markdown_item_update(
            source,
            {"label": "change 2", "tags": ["urgent"]},
            semantic_profile="planning",
        )


def test_post_write_self_check_rejects_a_splice_whose_frontmatter_fence_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers the guard's `self_check_marker is None` branch specifically.

    A splice that glues content into the closing `---` fence doesn't always
    raise on re-parse -- `_FM_PATTERN` can simply fail to find a fence at
    all, so `parse_frontmatter` returns no exception and `({}, text, None)`.
    That is the silent, worse failure mode (see the last-key test above);
    the guard must treat a missing marker as a rejection too, not just an
    exception.
    """
    source = _SOURCE_TEMPLATE.format(marker="")
    real_parse_frontmatter = vault.parse_frontmatter

    def _reports_no_frontmatter_found(text: str, *, strict: bool = False):
        if strict:
            return {}, text, None
        return real_parse_frontmatter(text, strict=strict)

    monkeypatch.setattr(vault, "parse_frontmatter", _reports_no_frontmatter_found)

    with pytest.raises(collections.CollectionError, match="INVALID_RECORD_ITEM"):
        record_formats.render_markdown_item_update(
            source,
            {"label": "change 2", "tags": ["urgent"]},
            semantic_profile="planning",
        )
