"""The plans that `read_instructions(topic="curation.propose")` teaches work in a vault that holds
their pages.

OpenSpec add-curation-relocation-protocol 5.1. Each test names the failure it catches.
"""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import Any

import pytest

from exomem import commands, curation, curation_examples
from exomem.public_surface import help_for
from exomem.vault import content_hash, parse_frontmatter

NOTE = """---
type: insight
status: active
created: 2026-04-10
updated: 2026-04-10
sources: []
tags: [team-practice]
exomem_id: {identity}
---

# {title}

## Claim

{body}
"""
SOURCE = """---
type: source
source_type: article
captured: 2026-04-02
tags: [team-practice]
---

# Source: {title}

## Capture

{body}
"""


def _taught(name: str) -> dict[str, Any]:
    reply = help_for("curation.propose", commands.PRODUCT_COMMANDS)
    return copy.deepcopy(next(example for example in reply["examples"] if example["name"] == name))


def _write(vault: Path, rel: str, body: str) -> None:
    template = SOURCE if "/Sources/" in rel else NOTE
    title = Path(rel).stem.replace("-", " ").capitalize()
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template.format(identity=uuid.uuid4(), title=title, body=body), encoding="utf-8")


def _hold_pages(vault: Path, example: dict[str, Any]) -> list[dict[str, Any]]:
    """Write every page the example names, each linking to the first page it moves, and fill in
    the hashes an agent reads before it proposes. Returns the plan's steps."""
    steps = example["plan"]["steps"]
    moved = [step["args"]["old_path"] for step in steps if step["kind"] == "move"]
    link = f"See [[{moved[0].removesuffix('.md')}]]." if moved else "No links yet."
    for rel in moved:
        _write(vault, rel, "Weekly check-ins surfaced blockers early.")
    for row in example.get("preview", {}).get("referrers", []):
        _write(vault, row["path"], link)
    for step in steps:
        if step["kind"] != "edit":
            continue
        operation = step["args"]["operation"]
        _write(vault, step["args"]["path"], operation.get("old_string", link))
        assert operation["expected_hash"] == curation_examples.HASH_PLACEHOLDER
        operation["expected_hash"] = content_hash((vault / step["args"]["path"]).read_text(encoding="utf-8"))
    return steps


@pytest.mark.parametrize("name", ["split", "move", "rehome"])
def test_proposal_accepts_each_taught_plan(vault: Path, name: str) -> None:
    """An example that teaches an argument, a field or a step order that proposal refuses."""
    example = _taught(name)
    _hold_pages(vault, example)

    proposed = curation.propose(vault, example["plan"])

    assert curation.preview(vault, run_id=proposed["run_id"])["blockers"] == []


def test_the_taught_move_preview_shows_the_outcomes_it_teaches(vault: Path) -> None:
    """Teaching that tells an agent to expect referrer outcomes that the sealed preview does not
    list, so the person approves something other than what they were shown."""
    example = _taught("move")
    _hold_pages(vault, example)

    proposed = curation.propose(vault, example["plan"])

    sealed = curation.preview(vault, run_id=proposed["run_id"])["binding_manifest"][-1]
    assert sealed["prepared"]["referrers"] == example["preview"]["referrers"]


def test_the_taught_rehome_applies_and_repoints_a_source_it_also_corrects(vault: Path) -> None:
    """A taught rehome whose final `repoint-links` refuses a Source that an earlier `edit` of the
    same plan corrected, so the example proposes but never completes."""
    example = _taught("rehome")
    steps = _hold_pages(vault, example)
    moved = {step["args"]["old_path"]: step["args"]["new_path"] for step in steps if step["kind"] == "move"}
    first_old, first_new = next(iter(moved.items()))
    corrected = [step["args"]["path"] for step in steps if step["kind"] == "edit"]
    proposed = curation.propose(vault, example["plan"])

    result = curation.apply(
        vault,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved rehome.",
    )
    while result["phase"] == "executing":
        result = curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])

    assert result["phase"] == "completed"
    for old, new in moved.items():
        assert not (vault / old).exists()
        fields, _body, _raw = parse_frontmatter((vault / new).read_text(encoding="utf-8"))
        assert fields["project"] == "project-alpha"
    for rel in corrected:
        text = (vault / rel).read_text(encoding="utf-8")
        assert parse_frontmatter(text)[0]["project"] == "project-alpha"
        assert f"[[{first_new.removesuffix('.md')}]]" in text
        assert f"[[{first_old.removesuffix('.md')}]]" not in text
