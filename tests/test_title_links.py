"""Links shown by page title to an agent, and taken back as the stored path form.

The vault stores `[[Knowledge Base/Notes/Insights/slug]]`. An agent on the public tool shape reads
`[[Page Title]]`, copies any span of the page into `edit_note`, and the edit lands on the stored
bytes. Every case here runs on a real vault and a real resolver.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import pytest
from fastmcp import Client

from exomem import call_ledger, capabilities, commands, edit, find, freshness, writer_lease
from exomem import server as server_module
from exomem.vault import (
    TitleLinkView,
    WikilinkResolver,
    normalize_body_wikilinks,
    render_title_links,
    walk_vault_md,
)

NOTES = "Knowledge Base/Notes/Insights"


def _page(vault: Path, stem: str, title: str) -> str:
    """Write a page whose frontmatter names `title`, and return its stored link target."""
    path = vault / NOTES / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\ntitle: "{title}"\ntype: insight\n---\n\n# {title}\n', encoding="utf-8")
    return f"{NOTES}/{stem}"


@pytest.fixture
def public_shape():
    """Serve the call as the public MCP tool shape does: the shape that shows links by title."""
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="product", tier2_enabled=True, product_commands=(), tool_shape="public"
    )
    with capabilities.active_surface(descriptor):
        yield


def _view(vault: Path, body: str, visible=None) -> TitleLinkView:
    return render_title_links(body, vault, resolver=WikilinkResolver(vault), visible=visible)


def _unshown(view: TitleLinkView, body: str, start: int, end: int) -> str:
    """The stored text of shown `[start, end)`: each whole link in it is put back as stored."""
    out, cursor = [], start
    for stored_start, stored_end, shown_start, shown_end in view.spans:
        if shown_start >= start and shown_end <= end:
            out.append(view.text[cursor:shown_start])
            out.append(body[stored_start:stored_end])
            cursor = shown_end
    out.append(view.text[cursor:end])
    return "".join(out)


def test_a_link_is_shown_by_title_with_its_anchor_and_alias(vault: Path) -> None:
    target = _page(vault, "alpha-slug", "Alpha Title")
    body = f"See [[{target}]] and [[{target}#Part|the part]].\n"

    view = _view(vault, body)

    assert view.text == "See [[Alpha Title]] and [[Alpha Title#Part|the part]].\n"


def test_a_duplicate_title_keeps_the_stored_link(vault: Path) -> None:
    first = _page(vault, "alpha-one", "Alpha Title")
    _page(vault, "alpha-two", "Alpha Title")
    body = f"See [[{first}]].\n"

    assert _view(vault, body).text == body


def test_a_title_that_is_another_pages_stem_keeps_the_stored_link(vault: Path) -> None:
    # `[[gamma]]` resolves by stem before title, so it would write back to the other page.
    titled = _page(vault, "gamma-notes", "gamma")
    _page(vault, "gamma", "Something else")
    body = f"See [[{titled}]].\n"

    assert _view(vault, body).text == body


def test_a_title_that_a_link_cannot_carry_keeps_the_stored_link(vault: Path) -> None:
    target = _page(vault, "paths-slug", "Read/Write Paths")
    body = f"See [[{target}]].\n"

    assert _view(vault, body).text == body


def test_a_title_with_a_backtick_keeps_the_stored_link_and_its_neighbours_come_back_whole(vault: Path) -> None:
    # A write masks code spans: a lone backtick pairs with a later one and hides the links between.
    ticked = _page(vault, "tick-slug", "A lone ` tick")
    paired = _page(vault, "code-slug", "Never use `git stash`")
    other = _page(vault, "open-slug", "Open Title")
    body = f"See [[{ticked}]], [[{paired}]] and [[{other}]] and `code`.\n"

    view = _view(vault, body)

    assert view.text == f"See [[{ticked}]], [[{paired}]] and [[Open Title]] and `code`.\n"
    assert normalize_body_wikilinks(view.text, vault, resolver=WikilinkResolver(vault))[0] == body


def test_an_alias_with_inline_code_is_shown_and_taken_back_intact(vault: Path) -> None:
    target = _page(vault, "alpha-slug", "Alpha Title")
    body = f"Use [[{target}|the `x` flag]] here.\n"

    view = _view(vault, body)

    assert view.text == "Use [[Alpha Title|the `x` flag]] here.\n"
    assert normalize_body_wikilinks(view.text, vault, resolver=WikilinkResolver(vault))[0] == body


def test_a_withheld_target_keeps_the_stored_link_and_shows_no_title(vault: Path) -> None:
    withheld = _page(vault, "secret-slug", "Secret Title")
    shown = _page(vault, "open-slug", "Open Title")
    body = f"See [[{withheld}]] and [[{shown}]].\n"

    view = _view(vault, body, visible=lambda path: "secret-slug" not in path)

    assert view.text == f"See [[{withheld}]] and [[Open Title]].\n"


def test_a_link_in_code_is_left_as_written(vault: Path) -> None:
    target = _page(vault, "alpha-slug", "Alpha Title")
    body = f"Inline `[[{target}]]` and\n\n```\n[[{target}]]\n```\n"

    assert _view(vault, body).text == body


@pytest.mark.parametrize(
    "template",
    [
        "Plain [[{t}]] text.\n",
        "[[{t}]][[{t}#Part]] adjacent, then [[{t}|alias]]\n",
        "A kept [[{t}]] next to a duplicate [[{d}]] and a forward [[Not yet written]].\n",
    ],
)
def test_every_range_of_the_shown_body_maps_back_to_the_stored_bytes(vault: Path, template: str) -> None:
    target = _page(vault, "alpha-slug", "Alpha Title")
    duplicate = _page(vault, "beta-one", "Beta Title")
    _page(vault, "beta-two", "Beta Title")
    body = template.format(t=target, d=duplicate)
    view = _view(vault, body)
    assert view.spans, "the fixture must show at least one link by title"

    for start in range(len(view.text) + 1):
        for end in range(start, len(view.text) + 1):
            stored = view.stored_range(start, end)
            inside_a_link = any(
                shown_start < offset < shown_end
                for offset in (start, end)
                for _, _, shown_start, shown_end in view.spans
            )
            assert (stored is None) == inside_a_link
            if stored is not None:
                assert body[stored[0] : stored[1]] == _unshown(view, body, start, end)


@pytest.mark.usefixtures("public_shape")
def test_an_edit_copied_from_the_shown_page_lands_on_the_stored_bytes(vault: Path) -> None:
    target = _page(vault, "alpha-slug", "Alpha Title")
    body = f"First line.\nSee [[{target}#Part|the part]] for the cap of 30 s.\nLast line.\n"

    edited, _ = edit.apply_surgical_replace(
        body, "[[Alpha Title#Part|the part]] for the cap of 30 s", "[[Alpha Title]] for a cap of 10 s", False, vault,
    )

    assert edited == f"First line.\nSee [[{target}]] for a cap of 10 s.\nLast line.\n"


@pytest.mark.usefixtures("public_shape")
def test_a_snippet_that_cuts_a_link_in_half_is_refused(vault: Path) -> None:
    target = _page(vault, "alpha-slug", "Alpha Title")
    body = f"See [[{target}]] now.\n"

    with pytest.raises(edit.EditError) as refused:
        edit.apply_surgical_replace(body, "Title]] now", "Name]] now", False, vault)

    assert refused.value.code == "STRING_NOT_FOUND"
    assert "include each link it touches whole" in refused.value.reason


@pytest.mark.usefixtures("public_shape")
def test_shown_matches_count_and_replace_as_stored_ones_do(vault: Path) -> None:
    target = _page(vault, "alpha-slug", "Alpha Title")
    body = f"One [[{target}]].\nTwo [[{target}]].\n"

    with pytest.raises(edit.EditError) as ambiguous:
        edit.apply_surgical_replace(body, "[[Alpha Title]]", "[[Alpha Title|it]]", False, vault)
    replaced, _ = edit.apply_surgical_replace(body, "[[Alpha Title]]", "[[Alpha Title|it]]", True, vault)

    assert ambiguous.value.code == "AMBIGUOUS_MATCH"
    assert replaced == f"One [[{target}|it]].\nTwo [[{target}|it]].\n"


@pytest.mark.usefixtures("public_shape")
def test_a_snippet_in_the_stored_form_still_applies(vault: Path) -> None:
    target = _page(vault, "alpha-slug", "Alpha Title")
    body = f"See [[{target}]] now.\n"

    edited, _ = edit.apply_surgical_replace(body, f"[[{target}]] now", f"[[{target}]] later", False, vault)

    assert edited == f"See [[{target}]] later.\n"


@pytest.mark.usefixtures("public_shape")
def test_a_snippet_is_counted_where_the_agent_sees_it_not_in_a_path_it_cannot_see(vault: Path) -> None:
    target = _page(vault, "alpha-slug", "Alpha Title")
    body = f"Release Notes go here. See [[{target}]].\n"

    once, _ = edit.apply_surgical_replace(body, "Notes", "Changelog", False, vault)
    every, _ = edit.apply_surgical_replace(body, "Notes", "Changelog", True, vault)

    assert once == every == f"Release Changelog go here. See [[{target}]].\n"


@pytest.mark.usefixtures("public_shape")
def test_a_stored_snippet_is_not_matched_inside_a_path_the_agent_cannot_see(vault: Path) -> None:
    # "budget" is not on the page as shown ("Budget" is), but it is in the path of the link.
    target = _page(vault, "retry-budget", "Retry Budget")
    body = f"The Retry Budget caps load. See [[{target}]].\n"

    with pytest.raises(edit.EditError) as slipped:
        edit.apply_surgical_replace(body, "budget", "allowance", False, vault)
    with pytest.raises(edit.EditError) as in_path:
        edit.apply_surgical_replace(body, "Insights", "Ideas", True, vault)

    assert slipped.value.code == in_path.value.code == "STRING_NOT_FOUND"


def _preview(vault: Path, body: str, old_string: str) -> edit.EditValidation:
    rel = "Knowledge Base/Notes/Research/Taste/scratch-taste.md"
    page = vault / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: research-note\nproject: taste\nstatus: active\n"
        "created: 2026-05-29\nupdated: 2026-05-29\ntags: [taste]\n---\n" + body,
        encoding="utf-8",
    )
    return edit.edit(
        vault,
        path=rel,
        why="preview",
        old_string=old_string,
        new_string="x",
        validate_only=True,
        today=dt.date(2026, 6, 1),
    )


@pytest.mark.usefixtures("public_shape")
def test_a_preview_of_a_refused_snippet_shows_no_line_with_a_hidden_path(vault: Path) -> None:
    target = _page(vault, "retry-budget", "Retry Budget")

    preview = _preview(vault, f"The Retry Budget caps load. See [[{target}]].\n", "budget")

    assert preview.match_count == 0
    assert preview.matches == []


@pytest.mark.usefixtures("public_shape")
def test_a_preview_of_a_stored_snippet_shows_its_line_as_the_agent_reads_it(vault: Path) -> None:
    target = _page(vault, "retry-budget", "Retry Budget")

    preview = _preview(vault, f"See [[{target}]] now, and [[{target}]] later.\n", f"[[{target}]] now")

    assert preview.match_count == 1
    assert preview.matches == ["See [[Retry Budget]] now, and [[Retry Budget]] later."]


@pytest.mark.usefixtures("public_shape")
def test_a_snippet_that_is_also_inside_a_link_title_is_refused_as_ambiguous(vault: Path) -> None:
    target = _page(vault, "retry-budget", "Retry Budget")
    body = f"Retry the call. See [[{target}]].\n"

    with pytest.raises(edit.EditError) as refused:
        edit.apply_surgical_replace(body, "Retry", "Repeat", True, vault)

    assert refused.value.code == "AMBIGUOUS_MATCH"
    assert "inside the title of a link" in refused.value.reason


@pytest.mark.usefixtures("public_shape")
def test_a_link_the_edit_leaves_alone_keeps_its_stored_bytes(vault: Path) -> None:
    # A bare-stem link is not the canonical path form: put back as stored, it is not normalized.
    _page(vault, "alpha-slug", "Alpha Title")
    body = "Cap of 30 s, see [[alpha-slug]].\n"

    edited, _ = edit.apply_surgical_replace(
        body, "Cap of 30 s, see [[Alpha Title]]", "Cap of 10 s, see [[Alpha Title]]", False, vault
    )

    assert edited == "Cap of 10 s, see [[alpha-slug]].\n"


@pytest.mark.usefixtures("public_shape")
def test_a_snippet_that_starts_inside_a_code_span_still_keeps_its_link(vault: Path) -> None:
    # On its own the snippet's backticks pair around the link, but in the page the link is live.
    target = _page(vault, "alpha-slug", "Alpha Title")
    body = f"Flags `a` then [[{target}]] and `b`.\n"

    edited, _ = edit.apply_surgical_replace(
        body, "a` then [[Alpha Title]] and `b", "z` then [[Alpha Title]] and `b", False, vault
    )

    assert edited == f"Flags `z` then [[{target}]] and `b`.\n"


@pytest.mark.usefixtures("public_shape")
def test_a_link_the_edit_quotes_in_code_stays_as_written(vault: Path) -> None:
    target = _page(vault, "alpha-slug", "Alpha Title")
    body = f"See [[{target}]] now.\n"

    edited, _ = edit.apply_surgical_replace(
        body, "See [[Alpha Title]] now", "Write `[[Alpha Title]]` now", False, vault
    )

    assert edited == "Write `[[Alpha Title]]` now.\n"


def test_a_caller_that_never_saw_the_shown_form_edits_the_stored_bytes(vault: Path) -> None:
    target = _page(vault, "alpha-slug", "Alpha Title")
    body = f"See [[{target}]] now.\n"

    with pytest.raises(edit.EditError) as refused:
        edit.apply_surgical_replace(body, "[[Alpha Title]] now", "[[Alpha Title]] later", False, vault)

    assert refused.value.code == "STRING_NOT_FOUND"


# ---- the public tool shape, end to end ----


@pytest.fixture
def build(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    call_ledger.reset_chain_cache()
    yield lambda: server_module.build_server(require_auth=False)
    writer_lease.reset_managers_for_tests()


def _session(mcp, scenario):
    async def run():
        async with Client(mcp) as client:
            return await scenario(client)

    return asyncio.run(run())


async def _call(client, name, arguments):
    result = await client.call_tool(name, arguments, raise_on_error=False)
    body = result.structured_content
    if isinstance(body, dict) and set(body) == {"result"} and isinstance(body["result"], dict):
        body = body["result"]
    return body if body is not None else {
        "is_error": result.is_error,
        "text": " ".join(getattr(block, "text", "") for block in result.content),
    }


def _resident_resolver(vault: Path) -> None:
    """Hold the shared resolver in memory and current, as the running server's watcher keeps it.

    A read only borrows the resident resolver: it never builds or copies one.
    """
    freshness.seed(vault, "vault", [(str(p), freshness.stat_signature(p)) for p in walk_vault_md(vault)])
    find.shared_resolver(vault)


def _stored(vault: Path, title: str) -> str:
    for path in (vault / "Knowledge Base" / "Notes").rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        if f"\n# {title}\n" in text:
            return text
    return ""


def test_an_agent_reads_a_title_link_and_edits_the_span_it_copied(build, vault) -> None:
    async def scenario(client):
        await _call(client, "create_note", {
            "title": "Backoff ceiling", "type": "insight", "unlinked_reason": "A standalone test note.",
            "content": "# Backoff ceiling\n\n## Observations\n\n- [finding] Ceilings bound the wait #reliability\n",
        })
        await _call(client, "create_note", {
            "title": "Retry policy", "type": "insight",
            "content": (
                "# Retry policy\n\n## Observations\n\n"
                "- [operating constraint] Cap retries at 30 s as in [[Backoff ceiling]] #reliability\n"
                "- [finding] Untouched line #reliability\n"
            ),
        })
        _resident_resolver(vault)
        read = await _call(client, "read_page", {"page": "Retry policy"})
        raw = await _call(client, "read_page", {"page": "Retry policy", "options": {"include_raw": True}})
        span = "Cap retries at 30 s as in [[Backoff ceiling]]"
        edited = await _call(client, "edit_note", {
            "note": "Retry policy", "why": "Cap changed after review", "action": "replace_text",
            "old_text": span, "new_text": "Cap retries at 10 s as in [[Backoff ceiling]]",
        })
        return read, raw, span, edited

    read, raw, span, edited = _session(build(), scenario)

    assert span in read["body"] and "Knowledge Base/" not in read["body"], read
    # The raw file reads like the body: its frontmatter is as stored, its links are shown by title.
    assert raw["content"].startswith("---\n") and raw["content"].endswith(raw["body"]), raw
    assert span in raw["content"] and "Knowledge Base/Notes/Insights/backoff-ceiling" not in raw["content"], raw
    assert "error" not in edited, edited
    stored = _stored(vault, "Retry policy")
    assert "Cap retries at 10 s as in [[Knowledge Base/Notes/Insights/backoff-ceiling]]" in stored
    assert "- [finding] Untouched line #reliability" in stored
    assert "30 s" not in stored
    # A caller that is not on the public shape reads the stored form.
    assert "[[Knowledge Base/Notes/Insights/backoff-ceiling]]" in commands.op_read_memory(
        vault, path="Knowledge Base/Notes/Insights/retry-policy"
    )["body"]


def test_an_observation_written_with_a_title_link_stores_the_path_form(build, vault) -> None:
    async def scenario(client):
        for title in ("Backoff ceiling", "Jitter policy"):
            await _call(client, "create_note", {
                "title": title, "type": "insight", "unlinked_reason": "A standalone test note.",
                "content": f"# {title}\n\n## Observations\n\n- [finding] Original wording #reliability\n",
            })
        added = await _call(client, "add_observation", {
            "note": "Jitter policy", "category": "finding",
            "text": "Bounded by [[Backoff ceiling]], see [[Not yet written]]",
        })
        edited = await _call(client, "edit_note", {
            "note": "Jitter policy", "why": "Link the ceiling", "action": "edit_observation",
            "observation": added.get("unit_id"), "category": "finding",
            "text": "Still bounded by [[Backoff ceiling]]",
        })
        _resident_resolver(vault)
        units = await _call(client, "read_page", {"page": "Jitter policy", "part": "units"})
        return added, edited, units

    added, edited, units = _session(build(), scenario)

    assert "error" not in added and "error" not in edited, (added, edited)
    assert any("Not yet written" in warning for warning in added.get("warnings", [])), added
    stored = _stored(vault, "Jitter policy")
    assert "Still bounded by [[Knowledge Base/Notes/Insights/backoff-ceiling]]" in stored
    assert "[[Backoff ceiling]]" not in stored
    assert "Still bounded by [[Backoff ceiling]]" in str(units["units"])
