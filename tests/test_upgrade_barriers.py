"""The upgrade barrier file's CI check (automate-cloud-releases D3, task 1.10).

The cloud-deploy workflow stops on each uncrossed entry, and cellctl reads the
chart's copy to hold a failed attempt across a one-way `cell` entry. An entry
the schema rejects would be skipped by both, and an entry whose runbook section
does not exist leaves the person who receives the alert with no step to follow.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
BARRIERS = ROOT / "infra/contracts/upgrade-barriers-v1.json"
SCHEMA = ROOT / "infra/contracts/upgrade-barriers-v1.schema.json"
# A runbook edit can remove a section an entry names, so a docs-only change runs this check too.
pytestmark = pytest.mark.reads_repository_docs


def _anchors(markdown: str) -> set[str]:
    """GitHub's heading anchors: lower case, punctuation dropped, spaces as hyphens."""

    anchors = set()
    for line in markdown.splitlines():
        match = re.match(r"#{1,6}\s+(.*)", line)
        if match:
            anchors.add(re.sub(r"[^\w\- ]", "", match[1].strip().lower()).replace(" ", "-"))
    return anchors


def barrier_errors(document: object) -> list[str]:
    errors = [error.message for error in jsonschema.Draft202012Validator(
        json.loads(SCHEMA.read_text(encoding="utf-8"))).iter_errors(document)]
    if errors:
        return errors
    ids = [entry["id"] for entry in document["barriers"]]
    errors += [f"duplicate id {entry_id}" for entry_id in sorted({i for i in ids if ids.count(i) > 1})]
    for entry in document["barriers"]:
        path, anchor = entry["runbook"].split("#", 1)
        runbook = ROOT / path
        if not runbook.is_file() or anchor not in _anchors(runbook.read_text(encoding="utf-8")):
            errors.append(f"{entry['id']}: runbook section {entry['runbook']} does not exist")
    return errors


def _document() -> dict:
    return json.loads(BARRIERS.read_text(encoding="utf-8"))


def test_the_barrier_file_passes_its_check() -> None:
    assert barrier_errors(_document()) == []


def test_an_entry_whose_runbook_section_does_not_exist_fails() -> None:
    # Catches a barrier with no step that a person can follow.
    document = copy.deepcopy(_document())
    document["barriers"][0]["runbook"] = "docs/runbooks/cloud-platform-deploy.md#a-section-nobody-wrote"

    assert any("does not exist" in error for error in barrier_errors(document))


@pytest.mark.parametrize(("field", "value"), [("scope", "fleet"), ("kind", "reversible")])
def test_an_unknown_scope_or_kind_fails(field: str, value: str) -> None:
    # Catches an entry that the workflow and cellctl would skip.
    document = copy.deepcopy(_document())
    document["barriers"][0][field] = value

    assert barrier_errors(document)
