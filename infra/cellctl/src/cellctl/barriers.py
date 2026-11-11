"""automate-cloud-releases D3: upgrade barriers, and whether an attempt crosses one.

The barrier file (infra/contracts/upgrade-barriers-v1.json) lists each release
that is one-way or needs an operator step. The chart carries a copy and renders
it into cellctl's environment. Pure functions, no I/O.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# The entry fields' closed sets, which the barrier file's JSON schema fixes.
SCOPES = ("platform", "cell")
KINDS = ("one-way", "operator-step")
ONE_WAY = "one-way"
CELL = "cell"
# A release tag's shape, as the release workflow publishes it (vMAJOR.MINOR.PATCH).
_RELEASE_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

Release = tuple[int, int, int]


@dataclass(frozen=True)
class Barrier:
    id: str
    version: Release
    scope: str
    kind: str


def parse_release(tag: str | None) -> Release | None:
    """A release tag as a comparable version, or None when it is not one."""

    match = _RELEASE_RE.fullmatch(tag or "")
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def load_barriers(raw: str) -> tuple[Barrier, ...]:
    """The barrier file's entries. An entry with an unknown scope or kind, or a version
    that is not a release, fails cellctl at start: skipping it would cross it silently."""

    document = json.loads(raw)
    entries = document.get("barriers") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise ValueError("the barrier file has no barriers list")
    barriers = []
    for entry in entries:
        version = parse_release(entry.get("version")) if isinstance(entry, dict) else None
        if (version is None or entry.get("scope") not in SCOPES or entry.get("kind") not in KINDS
                or not isinstance(entry.get("id"), str)):
            raise ValueError(f"the barrier file has an unreadable entry: {entry!r}")
        barriers.append(Barrier(entry["id"], version, entry["scope"], entry["kind"]))
    return tuple(barriers)


def _one_way_cell(barriers: tuple[Barrier, ...]) -> list[Barrier]:
    return [barrier for barrier in barriers if barrier.scope == CELL and barrier.kind == ONE_WAY]


def needs_history(barriers: tuple[Barrier, ...]) -> bool:
    """Only a one-way `cell` entry makes an attempt's releases matter."""

    return bool(_one_way_cell(barriers))


def crosses_forward(previous: Release | None, target: Release | None, barriers: tuple[Barrier, ...]) -> bool:
    """A crossing attempt: a one-way `cell` entry lies between the previous image's
    release and the target's. An unmapped previous image counts as older than every
    entry, and an unmapped target as newer, so neither restores across a barrier."""

    return any(
        (previous is None or previous < barrier.version) and (target is None or target >= barrier.version)
        for barrier in _one_way_cell(barriers)
    )


def crosses_backward(previous: Release | None, target: Release | None, barriers: tuple[Barrier, ...]) -> bool:
    """A target before a one-way `cell` entry that the previous image is past. Both
    releases must be known: an unmapped previous image is past no entry, and an
    unmapped target is before none."""

    if previous is None or target is None:
        return False
    return any(previous >= barrier.version > target for barrier in _one_way_cell(barriers))


def image_digest(image: str | None) -> str | None:
    """The `sha256:` digest of an image reference, the image history's key."""

    if not image or "@" not in image:
        return None
    return image.rsplit("@", 1)[1]
