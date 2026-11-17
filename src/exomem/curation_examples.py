"""Worked curation plans that `read_instructions(topic="curation.propose")` returns.

Each plan passes proposal in a vault that holds the pages it names, once every `expected_hash`
placeholder carries that page's current `content_hash` (tests/test_curation_examples.py).
"""

from __future__ import annotations

from typing import Any

from . import move_file
from .kbdir import managed_join

#: Stands where a plan needs the reviewed hash of a page that the agent reads first.
HASH_PLACEHOLDER = "<content_hash of this page from read_page>"

_INSIGHTS = "Notes/Insights"
_SOURCES = "Sources/Articles"
_CHECK_INS = "weekly-check-ins-surface-blockers-early.md"
_DECISION_LOG = "a-decision-log-speeds-onboarding.md"
_RITUALS = "team-rituals-that-paid-off.md"
_INTERVIEW_SOURCE = "2026-04-02-interview-with-jane-example.md"
_RETRO_SOURCE = "2026-04-09-project-alpha-retrospective.md"


def _insight(name: str) -> str:
    return managed_join(f"{_INSIGHTS}/{name}")


def _source(name: str) -> str:
    return managed_join(f"{_SOURCES}/{name}")


def _move(step_id: str, name: str, **fields: str) -> dict[str, Any]:
    args: dict[str, Any] = {
        "old_path": _insight(name),
        "new_path": managed_join(f"{_INSIGHTS}/project-alpha/{name}"),
    }
    if fields:
        args["fields"] = fields
    return {"step_id": step_id, "kind": "move", "args": args}


def _child(step_id: str, title: str, slug: str, project: str, claim: str) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "kind": "create-note",
        "args": {
            "title": title,
            "slug": slug,
            "note_type": "insight",
            "project": project,
            "content": f"## Claim\n\n{claim}\n\n## Origin\n\nSplit from [[team-rituals-that-paid-off]].\n",
        },
    }


def _project_fix(step_id: str, rel: str) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "kind": "edit",
        "args": {
            "path": rel,
            "why": "The capture belongs to Project Alpha.",
            "operation": {
                "kind": "patch_frontmatter",
                "field": "project",
                "value": "project-alpha",
                "expected_hash": HASH_PLACEHOLDER,
            },
        },
    }


_REPOINT = {"step_id": "repoint", "kind": "repoint-links", "args": {}}


def examples() -> list[dict[str, Any]]:
    """The split, move and rehome plans, each with when to use it and what its preview shows."""
    return [
        {
            "name": "split",
            "when": (
                "A note answers two questions. Each child note declares its own `project`; "
                "the `edit` keeps the remainder in the original note and links the children."
            ),
            "plan": {
                "version": 1,
                "title": "Split the team rituals note by project",
                "steps": [
                    _child(
                        "child-alpha",
                        "Weekly check-ins surface blockers early",
                        "weekly-check-ins-surface-blockers-early",
                        "project-alpha",
                        "Weekly check-ins with Jane Example surfaced blockers a week before monthly reviews did.",
                    ),
                    _child(
                        "child-beta",
                        "A decision log speeds onboarding",
                        "a-decision-log-speeds-onboarding",
                        "project-beta",
                        "A written decision log let new members of Project Beta catch up without meetings.",
                    ),
                    {
                        "step_id": "remainder",
                        "kind": "edit",
                        "args": {
                            "path": _insight(_RITUALS),
                            "why": "Each question now has its own note.",
                            "operation": {
                                "kind": "replace_string",
                                "old_string": (
                                    "Weekly check-ins surfaced blockers early, and a decision log "
                                    "sped up onboarding."
                                ),
                                "new_string": (
                                    "See [[weekly-check-ins-surface-blockers-early]] and "
                                    "[[a-decision-log-speeds-onboarding]]."
                                ),
                                "expected_hash": HASH_PLACEHOLDER,
                            },
                        },
                    },
                ],
            },
        },
        {
            "name": "move",
            "when": (
                "One note belongs in another folder. The move records the old path in the note's "
                "`moved_from`, so old links keep resolving; `repoint-links` then rewrites them. "
                "Preview lists one outcome per referrer: show those to the person before apply."
            ),
            "plan": {
                "version": 1,
                "title": "Move the check-ins note into its project folder",
                "steps": [_move("move", _CHECK_INS), _REPOINT],
            },
            "preview": {
                "referrers": [
                    {"path": _insight(_RITUALS), "outcome": move_file.REWRITTEN},
                    {"path": _source(_INTERVIEW_SOURCE), "outcome": move_file.REPOINTED},
                ],
            },
        },
        {
            "name": "rehome",
            "when": (
                "Several notes move to their project. Each `move` sets `project` through its `fields` "
                "patch, which takes pointer fields only. Each `edit` corrects the `project` of a "
                "Source that belongs with them; only that field changes. One `repoint-links` step "
                "comes last and rewrites every link to the moved notes, Sources included."
            ),
            "plan": {
                "version": 1,
                "title": "Rehome the Project Alpha notes",
                "steps": [
                    _move("move-check-ins", _CHECK_INS, project="project-alpha"),
                    _move("move-decision-log", _DECISION_LOG, project="project-alpha"),
                    _project_fix("fix-interview", _source(_INTERVIEW_SOURCE)),
                    _project_fix("fix-retrospective", _source(_RETRO_SOURCE)),
                    _REPOINT,
                ],
            },
        },
    ]
