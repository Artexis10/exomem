"""The f38 case set: its split, its digests and the checks that run before any session starts.

The projects are written by an independent lane and stay in evaluator custody under the run root
(``cases/epistemic/``): ``project-<x>.json`` files and ``SPLIT.json``, which names the dev and
held-out projects and the SHA-256 of every project file. A run refuses a project whose file differs
from that digest, whose structure is wrong, or whose probe holds its own answer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..workspace import sha256_of

#: Where a project file sits under the run root.
CASES_DIR = ("cases", "epistemic")
#: The dimension label of a probe that asks for a fact that never changes (a bench label, not an epistemic dimension).
STABLE = "stable"
#: A key string of this many characters or more is checked as a substring; a shorter one as a whole token.
MIN_SUBSTRING_CHARS = 4
#: Top-level workspace names the harness owns: a scripted file there would collide with an arm's memory or instruction.
RESERVED_NAMES = ("memory", "CLAUDE.md", ".claude")


def refused(message: str) -> SystemExit:
    return SystemExit(f"refused: {message}")


def cases_dir(root: Path) -> Path:
    return root.joinpath(*CASES_DIR)


def read_split(root: Path) -> dict:
    """``SPLIT.json``: the dev and held-out project names and each project file's digest."""
    path = cases_dir(root) / "SPLIT.json"
    if not path.is_file():
        raise refused(f"no epistemic case set: {path} is missing")
    split = json.loads(path.read_text())
    missing = [key for key in ("dev", "heldout", "case_sha256") if key not in split]
    if missing:
        raise refused(f"{path} has no {', '.join(missing)}")
    return split


def is_stable(probe: dict) -> bool:
    return probe["dimension"] == STABLE


def unsafe_paths(files: dict) -> list[str]:
    """The scripted file paths that leave the workspace or collide with a name the harness owns."""
    bad = []
    for name in files:
        parts = Path(name).parts
        if Path(name).is_absolute() or ".." in parts or not parts or parts[0] in RESERVED_NAMES:
            bad.append(name)
    return bad


def structure_problems(project: dict) -> list[str]:
    """Everything wrong with a project's shape, each problem naming what to fix."""
    problems: list[str] = []
    sessions = project.get("sessions")
    probes = project.get("probes")
    if not isinstance(sessions, list) or not sessions or not isinstance(probes, list):
        return ["it needs a non-empty `sessions` list and a `probes` list"]
    numbers = [s.get("session") for s in sessions]
    if numbers != sorted(set(numbers)):
        problems.append("sessions must be unique and in ascending order")
    for s in sessions:
        if not isinstance(s.get("task"), str) or not isinstance(s.get("files"), dict):
            problems.append(
                f"session {s.get('session')} needs a `task` string and a `files` object"
            )
        elif bad := unsafe_paths(s["files"]):
            problems.append(f"session {s['session']} has unsafe or reserved file paths: {bad}")
    seen: set[str] = set()
    for p in probes:
        name = p.get("probe_id")
        key = p.get("key")
        if name in seen or not isinstance(name, str):
            problems.append(f"probe id {name!r} is missing or repeated")
        seen.add(name)
        if p.get("session") not in numbers:
            problems.append(f"probe {name} names session {p.get('session')}, which does not exist")
        if not isinstance(p.get("task"), str) or not isinstance(p.get("files"), dict):
            problems.append(f"probe {name} needs a `task` string and a `files` object")
        elif bad := unsafe_paths(p["files"]):
            problems.append(f"probe {name} has unsafe or reserved file paths: {bad}")
        if not isinstance(key, dict) or not isinstance(key.get("value"), str):
            problems.append(f"probe {name} needs a `key` with a `value` string")
        elif not all(isinstance(key.get(f), list) for f in ("accept", "rubric", "behaviours")):
            problems.append(
                f"probe {name}: key.accept, key.rubric and key.behaviours must be lists"
            )
        # A probe that is neither stable nor tied to a change would silently drop out of every statistic.
        if (p.get("change_id") is None) != (p.get("dimension") == STABLE):
            problems.append(
                f"probe {name}: a stable probe has no change_id, and a change probe has one"
            )
    return problems


