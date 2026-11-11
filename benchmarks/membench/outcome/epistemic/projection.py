"""The f38 projection: a generated, rendered and keyed world, as a project that the epistemic suite plays.

A project is the suite's case format: sessions with their files, and probes with their keys. The
projection changes no record of the world. It places each document in the session file of its week,
words each key for a judge that reads text, and labels each probe with the dimension that
``exomem.yaml`` gives its family.

* **Sessions.** Session ``n`` is week ``n`` of the world's timeline. Its files are the documents dated
  in that week, named by kind, date and a serial, so that no file name holds a thread or a step. Its
  task is one of the work tasks of ``exomem.yaml``, drawn by a seeded choice. It names no value.
* **Probes.** A probe belongs to the session of its date. Its task is the renderer's task, plus, for
  a premise probe, one line that states the stale state, because the case check requires the premise
  in the task. Every other string of the key stays out of the task, the probe's files and its
  session's files, by the case check's leak rule. The planner keeps a probe's session free of the key's
  things, so the projection only checks that, and refuses a world where a session holds a string.
* **Keys.** The oracle's answers are ids. The projection names each in words: an entity by its names
  that the task does not use, a statement by the sentence that its document renders, a set by its
  members. A key whose answer is no single value carries ``value: null`` and a rubric, which every
  key has: the reading's own description, the thing that it was asked of and the answer.
* **Contest.** Each document of a key's contest set becomes a ``{session, file}`` entry, in session order.
* **Labels.** A probe of the recall form is ``stable`` and has no change. Every other probe has the
  dimension of its family, or the family's own key where the family has none shipped, and the id of
  its thread as its change.

``projection_digest`` is the digest of ``exomem.yaml``: a world's manifest pins it, so an edited
prediction or work task changes no world.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from random import Random
from typing import Any

import yaml

from membench.clock import week_of
from membench.ids import child_seed

from ...world import core
from ...world.core import Vocabulary
from ...world.keys import Key, key_spec
from ...world.loader import CATALOGUE_DIR, Catalogue, Refusal, load_catalogue
from ...world.manifest import dump
from ...world.mechanisms import Oracle
from ...world.pack import PACKS_DIR, load_pack
from ...world.planner import PLANTED, Plan, RoundSpec, Thread, plan_world
from ...world.render import Document, Reader, Rendering, render
from ...world.surface import load_bank
from ..workspace import sha256_of
from .cases import MIN_SUBSTRING_CHARS, STABLE, cases_dir, occurs, probe_forms

#: The projection data: the dimension and the filed prediction of each family, and what f38 needs to play a project.
PROJECTION_FILE = Path(__file__).resolve().parents[2] / "projections" / "exomem.yaml"
#: The probe form that asks for a fact that never changes. ``attributes.yaml`` declares it, and f38 labels it ``stable``.
RECALL_FORM = "stable"
#: The one file under a document's kind that holds the text of a document with no kind.
_UNKIND = "file"


class ProjectionRefused(Exception):
    """The world cannot be projected; every refusal is in ``refusals``, each naming its probe."""

    def __init__(self, refusals: list[Refusal]):
        self.refusals = refusals
        super().__init__("\n".join(str(r) for r in refusals))


def load_projection(path: Path = PROJECTION_FILE) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def projection_digest(path: Path = PROJECTION_FILE) -> str:
    """The digest that a world's manifest pins for this projection: the SHA-256 of the file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def round_spec(catalogue: Catalogue, data: Mapping[str, Any], **dials: Any) -> RoundSpec:
    """The round that an f38 development project asks: the entries of its stage, and the recall control again
    until the project holds the recall probes that ``data`` asks. No recipe is added: a thread of one recipe
    again binds fresh things, and the planner reserves them."""
    stage = data["f38"]["stage"]
    entries = [i for i in catalogue.buildable if catalogue.entries[i].stage == stage]
    recall = [i for i in entries if RECALL_FORM in catalogue.entries[i].probes]
    again = next((i for i in recall if catalogue.entries[i].twin_of is None), None)
    short = data["f38"]["recall_probes"] - len(recall)
    if short > 0:
        if again is None:
            detail = "no entry of the stage plants a recall control"
            raise ProjectionRefused([Refusal("no-recall", f"stage {stage}", detail)])
        entries += [again] * short
    return RoundSpec(tuple(entries), **dials)


