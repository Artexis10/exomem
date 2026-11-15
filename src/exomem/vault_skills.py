"""What an Agent Skill is in the vault, in one place.

The context catalogue, task-method projection, page reads and the session block
(`skills_context`) all recognise a skill here, by the published Agent Skills format alone.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, NamedTuple

from . import vault

# The skill page's file name and frontmatter keys are fixed by the published Agent Skills format.
SKILL_FILE = "SKILL.md"

# The published Agent Skills format limits a description to 1,024 characters; the catalogue keeps that much of it,
# so the vault's summary bound (`budgets.skill_summary_max_chars`, at most 1,000) never meets the stored text first.
DESCRIPTION_MAX_CHARS = 1024
# A client lists tens of native skills; these bounds keep a hostile or broken request cheap.
NATIVE_SKILLS_MAX = 200
NATIVE_SKILL_NAME_MAX_CHARS = 128

# A sentence ends at terminal punctuation before whitespace: a format rule, not a reading of the words.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


class Skill(NamedTuple):
    name: str
    description: str


def enabled() -> bool:
    return os.environ.get("EXOMEM_ACTIVATION_SKILLS", "on").strip().casefold() != "off"


def recognise(rel_path: str, frontmatter: Mapping[str, Any] | None) -> Skill | None:
    """The skill a page is, or None.

    A skill is a page named `SKILL.md` whose frontmatter has a non-empty string `name` and `description`,
    anywhere in managed knowledge except Sources and Evidence, where captured text is not an instruction.
    """
    if rel_path.rpartition("/")[2] != SKILL_FILE or vault.in_append_only_tree(rel_path) is not None:
        return None
    if not isinstance(frontmatter, Mapping):
        return None
    name, description = frontmatter.get("name"), frontmatter.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        return None
    from .governance import egress  # deferred: the guard's package loads the context catalogue

    # A name is served as one line without wikilinks, so whether a linked page exists changes no byte of it.
    name, description = " ".join(egress.without_wikilinks(name).split()), description.strip()
    return Skill(name, description) if name and description else None


def summary(description: str, limit: int) -> str:
    """The description's first sentence, cut at a whole word to `limit` characters and marked when cut."""
    text = _SENTENCE_END.split(" ".join(description.split()), maxsplit=1)[0]
    if len(text) <= limit:
        return text
    room = limit - 1  # the cut marker takes one character
    cut = text.rfind(" ", 0, room + 1)
    return text[: cut if cut > 0 else room].rstrip() + "…"


def bounded_native_names(names: object) -> frozenset[str]:
    """The skill names a caller says it loads natively: strings only, within the request bounds, the rest ignored."""
    if not isinstance(names, (list, tuple)):
        return frozenset()
    kept = (name.strip() for name in names[:NATIVE_SKILLS_MAX] if isinstance(name, str))
    return frozenset(name for name in kept if name and len(name) <= NATIVE_SKILL_NAME_MAX_CHARS)


def item_line(item: Mapping[str, Any]) -> str:
    text = f"- {item['name']}: {item['summary']}" if item.get("summary") else f"- {item['name']}"
    return f"{text} [{item['ref']}]"


def chars(block: Mapping[str, Any]) -> int:
    lines = [item_line(item) for item in block.get("items", ())]
    return len("\n".join(lines)) + (1 if lines else 0)
