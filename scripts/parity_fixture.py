"""Identical Markdown bytes for the current-source public comparison."""
from __future__ import annotations

import hashlib
from pathlib import Path

import durable_closure_common as common

TRACKER = "active-tracker.md"
OLD_TARGET = "archived-runbook.md"
NEW_TARGET = "background.md"
OLD_LINK = "[[Archived Runbook]]"
NEW_LINK = "[[Background]]"
SUFFIX = "\n## Concurrent\n\n## Relations\n\n- supports [[Archived Runbook]]"


def normalized_body(body: str) -> str:
    """Remove opening metadata and its separator without trimming body bytes."""
    if body.startswith("---\n"):
        _metadata, separator, remainder = body[4:].partition("\n---\n")
        if separator:
            return remainder.removeprefix("\n")
    return body


def read_body_equals(payload: dict, expected: str) -> bool:
    """Compare body bytes except the products' single final-LF convention."""
    body = common._read_body(payload)
    return body is not None and normalized_body(body).removesuffix("\n") == expected.removesuffix("\n")


def tracker_body(pages: int) -> str:
    return dict(common.fixture_pages(pages))[TRACKER] + SUFFIX


def materialize(root: Path, pages: int) -> dict:
    fixture = common.materialize_fixture(root, pages=pages)
    (root / TRACKER).write_text(tracker_body(pages), encoding="utf-8")
    for row in fixture["pages"]:
        raw = (root / row["path"]).read_bytes()
        row.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    fixture["digest"] = hashlib.sha256("".join(
        f"{row['path']}:{row['sha256']}\n" for row in fixture["pages"]
    ).encode()).hexdigest()
    fixture["generator"] = "parity-markdown-v1"
    fixture["total_bytes"] = sum(row["bytes"] for row in fixture["pages"])
    return fixture