def session_of(day: date) -> int:
    """The session of a date: the week of the world's timeline that it falls in."""
    return week_of(day)


class _Words:
    """The words of a world's values, read from the rendered documents and the thread's own oracle."""

    def __init__(self, plan: Plan, rendering: Rendering, data: Mapping[str, Any]) -> None:
        self.world = plan.world()
        self.reader = Reader(plan, self.world)
        self.lines = {
            line.ref: line.text
            for document in rendering.documents.values()
            for line in document.lines
        }
        self.data = data["f38"]
        self.oracle: Oracle | None = None

    def bound(self, oracle: Oracle) -> _Words:
        """The same words, read through the oracle of one thread's own world."""
        other = copy.copy(self)
        other.oracle = oracle
        return other

    def names(self, entity: str, at: date) -> list[str]:
        """Every name of an entity at a date, in the order of the identity map."""
        return list(self.oracle.read("identity_map.names", of=entity, at=at))

    def leaf(self, value: Any, at: date, *, quoted: bool = False) -> str:
        """One answer that is no set. A statement is the sentence of its document, in quotes when ``quoted``,
        and an entity is its names."""
        if value is None:
            return self.data["words"]["none"]
        if isinstance(value, bool):
            return self.data["words"]["true" if value else "false"]
        if isinstance(value, str) and value in self.lines:
            return (
                self.data["statement"].format(text=self.lines[value])
                if quoted
                else self.lines[value]
            )
        if isinstance(value, str) and value in self.world.attributes:
            subject, schema = self.reader.where(value, at)
            about = subject if schema is None else schema.label
            return self.data["attribute"].format(about=about, subject=subject)
        if isinstance(value, str) and value in self.world.entities:
            return " / ".join(self.names(value, at))
        return self.reader.text(value, at)

    def phrase(self, value: Any, at: date) -> str:
        """An answer in words, keeping the shape of a set: members apart, a set within a set in brackets."""
        if not isinstance(value, list | tuple):
            return self.leaf(value, at, quoted=True)
        parts = [
            f"({self.phrase(v, at)})" if isinstance(v, list | tuple) else self.phrase(v, at)
            for v in value
        ]
        return "; ".join(parts) or self.data["words"]["none"]

    def members(self, value: Any, at: date, task: str) -> list[str]:
        """The strings that a key holds for an answer: each member of a set once. An entity gives each of its
        names that the task does not use, because a name in the task is no answer to the task."""
        if isinstance(value, list | tuple):
            return [m for v in value for m in self.members(v, at, task)]
        if value is None or isinstance(value, bool):
            return []
        if isinstance(value, str) and value in self.world.entities:
            return [n for n in self.names(value, at) if not occurs(n, task)]
        return [self.leaf(value, at)]


