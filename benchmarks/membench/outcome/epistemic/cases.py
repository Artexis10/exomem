"""The f38 case set: its split, its digests and the checks that run before any session starts.

The projects are written by an independent lane and stay in evaluator custody under the run root
(``cases/epistemic/``): ``project-<x>.json`` files and ``SPLIT.json``, which names the dev and
held-out projects and the SHA-256 of every project file. A run refuses a project whose file differs
from that digest, whose structure is wrong, or whose probe holds its own answer.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import yaml

from ...world.loader import CATALOGUE_DIR
from ..workspace import sha256_of

#: Where a project file sits under the run root.
CASES_DIR = ("cases", "epistemic")
#: The dimension label of a probe that asks for a fact that never changes (a bench label, not an epistemic dimension).
STABLE = "stable"
#: A key string of this many characters or more is checked as a substring; a shorter one as a whole token.
MIN_SUBSTRING_CHARS = 4
#: The one behaviour that code names: the judge rates reliance on a trap as this, and ``judge.behaviours_asked``
#: adds it to every change probe. The registry must hold it; every other behaviour is read from the registry.
TRUSTS_STALE = "trusts_stale"
#: The one reading that code acts on: a probe whose key names it is read as harm against arm N, which is expected to pass
#: it, so the memory-needed gate skips it. ``attributes.yaml`` declares it in ``result_readings`` and code implements it.
HARM = "harm"
#: The one arm that reads a key's contest set: it has no memory and sees the scripted files that ``key.contest`` names.
#: Its code is named here because the case check and the scheduler both act on it; the arm table is in ``run.py``.
GOLD_ARM = "G"
#: Top-level workspace names the harness owns: a scripted file there would collide with an arm's memory or instruction.
RESERVED_NAMES = ("memory", "CLAUDE.md", ".claude")
#: The names the harness itself introduces for a memory store: the folder it gives arm F and the product's name.
#: A closed set of the harness's own literals, not a reading of what an answer means. The judge reads each token
#: that holds one as a neutral mark, and the case check keeps them out of case text, so every masked token comes
#: from an arm. The mask hides these names; it does not hide every hint of the arm.
STORE_NAMES = ("memory/", "exomem")


@functools.cache
def _catalogue(name: str, field: str) -> dict:
    """``field`` of one catalogue file, read once: the shipped registry is the only list that the suite reads."""
    return yaml.safe_load((CATALOGUE_DIR / name).read_text(encoding="utf-8"))[field]


def behaviours() -> dict[str, dict]:
    """Every behaviour that the judge may rate, with its polarity and description, from ``behaviours.yaml``."""
    return _catalogue("behaviours.yaml", "behaviours")


def readings() -> dict[str, str]:
    """Every way that a probe's result is read, from ``result_readings`` in ``attributes.yaml``."""
    return _catalogue("attributes.yaml", "result_readings")


def probe_forms() -> dict[str, dict]:
    """Every form of probe, with how its result is read (``read_as``), from ``attributes.yaml``."""
    return _catalogue("attributes.yaml", "probe_forms")


def families() -> dict[str, dict]:
    """Every family of the phenomenon catalogue, in registry order, from ``families.yaml``."""
    return _catalogue("families.yaml", "families")


def lineage_sources() -> dict[str, dict]:
    """Every lineage source that a phenomenon or a family may name, from ``lineage.yaml``."""
    return _catalogue("lineage.yaml", "lineage")


def holds_store_name(text: str) -> bool:
    """Whether ``text`` holds a store name: the one rule that the mask, the case check and the report count share."""
    folded = text.casefold()
    return any(name in folded for name in STORE_NAMES)


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