def key_strings(key: dict) -> list[tuple[str, str]]:
    """``(field, string)`` for the key's value, each accepted phrasing and its trap, as authored."""
    found = [("key.value", key["value"])]
    found += [(f"key.accept[{i}]", text) for i, text in enumerate(key["accept"])]
    if key.get("trap"):
        found.append(("key.trap", key["trap"]))
    return [(field, text.strip()) for field, text in found if text.strip()]


def occurs(needle: str, text: str) -> bool:
    """Whether an authored key string appears in ``text``, case-folded.

    A string of ``MIN_SUBSTRING_CHARS`` or more is a substring match. A shorter one is a whole-token
    match, so that "2" does not hit "12". This checks that authored key strings do not leak and
    reads no meaning; a paraphrased leak is caught by the solvable and memory-needed gates instead.
    """
    needle, text = needle.casefold(), text.casefold()
    if len(needle) >= MIN_SUBSTRING_CHARS:
        return needle in text
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", text) is not None


def check_probes(project: dict) -> tuple[list[str], list[dict], int]:
    """``(refusals, warnings, strings checked)`` for the strings a probe's key must not share with its inputs.

    The inputs are the probe's task, its files and its session's files. A key string of
    ``MIN_SUBSTRING_CHARS`` or more that occurs there refuses the case set. A shorter one is only a
    warning, because a short string such as a number hits ordinary text. Warnings name the probe,
    the field and the place, never the string: the run record is read outside evaluator custody.
    """
    by_session = {s["session"]: s for s in project["sessions"]}
    refusals: list[str] = []
    warnings: list[dict] = []
    checked = 0
    for probe in project["probes"]:
        session = by_session[probe["session"]]
        places = [("task", probe["task"])]
        places += [(f"file {name}", text) for name, text in probe["files"].items()]
        places += [
            (f"session {session['session']} file {n}", t) for n, t in session["files"].items()
        ]
        for field, needle in key_strings(probe["key"]):
            checked += 1
            for where, text in places:
                if not occurs(needle, text):
                    continue
                if len(needle) >= MIN_SUBSTRING_CHARS:
                    refusals.append(f"probe {probe['probe_id']}: {field} appears in its {where}")
                else:
                    warnings.append(
                        {
                            "probe_id": probe["probe_id"],
                            "field": field,
                            "where": where,
                            "length": len(needle),
                        }
                    )
    return refusals, warnings, checked


def load_project(root: Path, name: str, digest: str) -> tuple[dict, dict]:
    """One project and its case-check record. Refuses a changed file, a wrong shape or a leaking probe."""
    path = cases_dir(root) / f"{name}.json"
    if not path.is_file():
        raise refused(f"{path} is missing")
    actual = sha256_of(path)
    if actual != digest:
        raise refused(f"{path} differs from SPLIT.json: expected {digest}, found {actual}")
    project = json.loads(path.read_text())
    if problems := structure_problems(project):
        raise refused(f"{path} is malformed:\n  " + "\n  ".join(problems))
    refusals, warnings, checked = check_probes(project)
    if refusals:
        raise refused(
            f"the case set holds its own answers; repair or drop these probes in {path}:\n  "
            + "\n  ".join(refusals)
        )
    return project, {
        "sha256": actual,
        "strings_checked": checked,
        "short_string_warnings": warnings,
    }


def load_split(root: Path, split: str, only: str | None = None) -> dict[str, tuple[dict, dict]]:
    """``name -> (project, case check)`` for the projects of ``split`` (``dev`` or ``heldout``), or just ``only``."""
    index = read_split(root)
    names = index[split]
    if only is not None:
        if only not in names:
            raise refused(f"--project {only} is not in the {split} split ({', '.join(names)})")
        names = [only]
    unlisted = [name for name in names if name not in index["case_sha256"]]
    if unlisted:
        raise refused(f"SPLIT.json has no case_sha256 for {', '.join(unlisted)}")
    return {name: load_project(root, name, index["case_sha256"][name]) for name in names}