def _distinct(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _argument_words(
    words: _Words, thread: Thread, part: Mapping[str, Any], at: date
) -> tuple[str, str]:
    """What a key part was asked of, and its qualifiers (`where`, `who`, `for`), in words."""
    subject = words.phrase(thread.resolve(part["of"]), at) if "of" in part else ""
    qualifiers = [
        words.data["qualifier"].format(argument=name, value=words.phrase(thread.resolve(ref), at))
        for name, ref in part.items()
        if name not in ("read", "of", "when", "role")
    ]
    return subject, "".join(qualifiers)


def _rubric(
    words: _Words,
    thread: Thread,
    key: Key,
    spec: Mapping[str, Any],
    descriptions: Mapping[str, str],
) -> list[str]:
    """The key's rubric: the warranted answer, then each further fact, each as the reading, what it was
    asked of and its answer. A part that is a role names no reading, so it has no line."""
    parts = [("answer", spec["value"], key.value)]
    parts += [
        ("fact", part, fact.value)
        for part, fact in zip(spec.get("facts", []), key.facts, strict=True)
    ]
    lines = []
    for line, part, answer in parts:
        if "read" not in part:
            continue
        subject, qualifiers = _argument_words(words, thread, part, key.at)
        lines.append(
            words.data["rubric"][line].format(
                description=descriptions[part["read"]],
                of=subject + qualifiers,
                value=words.phrase(answer, key.at),
            )
        )
    return lines


def _descriptions() -> dict[str, str]:
    """The description of each reading, by its full name: ``mechanism.reading``."""
    raw = yaml.safe_load((CATALOGUE_DIR / "attributes.yaml").read_text(encoding="utf-8"))
    return {
        f"{mechanism}.{reading}": text
        for mechanism, body in raw["truth_mechanisms"].items()
        for reading, text in body["readings"].items()
    }


def project(
    plan: Plan, rendering: Rendering, catalogue: Catalogue, data: Mapping[str, Any]
) -> dict[str, Any]:
    """One f38 project: the sessions of ``plan``'s timeline and a probe for each key of ``rendering``.

    Raises ``ProjectionRefused`` when a key cannot be worded without a leak. The case check of the suite
    then runs on the project as it does on any other."""
    world = plan.world()
    files, place = _files(world.artefacts.values(), rendering.documents)
    sessions = [
        {"session": n, "task": _work_task(plan, data, n), "files": files.get(n, {})}
        for n in range(1, plan.spec.sessions + 1)
    ]
    tasks = {(p.thread, p.form): p for p in rendering.probes}
    descriptions, forms = _descriptions(), probe_forms()
    refusals: list[Refusal] = []
    probes = []
    reader = _Words(plan, rendering, data)
    for key in rendering.keys:
        thread = plan.thread(key.thread)
        entry = catalogue.entries[thread.entry]
        words = reader.bound(Oracle(plan.thread_world(thread, thread.variant), plan.validity()))
        spec = key_spec(catalogue, thread, key.form)
        task = tasks[(key.thread, key.form)].text
        about = f"probe {thread.id} {key.form}"

        states = spec["value"].get("read") not in data["f38"]["property_readings"]
        one = states and _is_one(key.value)
        singles = _distinct(words.members(key.value, key.at, task)) if states else []
        premise = None if key.premise is None else words.leaf(key.premise, key.at)
        if premise is not None and not occurs(premise, task):
            task = f"{task} {data['f38']['premise_line'].format(premise=premise)}"
        if one and not singles:
            refusals.append(
                Refusal("no-answer-name", about, "every name of the answer is in the task")
            )
            continue
        key_record = {
            "value": singles[0] if one else None,
            "accept": singles[1:] if one else [],
            "rubric": _rubric(words, thread, key, spec, descriptions),
            "behaviours": list(
                entry.expect if thread.variant == PLANTED else data["f38"]["mutation_behaviours"]
            ),
            "traps": _distinct([m for t in key.traps for m in words.members(t, key.at, task)]),
            "premise": premise,
            "reading": forms[key.form]["read_as"],
            "contest": sorted(
                ({"session": place[a][0], "file": place[a][1]} for a in key.contest),
                key=lambda ref: (ref["session"], ref["file"]),
            ),
        }
        strings = [
            s
            for s in (key_record["value"], *key_record["accept"], *key_record["traps"])
            if s and len(s) >= MIN_SUBSTRING_CHARS
        ]
        leaked = next(
            (n["session"] for n in sessions if any(occurs(x, n["task"]) for x in strings)), None
        )
        if leaked is not None:
            detail = f"the work task of session {leaked} holds a string of its key"
            refusals.append(Refusal("work-task-holds-key-string", about, detail))
            continue
        asked = session_of(key.at)
        if any(occurs(s, task) for s in strings):
            refusals.append(
                Refusal("task-holds-key-string", about, "its task holds a string of its key")
            )
            continue
        if any(occurs(s, text) for s in strings for text in files.get(asked, {}).values()):
            detail = f"a file of session {asked} holds a string of its key"
            refusals.append(Refusal("session-holds-key-string", about, detail))
            continue
        stable = key.form == RECALL_FORM
        probes.append(
            {
                "probe_id": f"{thread.id}.{key.form}",
                "session": asked,
                "task": task,
                "files": {},
                "dimension": STABLE if stable else _dimension(data, entry.family),
                "change_id": None if stable else thread.id,
                "family": entry.family,
                "phenomenon": entry.id,
                "key": key_record,
            }
        )
    if refusals:
        raise ProjectionRefused(refusals)
    return {"sessions": sessions, "probes": probes}


def _is_one(value: Any) -> bool:
    """Whether an answer is one thing that a key can hold as its value: a value, an entity or a statement."""
    if isinstance(value, list | tuple):
        return len(value) == 1 and _is_one(value[0])
    return value is not None and not isinstance(value, bool)


def _dimension(data: Mapping[str, Any], family: str) -> str:
    """The shipped dimension of a family, or the family's own key where the product ships none."""
    return data["families"][family]["dimension"] or family


def _work_task(plan: Plan, data: Mapping[str, Any], session: int) -> str:
    tasks = data["f38"]["work_tasks"]
    return tasks[Random(child_seed(plan.seed, "session", session)).randrange(len(tasks))]


def _files(
    artefacts: Any, documents: Mapping[str, Document]
) -> tuple[dict[int, dict[str, str]], dict[str, tuple[int, str]]]:
    """Each document as a file of its session, and where each artefact is. A file is named by the
    document's kind, its date and a serial among the documents of that kind and date."""
    slots: dict[tuple[str, date], list[str]] = defaultdict(list)
    for artefact in sorted(artefacts, key=lambda a: a.id):
        slots[(artefact.kind or _UNKIND, artefact.at)].append(artefact.id)
    files: dict[int, dict[str, str]] = defaultdict(dict)
    place: dict[str, tuple[int, str]] = {}
    for (kind, day), ids in slots.items():
        for serial, id_ in enumerate(ids, start=1):
            name = f"{kind}/{day.isoformat()}-{serial}.txt"
            files[session_of(day)][name] = documents[id_].text
            place[id_] = (session_of(day), name)
    return dict(files), place


def write_project(root: Path, name: str, project_record: Mapping[str, Any]) -> Path:
    """Write ``project-<name>.json`` and ``SPLIT.json`` under ``root``, as the suite reads them: the project is the
    development project, and the held-out list is empty."""
    directory = cases_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(dump(project_record), encoding="utf-8")
    split = {"dev": [name], "heldout": [], "case_sha256": {name: sha256_of(path)}}
    (directory / "SPLIT.json").write_text(json.dumps(split, indent=1) + "\n", encoding="utf-8")
    return path


def generate(root: Path, seed: int, *, level: str = "stated", **dials: Any) -> dict[str, Any]:
    """Generate the development world of ``seed``, write it with its keys under ``root/world``, and write its
    f38 project under ``root`` (``cases/epistemic``). Returns the world's manifest digest, the project's name
    and its file's digest. Refuses a world whose keys cannot be worded, as ``project`` does."""
    data, vocabulary = load_projection(), Vocabulary.load()
    catalogue = load_catalogue(implemented=core.implemented())
    pack = load_pack(PACKS_DIR / "operations.yaml", vocabulary)
    split = "dev"
    bank = load_bank(PACKS_DIR / f"{pack.id}.{split}.forms.yaml", vocabulary, pack)
    plan = plan_world(
        round_spec(catalogue, data, **dials), catalogue, pack, vocabulary, split, seed
    )
    rendering = render(plan, catalogue, bank, level)
    record = project(plan, rendering, catalogue, data)
    name = f"project-{plan.name}"
    path = write_project(root, name, record)
    digest = rendering.write(plan, root / "world", projection_digest())
    return {"manifest": digest, "project": name, "sha256": sha256_of(path)}
