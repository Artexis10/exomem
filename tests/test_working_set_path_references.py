"""A path or URL quoted in a turn is one opaque reference, not words.

A turn that quoted `/home/<user>/handoffs/<file>.md` was read as the words
`home`, `handoffs`, ..., and `home` named the vault's `home` project by
exact alias: an unrelated project's preferences were carried into the
packet. A rooted path (POSIX, `~`, a Windows drive, an environment
variable), a URL with or without a scheme and an scp-style remote now
contribute no word to subject evidence. A relative path that ends in a file
name, or starts `./` or `../`, contributes only its final segment, and a
`Knowledge Base/` path is one reference up to its file name even with spaces
in it. Any other slash run is prose, and a vault page path still names the
page it always named.

Invented names throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_working_set_index import _seed_structure

from exomem import commands, lexstore, working_set_index, working_set_runtime

MARIT = "Knowledge Base/Entities/People/Marit Solheim.md"
PROJECT_PAGES = {
    "home": "Knowledge Base/Notes/home-tea-preference.md",
    "records": "Knowledge Base/Notes/records-tea-preference.md",
}


def _write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project_vault(vault: Path) -> Path:
    _seed_structure(vault)
    _write(
        vault,
        "Knowledge Base/_Schema/project-keys.yaml",
        "projects:\n"
        "  home:\n    folder: Home\n    category: personal\n"
        "  records:\n    folder: Records\n    category: operations\n",
    )
    for key, rel in PROJECT_PAGES.items():
        _write(
            vault,
            rel,
            f"---\ntype: note\nstatus: active\nproject: {key}\nupdated: 2026-09-20\n---\n\n"
            f"# {key.title()} tea preference\n\n## Summary\n\n"
            f"- [preference] Green tea in the afternoon for {key} work. ^{key}-tea\n",
        )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


def _reached(packet: dict) -> set[str]:
    anchors = {str(a.get("ref") or a.get("path")) for a in packet.get("anchors") or ()}
    units = {u["provenance"]["path"] for u in packet.get("units") or ()}
    return anchors | units


@pytest.mark.parametrize(
    "turn",
    [
        "Read /srv/home/handoffs/2026-10-04-records-sync.md and pick it up.",
        "Read ~/records/home-todo.md and pick it up.",
        r"Read C:\Users\example\Records\Home\plan.txt and pick it up.",
        "Read https://example.com/home/records/index.html and pick it up.",
        "Read Knowledge Base/Records/Home/sync-notes.md and pick it up.",
        "Read ./records/home/sync-notes.md and pick it up.",
        "Read Projects/Home/plan.md and pick it up.",
        "Read ${HOME}/records/home-todo.md and pick it up.",
        "Pull git@example.com:acme/home.git and pick it up.",
        "Pull deploy@prod:/srv/home/records and pick it up.",
        "See example.com/acme/records and pick it up.",
        'Read "/srv/notes/My Home Records/plan.md" and pick it up.',
        "Read `/srv/notes/My Home Records/plan.md` and pick it up.",
        "Read **/srv/home/handoff.md** and pick it up.",
        'Read "/srv/home/handoffs and pick it up.',
        "Read `/srv/home/handoffs and pick it up.",
        "Read {/srv/home/handoff.md} and pick it up.",
        "Read notes,/srv/home/handoffs and pick it up.",
        "Read \u2018/srv/home/handoff.md\u2019 and pick it up.",
    ],
    ids=[
        "posix",
        "tilde",
        "windows",
        "url",
        "relative-directories",
        "dot-relative-directories",
        "unrooted-to-a-file",
        "environment-variable",
        "scp-style-remote",
        "scp-style-remote-dotless-host",
        "schemeless-host",
        "quoted-with-spaces",
        "backticked-with-spaces",
        "bold",
        "unclosed-quote",
        "unclosed-backtick",
        "braces",
        "after-a-comma",
        "curly-single-quotes",
    ],
)
def test_a_quoted_path_names_no_project(project_vault: Path, turn: str) -> None:
    packet = commands.op_activate_context(project_vault, turn=turn)

    reached = _reached(packet)
    assert not reached & {"project:home", "project:records", *PROJECT_PAGES.values()}, reached


def test_the_same_project_named_in_prose_still_resolves(project_vault: Path) -> None:
    packet = commands.op_activate_context(project_vault, turn="How is the Records project going?")

    assert "project:records" in {a["ref"] for a in packet["anchors"] if a["status"] == "resolved"}
    assert PROJECT_PAGES["records"] in _reached(packet)


@pytest.mark.parametrize(
    "turn",
    [
        "Compare the records/staging/prod setups.",
        "Compare the Node.js/records setups.",
    ],
    ids=["three-words", "dotted-name-then-word"],
)
def test_slash_joined_words_are_prose_not_a_path(project_vault: Path, turn: str) -> None:
    """A slash run with no root, no leading `./` or `../` and no file name at
    its end is prose, and a dotted name such as `Node.js` before a slash is not
    a host."""
    packet = commands.op_activate_context(project_vault, turn=turn)

    assert "project:records" in _reached(packet)


@pytest.mark.parametrize(
    "turn",
    [
        f"Look at `{MARIT}` please.",
        f"Look at {MARIT} please.",
        f"Look at ./{MARIT} please.",
        "Look at Marit Solheim.md please.",
    ],
    ids=["quoted", "relative", "dot-relative", "bare-file-name"],
)
def test_a_quoted_vault_page_path_still_names_that_page(project_vault: Path, turn: str) -> None:
    """The page is named and nothing else is reached: the path's folder names
    (`Knowledge Base`, `Entities`, `People`) are not words of the turn."""
    packet = commands.op_activate_context(project_vault, turn=turn)

    assert [a["path"] for a in packet["anchors"]] == [MARIT], packet["anchors"]
    assert packet["anchors"][0]["status"] == "resolved"
