"""The f38 projection of the synthetic world: a generated development project that the suite plays.

The first case is the workflow: generate the seed-7 development world (and a second seed), write its
project, and run the suite's own case check on it. The project holds at least two change probes for
each of the 11 dimensions and four recall probes, and two runs give the same bytes. The next cases
show what the projection does that the case check alone would refuse: it asks a probe in a later
session when its planned week names the answer, and it refuses a world where no session is free. The
rest break one rule each: a work task that holds a key value, a contest entry that names a later
session, and a family key as a probe's label.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from benchmarks.membench.outcome.epistemic import cases, projection
from benchmarks.membench.world import core
from benchmarks.membench.world.core import Vocabulary
from benchmarks.membench.world.keys import key_spec
from benchmarks.membench.world.loader import load_catalogue
from benchmarks.membench.world.mechanisms import Oracle
from benchmarks.membench.world.pack import PACKS_DIR, load_pack
from benchmarks.membench.world.planner import plan_world
from benchmarks.membench.world.render import render
from benchmarks.membench.world.surface import load_bank
from exomem.public_artifact_privacy import scan_artifact

VOCABULARY = Vocabulary.load()
CATALOGUE = load_catalogue(implemented=core.implemented())
PACK = load_pack(PACKS_DIR / "operations.yaml", VOCABULARY)
BANK = load_bank(PACKS_DIR / "operations.dev.forms.yaml", VOCABULARY, PACK)
DATA = projection.load_projection()
# The eleven dimensions that f38 reports, as the epistemic task suite's design lists them.
DIMENSIONS = (
    "currency correction provenance attribution kind argument dependency entities identity scope"
    " uncertainty"
).split()


@functools.cache
def built(seed: int, twins: float = 0.25) -> tuple[Any, Any, dict]:
    """The plan, the rendering at `stated` and the project record of a development world."""
    plan = plan_world(
        projection.round_spec(CATALOGUE, DATA, twins=twins),
        CATALOGUE,
        PACK,
        VOCABULARY,
        "dev",
        seed,
    )
    rendering = render(plan, CATALOGUE, BANK, "stated")
    return plan, rendering, projection.project(plan, rendering, CATALOGUE, DATA)


def probes_of(record: dict) -> dict[str, dict]:
    return {p["probe_id"]: p for p in record["probes"]}


# -- the workflow ---------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [7, 3])
def test_a_generated_development_project_passes_the_suites_case_check(
    seed: int, tmp_path: Path
) -> None:
    summary = projection.generate(tmp_path, seed)

    ((project, check),) = cases.load_split(tmp_path, "dev").values()  # the suite's own check
    assert check["sha256"] == summary["sha256"] and check["strings_checked"] > 0
    assert len(project["sessions"]) == 12
    change = [p for p in project["probes"] if p["change_id"] is not None]
    for dimension in DIMENSIONS:
        assert sum(p["dimension"] == dimension for p in change) >= 2, dimension
    assert {p["dimension"] for p in change} == set(DIMENSIONS)
    recall = [p for p in project["probes"] if p["dimension"] == cases.STABLE]
    assert len(recall) == 4 and all(p["change_id"] is None for p in recall)
    # What an actor or a judge reads names no raw id of the world: sessions, tasks, files and keys.
    read = [
        project["sessions"],
        [{k: p[k] for k in ("task", "files", "key")} for p in project["probes"]],
    ]
    assert "dev-00." not in json.dumps(read)
    # The written world passes the privacy gate with its keys and the project beside it.
    assert not scan_artifact(tmp_path / "cases" / "epistemic" / f"{summary['project']}.json")


def test_two_runs_give_the_same_project_and_the_manifest_pins_the_projection_file(
    tmp_path: Path,
) -> None:
    first = projection.generate(tmp_path / "first", 7)
    second = projection.generate(tmp_path / "second", 7)
    assert first == second

    # A third run in a process with another hash seed gives the same bytes.
    root = Path(__file__).resolve().parents[1]
    code = "import sys; from pathlib import Path; from benchmarks.membench.outcome.epistemic import projection as p; print(p.generate(Path(sys.argv[1]), 7))"
    env = {**os.environ, "PYTHONHASHSEED": "3", "PYTHONPATH": f"{root / 'benchmarks'}:{root}"}
    other = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "third")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert other.stdout.strip() == str(first)

    manifest = json.loads((tmp_path / "first" / "world" / "manifest.json").read_text())
    assert (
        manifest["projection_digest"]
        == hashlib.sha256(projection.PROJECTION_FILE.read_bytes()).hexdigest()
    )


# -- what the projection does that the case check alone would refuse ----------------------------------


def test_every_probe_plays_in_the_session_that_the_planner_chose() -> None:
    for seed in (
        1,
        2,
        7,
    ):  # on the first commit of this projection, seed 1 shifted and seed 2 refused
        _, rendering, record = built(seed)
        planned = {f"{k.thread}.{k.form}": projection.session_of(k.at) for k in rendering.keys}
        files = {s["session"]: s["files"] for s in record["sessions"]}
        for probe in record["probes"]:
            assert probe["session"] == planned[probe["probe_id"]], (seed, probe["probe_id"])
            key = probe["key"]
            strings = [s for s in (key["value"], *key["accept"], *key["traps"]) if s]
            held = [
                t
                for t in files[probe["session"]].values()
                if any(cases.occurs(s, t) for s in strings if len(s) >= cases.MIN_SUBSTRING_CHARS)
            ]
            assert not held, (seed, probe["probe_id"])


def test_a_session_that_holds_a_string_of_a_probes_key_is_refused() -> None:
    plan, rendering, record = built(7)
    probe = next(p for p in record["probes"] if p["key"]["value"] and len(p["key"]["value"]) >= 4)
    leaky = next(  # a document of another thread, dated in the probe's session
        d
        for d in rendering.documents.values()
        if projection.session_of(plan.world().artefacts[d.artefact].at) == probe["session"]
        and not d.artefact.startswith(probe["probe_id"].rsplit(".", 1)[0])
    )
    documents = {
        **rendering.documents,
        leaky.artefact: dataclasses.replace(leaky, text=f"{leaky.text} {probe['key']['value']}"),
    }
    with pytest.raises(projection.ProjectionRefused) as refused:
        projection.project(
            plan, dataclasses.replace(rendering, documents=documents), CATALOGUE, DATA
        )
    thread, form = probe["probe_id"].rsplit(".", 1)
    assert f"session-holds-key-string: probe {thread} {form}" in str(refused.value)


def test_an_owner_or_seat_role_binds_an_attribute_whose_values_are_people() -> None:
    for seed in (7, 3):
        plan, rendering, _ = built(seed)
        threads = [
            t for t in plan.threads if t.recipe in ("entities.owner-change", "entities.role-change")
        ]
        assert threads
        for thread in threads:
            roles = (
                ("owner",) if thread.recipe == "entities.owner-change" else ("seat", "next_seat")
            )
            for role in roles:
                schema = plan.backbone.attributes[thread.roles[role]].schema
                assert schema.domain.generator == "holder", (thread.id, schema.label)
            for statement in plan.thread_world(thread, thread.variant).statements.values():
                person = plan.backbone.entities[statement.value]
                assert person.type == "person", (thread.id, person.name)
                assert person.name in rendering.documents[f"{statement.id}.doc"].text


def test_a_scope_probe_names_the_context_that_its_key_reads() -> None:
    for seed in (7, 3):
        plan, _, record = built(seed)
        scoped = [p for p in record["probes"] if p["family"] == "scope"]
        assert scoped
        for probe in scoped:
            thread = plan.thread(probe["change_id"])
            form = probe["probe_id"].rsplit(".", 1)[1]
            context = key_spec(CATALOGUE, thread, form)["value"]["where"]
            assert cases.occurs(plan.backbone.context_names[thread.roles[context]], probe["task"])


def test_a_work_task_that_holds_a_key_value_is_refused() -> None:
    plan, rendering, record = built(7)
    value = next(p["key"]["value"] for p in record["probes"] if p["key"]["value"])
    leaky = copy.deepcopy(DATA)
    leaky["f38"]["work_tasks"] = [f"Go through this week's files and keep to {value}."]
    with pytest.raises(projection.ProjectionRefused) as refused:
        projection.project(plan, rendering, CATALOGUE, leaky)
    assert "work-task-holds-key-string" in {r.code for r in refused.value.refusals}


def test_a_premise_probe_states_its_premise_and_a_probe_in_the_mutation_world_has_none() -> None:
    _, _, planted = built(7, twins=0.0)
    premise_probes = [p for p in planted["probes"] if p["key"]["premise"]]
    assert premise_probes
    for probe in premise_probes:
        assert cases.occurs(probe["key"]["premise"], probe["task"])  # the case check needs it
        assert probe["key"]["premise"] not in probe["key"]["traps"]

    plan, rendering, mutated = built(7, twins=1.0)
    asking = [p for p in mutated["probes"] if p["probe_id"].endswith(".premise")]
    assert asking
    behaviours = DATA["f38"]["mutation_behaviours"]
    for probe in asking:
        assert probe["key"]["premise"] is None and probe["key"]["behaviours"] == behaviours
        assert DATA["f38"]["premise_line"].split("{")[0] not in probe["task"]

    # The false premise's event is recorded in the mutation world, so the probe shows the judge no
    # premise; the event's carrier still reaches the contest set through the key's premise reading.
    twin = next(p for p in asking if p["phenomenon"] == "absence.false-premise")
    key = next(k for k in rendering.keys if f"{k.thread}.{k.form}" == twin["probe_id"])
    assert key.premise is not None and key.contest
    thread = plan.thread(key.thread)
    world = plan.thread_world(thread, thread.variant)
    oracle = Oracle(world, plan.validity())
    premise = projection._Words(plan, rendering, DATA).bound(oracle).leaf(key.premise, key.at)
    contest_texts = {
        mutated["sessions"][ref["session"] - 1]["files"][ref["file"]]
        for ref in twin["key"]["contest"]
    }
    assert contest_texts == {rendering.documents[a].text for a in key.contest}
    assert any(cases.occurs(premise, text) for text in contest_texts), (
        "the event's carrier left the contest set"
    )


def test_a_contest_entry_that_names_a_later_session_is_refused() -> None:
    _, _, record = built(7)
    broken = copy.deepcopy(record)
    probe = next(p for p in broken["probes"] if p["key"]["contest"] and p["session"] > 1)
    assert cases.structure_problems(broken) == []
    probe["session"] = min(ref["session"] for ref in probe["key"]["contest"]) - 1
    problems = cases.structure_problems(broken)
    assert any(probe["probe_id"] in p and "earlier" in p for p in problems)


def test_a_family_key_is_accepted_as_a_probe_label() -> None:
    _, _, record = built(7)
    relabelled = copy.deepcopy(record)
    next(p for p in relabelled["probes"] if p["change_id"])["dimension"] = "quantity"
    assert cases.structure_problems(relabelled) == []
    # A family that ships no dimension keeps its own key as the label.
    assert projection._dimension(DATA, "persistence") == "persistence"
    assert projection._dimension(DATA, "absence") == "uncertainty"


def test_every_probe_names_its_phenomenon_and_the_family_that_the_phenomenon_belongs_to() -> None:
    """The report finds a probe's lineage and its norms through the phenomenon, and its family through the family."""
    _, _, record = built(7)
    entries = CATALOGUE.entries
    assert {p["phenomenon"] for p in record["probes"]} <= set(entries)
    assert all(entries[p["phenomenon"]].family == p["family"] for p in record["probes"])
