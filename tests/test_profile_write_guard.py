"""A governed write changes a profile page only from a preview of the same resulting content.

The workflow under test is an agent editing the owner's profile: it previews the change, the owner sees the
resulting lines, and only then does the commit land. Each check names what a one-call rewrite would break.
The files edited directly in the vault are not governed writes and are not covered.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from exomem import commands, semantic_writes, server
from exomem import vault as vault_module

KB = "Knowledge Base"
PROFILE = f"{KB}/Notes/Profiles/games.md"
NOTE = f"{KB}/Notes/Insights/games-log.md"
CHEAP = "- [rule] Tactical cooperative play only. ^coop\n"
RELATIONS = "\n## Relations\n"


def _page(kind: str, name: str, line: str) -> str:
    return f"---\ntitle: {name}\ntype: {kind}\nstatus: draft\ntopics: [games]\n---\n\n# {name}\n\n{line}{RELATIONS}"


@pytest.fixture
def pages(vault: Path, owner_scope: None) -> Path:
    for path, kind in ((PROFILE, "profile"), (NOTE, "insight")):
        (vault / path).parent.mkdir(parents=True, exist_ok=True)
        (vault / path).write_text(_page(kind, Path(path).stem, CHEAP), encoding="utf-8")
    return vault


def _edit(vault: Path, path: str, **operation: object) -> dict:
    return commands.op_edit_memory(
        vault, path=path, why="change a rule",
        operation={"kind": "replace_string", **operation},
    )


def _bytes(vault: Path, path: str) -> bytes:
    return (vault / path).read_bytes()


def test_an_unreviewed_profile_edit_is_refused_and_leaves_the_page(pages: Path) -> None:
    before = _bytes(pages, PROFILE)

    with pytest.raises(ValueError, match="PROFILE_PREVIEW_REQUIRED.*preview"):
        _edit(pages, PROFILE, old_string="Tactical", new_string="Slow")

    assert _bytes(pages, PROFILE) == before


def test_a_reviewed_profile_edit_commits_exactly_the_previewed_content(pages: Path) -> None:
    change = {"old_string": "Tactical", "new_string": "Slow"}
    preview = _edit(pages, PROFILE, validate_only=True, **change)
    token = preview["semantic"]["transition_token"]

    _edit(pages, PROFILE, transition_token=token, **change)

    assert b"- [rule] Slow cooperative play only." in _bytes(pages, PROFILE)
    assert preview["semantic"]["after_hash"] == vault_module.content_hash(_bytes(pages, PROFILE).decode())


def test_a_review_of_another_change_is_refused_and_leaves_the_page(pages: Path) -> None:
    token = _edit(pages, PROFILE, validate_only=True, old_string="Tactical", new_string="Slow")["semantic"]["transition_token"]
    before = _bytes(pages, PROFILE)

    with pytest.raises(ValueError, match="LIFECYCLE_TRANSITION_MISMATCH"):
        _edit(pages, PROFILE, transition_token=token, old_string="Tactical", new_string="Reckless")

    assert _bytes(pages, PROFILE) == before


def test_an_ordinary_note_edits_without_a_preview(pages: Path) -> None:
    _edit(pages, NOTE, old_string="Tactical", new_string="Slow")

    assert b"- [rule] Slow cooperative play only." in _bytes(pages, NOTE)


def test_an_added_line_needs_the_preview_of_that_line(pages: Path) -> None:
    line = {"operation": "add", "category": "rule", "content": "Prefer short sessions."}
    with pytest.raises(ValueError, match="PROFILE_PREVIEW_REQUIRED"):
        commands.op_observe_memory(pages, PROFILE, **line)
    preview = commands.op_observe_memory(pages, PROFILE, **{**line, "operation": "validate"})

    commands.op_observe_memory(pages, PROFILE, transition_token=preview["semantic"]["transition_token"], **line)

    assert b"Prefer short sessions." in _bytes(pages, PROFILE)


def test_a_new_profile_needs_its_preview_hash(vault: Path, owner_scope: None) -> None:
    page = {"content": CHEAP, "title": "Games", "note_type": "profile", "status": "draft", "topics": ["games"]}
    with pytest.raises(ValueError, match="PROFILE_PREVIEW_REQUIRED"):
        commands.op_remember(vault, **page)
    # The writer names a new page from its title, so the test finds it in the profile area.
    assert not list((vault / KB / "Notes" / "Profiles").glob("*.md"))
    preview = commands.op_remember(vault, **page, validate_only=True)

    commands.op_remember(
        vault, **page, draft_id=preview["draft_id"], draft_hash=preview["draft_hash"], draft_token=preview["draft_token"],
    )

    (created,) = (vault / KB / "Notes" / "Profiles").glob("*.md")
    assert created.read_text(encoding="utf-8").endswith(CHEAP)


def test_a_profile_is_not_replaced_by_another_page_even_with_a_preview(pages: Path) -> None:
    # A successor page would not keep the profile's domain and would become the global profile.
    before = _bytes(pages, PROFILE)
    page = {"content": "Games now live in the games log.", "note_type": "insight", "title": "Games log two"}

    with pytest.raises(ValueError, match="PROFILE_SUPERSESSION_REFUSED.*edit_note"):
        commands.op_replace(
            pages, old_path=PROFILE, reason="fold the profile into a note", validate_only=True, **page,
        )

    assert _bytes(pages, PROFILE) == before


def test_a_profile_file_created_through_the_file_writer_needs_its_preview_hash(vault: Path, owner_scope: None) -> None:
    page = {"path": PROFILE, "content": f"# Games\n\n{CHEAP}", "frontmatter": {"type": "profile", "status": "draft", "topics": ["games"]}}
    with pytest.raises(ValueError, match="PROFILE_PREVIEW_REQUIRED"):
        commands.op_create_file(vault, **page)
    assert not (vault / PROFILE).exists()
    preview = commands.op_create_file(vault, **page, validate_only=True)

    commands.op_create_file(
        vault, **page, draft_id=preview["draft_id"], draft_hash=preview["draft_hash"], draft_token=preview["draft_token"],
    )

    assert (vault / PROFILE).is_file()


def _public(mcp, tool: str, **arguments: object) -> dict:
    """One call to a typed tool, as a connected agent sends it."""
    result = asyncio.run(mcp.call_tool(tool, arguments, run_middleware=True))
    return result.structured_content


@pytest.fixture
def mcp(pages: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "load_dotenv", lambda *_a, **_k: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(pages))
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    return server.build_server(require_auth=False)


def test_a_connected_agent_edits_a_profile_with_the_review_its_preview_returned(pages: Path, mcp) -> None:
    edit = {"note": PROFILE, "why": "change a rule", "action": "replace_text", "old_text": "Tactical", "new_text": "Slow"}
    assert _public(mcp, "edit_note", **edit)["error"]["code"] == "PROFILE_PREVIEW_REQUIRED"
    preview = _public(mcp, "edit_note", **edit, preview=True)["semantic"]
    # The proof alone does not show the owner agreed; the preview must say so where the agent reads the token.
    assert preview["next_action"] == semantic_writes.PROFILE_PREVIEW_NEXT_ACTION
    token = preview["transition_token"]

    _public(mcp, "edit_note", **edit, review={"transition_token": token})

    assert b"- [rule] Slow cooperative play only." in _bytes(pages, PROFILE)


def test_a_connected_agent_adds_a_profile_line_with_the_review_its_preview_returned(pages: Path, mcp) -> None:
    line = {"note": PROFILE, "category": "rule", "text": "Prefer short sessions."}
    assert _public(mcp, "add_observation", **line)["error"]["code"] == "PROFILE_PREVIEW_REQUIRED"
    token = _public(mcp, "add_observation", **line, preview=True)["semantic"]["transition_token"]

    _public(mcp, "add_observation", **line, review={"transition_token": token})

    assert b"Prefer short sessions." in _bytes(pages, PROFILE)


def test_a_connected_agent_creates_a_profile_with_the_review_its_preview_returned(vault: Path, owner_scope: None, monkeypatch) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *_a, **_k: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    mcp = server.build_server(require_auth=False)
    page = {
        "title": "Games", "content": CHEAP, "type": "profile", "fields": {"topics": ["games"]},
        "unlinked_reason": "a new profile has nothing to link yet",
    }
    assert _public(mcp, "create_note", **page)["error"]["code"] == "PROFILE_PREVIEW_REQUIRED"
    preview = _public(mcp, "create_note", **page, preview=True)
    assert preview["next_action"] == semantic_writes.PROFILE_PREVIEW_NEXT_ACTION

    _public(mcp, "create_note", **page, review={key: preview[key] for key in ("draft_id", "draft_hash", "draft_token")})

    (created,) = (vault / KB / "Notes" / "Profiles").glob("*.md")
    assert created.read_text(encoding="utf-8").endswith(CHEAP)


def test_a_connected_agent_cannot_promote_a_note_to_a_profile_by_superseding_it(pages: Path, mcp) -> None:
    # The successor would carry no reviewed domain; a new profile starts from create_note.
    before = _bytes(pages, NOTE)
    page = {"note": NOTE, "title": "Games", "content": CHEAP, "type": "profile", "why": "make it the profile"}

    refusal = _public(mcp, "supersede_note", **page, preview=True)["error"]

    assert refusal["code"] == "PROFILE_SUPERSESSION_REFUSED"
    assert _bytes(pages, NOTE) == before
