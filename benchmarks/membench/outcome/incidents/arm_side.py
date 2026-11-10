"""The half of an incident session that runs as the arm's own product code.

The runner starts this file with the arm's ``PYTHONPATH``, so the arm's own writer, guidance text,
frontmatter parser and status registry seed the vault and read the seeded note back. The runner
never parses a note itself.

    python arm_side.py seed VAULT WORK   # seed JSON on stdin; places WORK/CLAUDE.md; prints the note
    python arm_side.py probe VAULT PATH  # prints the note at the vault-relative PATH
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def probe(vault: Path, relative: str) -> dict:
    """The note's body and lifecycle class, as the arm's product code reads them.

    ``lifecycle_class`` is None when the arm's status registry cannot classify the label.
    """
    from exomem import lifecycle_statuses
    from exomem.vault import parse_frontmatter

    page = vault / relative
    if not page.exists():
        return {"path": relative, "exists": False, "body": None, "lifecycle_class": None}
    frontmatter, body, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
    classification = lifecycle_statuses.Basis(vault).classify(
        frontmatter.get("status"), path=relative, frontmatter=frontmatter
    )
    try:
        lifecycle = classification.require()
    except lifecycle_statuses.ClassificationUnavailable:
        lifecycle = None
    return {"path": relative, "exists": True, "body": body, "lifecycle_class": lifecycle}


def seed(vault: Path, work: Path, note: dict) -> dict:
    """Write the arm's learning instruction into ``work/CLAUDE.md`` and the case's note into the vault."""
    from exomem import commands
    from exomem.client_config import place_learning_instruction
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.prominence import LEARNING_INSTRUCTION

    place_learning_instruction(work / "CLAUDE.md", LEARNING_INSTRUCTION)
    with request_scope(owner_principal(surface="mcp")):
        written = commands.op_note(
            vault, title=note["title"], note_type=note["note_type"], content=note["content"]
        )
    return probe(vault, written["path"])


if __name__ == "__main__":
    command, vault_arg, third = sys.argv[1:4]
    if command == "seed":
        print(json.dumps(seed(Path(vault_arg), Path(third), json.load(sys.stdin))))
    else:
        print(json.dumps(probe(Path(vault_arg), third)))