def is_harm(probe: dict) -> bool:
    """Whether the probe's key reads it as harm; a key with no reading is read as a position, as every hand-written key is."""
    return probe["key"].get("reading") == HARM


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
    files_of_session = {
        s.get("session"): s["files"] for s in sessions if isinstance(s.get("files"), dict)
    }
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
        # `value` is null when the answer is not one thing (an attribution, an open question); the rubric carries it.
        if not isinstance(key, dict) or not isinstance(key.get("value"), (str, type(None))):
            problems.append(f"probe {name} needs a `key` whose `value` is a string or null")
        elif not all(isinstance(key.get(f), list) for f in ("accept", "rubric", "behaviours")):
            problems.append(
                f"probe {name}: key.accept, key.rubric and key.behaviours must be lists"
            )
        else:
            problems += key_problems(name, key, p.get("task"))
            problems += contest_problems(name, p, files_of_session)
        # A probe that is neither stable nor tied to a change would silently drop out of every statistic.
        if (p.get("change_id") is None) != (p.get("dimension") == STABLE):
            problems.append(
                f"probe {name}: a stable probe has no change_id, and a change probe has one"
            )
    return problems + store_name_problems(project)


def store_name_problems(project: dict) -> list[str]:
    """Each place where case text holds a store name, which the judge would read masked as an arm's note."""
    places: list[tuple[str, object]] = []

    def files_of(item: dict) -> dict:
        # `structure_problems` already names a `files` value that is not an object.
        files = item.get("files")
        return files if isinstance(files, dict) else {}

    for s in project.get("sessions") or []:
        places.append((f"session {s.get('session')} task", s.get("task")))
        for path, text in files_of(s).items():
            places.append((f"session {s.get('session')} file {path}", f"{path}\n{text}"))
    for p in project.get("probes") or []:
        name = p.get("probe_id")
        places.append((f"probe {name} task", p.get("task")))
        for path, text in files_of(p).items():
            places.append((f"probe {name} file {path}", f"{path}\n{text}"))
        key = p.get("key") if isinstance(p.get("key"), dict) else {}
        places.append((f"probe {name} key.value", key.get("value")))
        # `key_problems` already names a trap field of the wrong type; `key_strings` skips it here.
        places += [(f"probe {name} {field}", text) for field, text in trap_fields(key)]
        for field in ("accept", "rubric"):
            # `structure_problems` already names a field that is not a list.
            texts = key.get(field) if isinstance(key.get(field), list) else []
            for i, text in enumerate(texts):
                places.append((f"probe {name} key.{field}[{i}]", text))
    return [
        f"{where} holds a store name the judge masks ({', '.join(STORE_NAMES)}); reword it"
        for where, text in places
        if isinstance(text, str) and holds_store_name(text)
    ]


def key_traps(key: dict) -> list[str]:
    """The traps of a key as a list: ``key.traps``, or a hand-written project's single ``key.trap`` as a list of one."""
    if isinstance(key.get("traps"), list):
        return list(key["traps"])
    return [key["trap"]] if key.get("trap") else []


def trap_fields(key: dict) -> list[tuple[str, object]]:
    """``(field, trap)`` for each trap of a key, named as the author wrote it."""
    if isinstance(key.get("traps"), list):
        return [(f"key.traps[{i}]", trap) for i, trap in enumerate(key["traps"])]
    return [("key.trap", key["trap"])] if key.get("trap") else []


def key_problems(name: str, key: dict, task: object) -> list[str]:
    """What is wrong inside the lists of a key whose shape is right: each problem names what to fix."""
    problems = []
    if not all(isinstance(text, str) for text in key["accept"]):
        problems.append(f"probe {name}: every key.accept item must be a string")
    if not isinstance(key.get("trap"), (str, type(None))):
        problems.append(f"probe {name}: key.trap must be a string or null")
    if "traps" in key:
        traps = key["traps"]
        if not isinstance(traps, list) or not all(isinstance(trap, str) for trap in traps):
            problems.append(f"probe {name}: key.traps must be a list of strings")
        elif key.get("trap"):
            problems.append(f"probe {name}: give key.traps or key.trap, not both")
    problems += premise_problems(name, key.get("premise"), task)
    reading = key.get("reading")
    if reading is not None and (not isinstance(reading, str) or reading not in readings()):
        problems.append(
            f"probe {name}: key.reading names {reading!r}, which the registry lacks "
            f"(result_readings in attributes.yaml); use {list(readings())} or leave it out"
        )
    if unknown := [b for b in key["behaviours"] if b not in behaviours()]:
        problems.append(
            f"probe {name}: key.behaviours names {unknown}, which the behaviour registry "
            f"(behaviours.yaml) lacks; use {list(behaviours())}"
        )
    return problems


