"""How an f38 run sits on disk, and which of its windows count.

``<run>/window-<n>/<project>/`` holds one project played by every requested arm in lockstep, and
``state.json`` there says how it ended. ``<run>/window-<n>/history/<project>/<probe>/probe.json`` holds
a solvable-gate answer. A window belongs to one project: a project that a harness fault invalidated
replays alone in a new window number.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

#: A project's decision reads two windows of it.
WINDOWS_PER_PROJECT = 2
#: The two windows of a project start at least this far apart, because provider-side behaviour moves within hours.
MIN_WINDOW_GAP_HOURS = 6.0
#: Where the version probe of a window keeps its state, beside the projects.
PRODUCT_CHECK = "product-check"
#: Folders of a window that are not projects.
NOT_PROJECTS = ("history", "unfinished", PRODUCT_CHECK)


def project_states(run_dir: Path) -> list[dict]:
    """The ``state.json`` of every project window that ended, oldest start first within each project."""
    states = []
    for path in run_dir.glob("window-*/*/state.json"):
        if path.parent.name not in NOT_PROJECTS:
            states.append({**json.loads(path.read_text()), "dir": path.parent})
    return sorted(states, key=lambda s: (s["project"], s["started_at"], s["window"]))


def unfinished_windows(run_dir: Path) -> list[dict]:
    """The project folders that started and have no ``state.json``: a stop interrupted them and the next call replays them."""
    found = []
    for folder in sorted(run_dir.glob("window-*/*")):
        if (
            folder.is_dir()
            and folder.name not in NOT_PROJECTS
            and not (folder / "state.json").exists()
        ):
            found.append(
                {"window": int(folder.parent.name.removeprefix("window-")), "project": folder.name}
            )
    return found


def counted_windows(states: list[dict]) -> dict[str, list[dict]]:
    """Per project, the first ``WINDOWS_PER_PROJECT`` valid windows by start time: the ones a report scores."""
    counted: dict[str, list[dict]] = {}
    for state in states:
        if (
            state["status"] == "complete"
            and len(counted.setdefault(state["project"], [])) < WINDOWS_PER_PROJECT
        ):
            counted[state["project"]].append(state)
    return {project: windows for project, windows in counted.items() if windows}


def hours_apart(first: dict, second: dict) -> float:
    start = [datetime.fromisoformat(s["started_at"]) for s in (first, second)]
    return abs((start[1] - start[0]).total_seconds()) / 3600


def history_records(run_dir: Path) -> list[tuple[int, str, dict]]:
    """``(window, project, probe record)`` of every solvable-gate answer in the run."""
    found = []
    for path in sorted(run_dir.glob("window-*/history/*/*/probe.json")):
        window = int(path.parents[3].name.removeprefix("window-"))
        found.append((window, path.parents[1].name, json.loads(path.read_text())))
    return found
