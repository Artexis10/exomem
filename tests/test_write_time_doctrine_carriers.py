"""The `write-time-identity-candidates` doctrine, in the carriers that do not
share the compact-bootstrap byte ceiling: the shipped skill scaffold, the
`remember`/`replace_memory` tool-schema guidance, and the six capture texts
that today forbid the exact first-mention case this change asks for.

`tests/test_bootstrap_activation_carrier.py` and `tests/test_prominence*.py`
own the fourth carrier -- the compact bootstrap contract at `balanced` and
`maximal` -- which IS byte-budgeted and is tracked separately.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import exomem
from exomem import commands, semantic_authoring

REPO_ROOT = pathlib.Path(exomem.__file__).parents[2]
SRC_ROOT = pathlib.Path(exomem.__file__).parent

SCAFFOLD_SKILL = SRC_ROOT / "_scaffold" / "_Schema" / "SKILL.md"
PLUGIN_SKILL = REPO_ROOT / "plugins" / "claude-code" / "skills" / "exomem" / "SKILL.md"

CAPTURE_NUDGE_SOURCE = SRC_ROOT / "_hooks" / "exomem_capture_nudge.py"
CAPTURE_NUDGE_PLUGIN = REPO_ROOT / "plugins" / "claude-code" / "hooks" / "exomem_capture_nudge.py"

CAPTURE_SKILL_SOURCE = (
    SRC_ROOT / "_scaffold" / "_Schema" / "workflow-skills" / "exomem-capture" / "SKILL.md"
)
CAPTURE_SKILL_PLUGIN = (
    REPO_ROOT / "plugins" / "claude-code" / "skills" / "exomem-capture" / "SKILL.md"
)

OPERATIONS_SOURCE = SRC_ROOT / "_scaffold" / "_Schema" / "references" / "operations.md"
OPERATIONS_PLUGIN = (
    REPO_ROOT / "plugins" / "claude-code" / "skills" / "exomem" / "references" / "operations.md"
)

SIX_CAPTURE_TEXT_PAIRS = (
    (CAPTURE_NUDGE_SOURCE, CAPTURE_NUDGE_PLUGIN),
    (CAPTURE_SKILL_SOURCE, CAPTURE_SKILL_PLUGIN),
    (OPERATIONS_SOURCE, OPERATIONS_PLUGIN),
)

#: The two phrases the six capture texts forbade the first-mention case with.
OLD_PHRASES = ("stable recurring identity", "stable, recurring, central")


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _param(command, name: str):
    return next(param for param in command.params if param.name == name)


# --------------------------------------------------------------------- 3.2, scaffold


def _before_writing_section(text: str) -> str:
    lines = text.splitlines()
    start = lines.index("## Before writing")
    for offset, line in enumerate(lines[start + 1 :], start=start + 1):
        if line.startswith("## "):
            return "\n".join(lines[start + 1 : offset])
    return "\n".join(lines[start + 1 :])


@pytest.mark.parametrize("skill", [SCAFFOLD_SKILL, PLUGIN_SKILL])
def test_the_scaffold_teaches_linking_named_identities(skill: pathlib.Path) -> None:
    section = _before_writing_section(skill.read_text(encoding="utf-8"))

    assert "Wikilink" in section
    assert "whether or not a page exists yet" in section
    assert "entity_candidate" in section
    assert "in the same turn" in section


def test_the_two_shipped_skill_copies_stay_byte_identical() -> None:
    assert SCAFFOLD_SKILL.read_bytes() == PLUGIN_SKILL.read_bytes()


# ------------------------------------------------------------- 3.3, the write surface


def test_remember_and_replace_memory_content_teaches_linking() -> None:
    for name in ("remember", "replace_memory"):
        content_help = _param(_command(name), "content").help

        assert "Wikilink" in content_help
        assert "whether or not a page exists yet" in content_help


def test_observe_memory_and_edit_memory_are_untouched() -> None:
    """The requirement scopes to `remember`/`replace_memory` only (D2): the two
    whole-page writers a note's full set of named identities passes through."""
    observe_help = _param(_command("observe_memory"), "content").help
    edit_help = _param(_command("edit_memory"), "operation").help

    assert semantic_authoring.LINK_NAMED_IDENTITIES_GUIDANCE not in observe_help
    assert semantic_authoring.LINK_NAMED_IDENTITIES_GUIDANCE not in edit_help


# ------------------------------------------------------------------- 3.4, capture texts


@pytest.mark.parametrize(("source", "plugin"), SIX_CAPTURE_TEXT_PAIRS)
def test_each_capture_text_pair_stays_byte_identical(
    source: pathlib.Path, plugin: pathlib.Path
) -> None:
    assert source.read_bytes() == plugin.read_bytes()


def _reminder_constant(path: pathlib.Path) -> str:
    """The evaluated `REMINDER` string a capture-nudge hook script assigns.

    Read as source (never imported: these run as bare files under a client's
    own interpreter, per `CONTRIBUTING.md`) and evaluated with `ast`, so an
    adjacent-literal line wrap in the source is not mistaken for a gap in the
    rendered prose the agent actually reads.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "REMINDER"
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"no REMINDER assignment found in {path}")


def _rendered_prose(path: pathlib.Path) -> str:
    """This file's text as an agent reads it: a hook's evaluated `REMINDER`
    constant, or a Markdown file's own text with hard line wraps collapsed."""
    if path.suffix == ".py":
        return _reminder_constant(path)
    return " ".join(path.read_text(encoding="utf-8").split())


@pytest.mark.parametrize(
    "path",
    [
        CAPTURE_NUDGE_SOURCE,
        CAPTURE_NUDGE_PLUGIN,
        CAPTURE_SKILL_SOURCE,
        CAPTURE_SKILL_PLUGIN,
        OPERATIONS_SOURCE,
        OPERATIONS_PLUGIN,
    ],
)
def test_each_capture_text_now_permits_a_first_mention_entity(path: pathlib.Path) -> None:
    prose = _rendered_prose(path)

    assert "stable, and central or recurring" in prose
    for phrase in OLD_PHRASES:
        assert phrase not in prose


def test_no_shipped_source_or_plugin_file_still_says_recurrence_is_required() -> None:
    """Repository-wide grep for the two forbidden phrases (task 3.4), scoped to
    the shipped tree rather than the OpenSpec change that documents the
    rewording historically."""
    offenders: list[str] = []
    for root in (SRC_ROOT, REPO_ROOT / "plugins"):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".md"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for phrase in OLD_PHRASES:
                if phrase in text:
                    offenders.append(f"{path}: {phrase!r}")

    assert offenders == []