def contest_problems(name: str, probe: dict, files_of_session: dict[int, dict]) -> list[str]:
    """What is wrong with ``key.contest``: each entry names a scripted file of the probe's session or an earlier one.

    ``key.contest`` is optional here: a hand-written project has none, and only arm G needs it
    (``require_contest_sets``). A file that the project does not hold would drop out of G's prompt unseen.
    """
    contest = probe["key"].get("contest")
    if contest is None:
        return []
    shape = (
        isinstance(contest, list)
        and all(isinstance(ref, dict) and set(ref) == {"session", "file"} for ref in contest)
        and all(isinstance(ref["session"], int) and isinstance(ref["file"], str) for ref in contest)
    )
    if not shape:
        return [
            f"probe {name}: key.contest must be a list of objects with a `session` number and a `file` name"
        ]
    return [
        f"probe {name}: key.contest names file {ref['file']!r} of session {ref['session']}, "
        "which is not a scripted file of the probe's session or an earlier one"
        for ref in contest
        if ref["session"] > probe["session"]
        or ref["file"] not in files_of_session.get(ref["session"], {})
    ]


def require_contest_sets(projects: dict[str, tuple[dict, dict]], arms: list[str]) -> None:
    """Refuse arm G on a project with a probe that has no contest set, before any session starts.

    G is the ceiling: it reads what the key's author says bears on the probe. A probe without that
    would reach G as it reaches N, and a ceiling that equals the floor on some probes reads as no room
    for a reason the case set hides. A case set that holds no contest set (the hand-written projects)
    plays the other arms.
    """
    if GOLD_ARM not in arms:
        return
    lacking = [
        f"{name}: {', '.join(p['probe_id'] for p in project['probes'] if 'contest' not in p['key'])}"
        for name, (project, _) in projects.items()
        if any("contest" not in p["key"] for p in project["probes"])
    ]
    if lacking:
        raise refused(
            f"arm {GOLD_ARM} reads each probe's contest set (`key.contest`), and these probes have none: "
            f"{'; '.join(lacking)}. Add a contest set to each, or leave {GOLD_ARM} out of --arms"
        )


def premise_problems(name: str, premise: object, task: object) -> list[str]:
    """A premise is the state that the probe's task presupposes, so the task must contain it as authored."""
    if premise is None:
        return []
    if not isinstance(premise, str) or not premise.strip():
        return [f"probe {name}: key.premise must be a non-empty string or null"]
    if isinstance(task, str) and not occurs(premise.strip(), task):
        return [
            f"probe {name}: key.premise is not in its task; a premise probe's task must state "
            "the state that it presupposes"
        ]
    return []


def key_strings(key: dict) -> list[tuple[str, str]]:
    """``(field, string)`` for the key's value, each accepted phrasing and each trap, as authored.

    The premise is not here: its task must hold it, so the leak rule would refuse every premise probe.
    """
    found = [("key.value", key["value"])] if key["value"] is not None else []
    found += [(f"key.accept[{i}]", text) for i, text in enumerate(key["accept"])]
    found += trap_fields(key)
    return [
        (field, text.strip()) for field, text in found if isinstance(text, str) and text.strip()
    ]


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


def require_unchanged_since_run(projects: dict[str, tuple[dict, dict]], recorded: dict) -> None:
    """Refuse when a case file's digest differs from the one the run recorded when it played that project.

    ``load_split`` checks each file against ``SPLIT.json``; a report must also read the file the run played.
    A project the run did not record is not compared.
    """
    changed = [
        name
        for name, (_, check) in projects.items()
        if name in recorded and recorded[name]["sha256"] != check["sha256"]
    ]
    if changed:
        raise refused(
            f"the case file of {', '.join(changed)} changed since this run played it; "
            "judge it against the file it played, or start a new run"
        )
