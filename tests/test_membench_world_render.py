"""The template renderer of the synthetic world, at the levels `stated`, `paraphrased` and `unmarked`.

The first case is the workflow: render the seed-7 development world at each level, check that the
level changes the text and never the key, that no document below `stated` holds an explicit form, that
no probe shares a surface form with its thread's documents, and that the written world and its keys
pass the privacy gate. The next shows that each operation has a document that says what it did. The rest break
one rule each: an anchor that is missing, an explicit form below `stated`, a probe that shares a form,
a bank of another split, and a bank file that the loader refuses.
"""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml
from membench.clock import week_date
from membench.generate import _lint_vocabulary

from benchmarks.membench.world import core
from benchmarks.membench.world.core import Vocabulary
from benchmarks.membench.world.keys import write_keys
from benchmarks.membench.world.loader import load_catalogue
from benchmarks.membench.world.pack import PACKS_DIR, load_pack
from benchmarks.membench.world.planner import PLANTED, RoundSpec, Thread, plan_world
from benchmarks.membench.world.render import RenderRefused, anchor_problems, holds, render
from benchmarks.membench.world.surface import LEVELS, STATED, BankRefused, load_bank
from exomem.public_artifact_privacy import scan_artifact

VOCABULARY = Vocabulary.load()
CATALOGUE = load_catalogue(implemented=core.implemented())
PACK = load_pack(PACKS_DIR / "operations.yaml")
BANK_FILE = PACKS_DIR / "operations.dev.forms.yaml"
BANK = load_bank(BANK_FILE, VOCABULARY, PACK)
SLICE_ONE = tuple(i for i in CATALOGUE.buildable if CATALOGUE.entries[i].stage == "s1")
PLAN = plan_world(RoundSpec(SLICE_ONE), CATALOGUE, PACK, VOCABULARY, "dev", 7)


def bank_with(tmp_path: Path, edit: Any) -> Any:
    """The shipped bank after ``edit`` has changed its raw data, read back through the loader."""
    raw = copy.deepcopy(yaml.safe_load(BANK_FILE.read_text(encoding="utf-8")))
    edit(raw)
    path = tmp_path / "edited.forms.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_bank(path, VOCABULARY, PACK)


def form(raw: dict[str, Any], id_: str) -> dict[str, Any]:
    return next(f for f in raw["forms"] if f["id"] == id_)


def refused_codes(bank: Any, plan: Any = PLAN, level: str = "paraphrased") -> set[str]:
    with pytest.raises(RenderRefused) as refused:
        render(plan, CATALOGUE, bank, level)
    return {r.code for r in refused.value.refusals}


# -- the workflow ---------------------------------------------------------------------------------


def test_each_level_changes_the_text_of_the_planted_change_and_never_the_key(
    tmp_path: Path,
) -> None:
    rendered = {level: render(PLAN, CATALOGUE, BANK, level) for level in LEVELS}
    stated = rendered[STATED]
    thread = next(t for t in PLAN.threads if t.entry == "currency.reversal")
    new = thread.roles["new"]
    key = next(k for k in stated.keys if k.thread == thread.id and k.form == "state")
    assert key.value == new and key.proof

    for level, rendering in rendered.items():
        assert rendering.keys == write_keys(PLAN, CATALOGUE) == stated.keys  # no level moves a key
        assert set(rendering.documents) == set(stated.documents)
        # The document that carries the answer still shows it, at every level.
        for artefact in key.proof:
            assert holds(rendering.documents[artefact].text, str(new)), (level, artefact)

    # Below `stated`, the documents of the planted change differ from their `stated` text, and
    # none holds the sentence that its `stated` text gives.
    for level in ("paraphrased", "unmarked"):
        below = [d for d in rendered[level].documents.values() if d.level == level]
        assert below
        for document in below:
            explicit = stated.documents[document.artefact].lines
            for line in explicit:
                assert not holds(document.text, line.text), (level, document.artefact)
        # The documents of the other statements stay at `stated`.
        assert {d.level for d in rendered[level].documents.values()} == {STATED, level}

    # A paraphrased document names an attribute in words other than the probe's label.
    world = PLAN.world()
    for document in rendered["paraphrased"].documents.values():
        statement = world.statements.get(document.lines[0].ref)
        if (
            document.level == "paraphrased"
            and statement
            and statement.about in PLAN.backbone.attributes
        ):
            label = PLAN.backbone.attributes[statement.about].schema.label
            assert not holds(document.text, label), document.artefact

    # No probe is made from a surface form that a document of its own thread uses.
    thread_of = {s["id"]: t.id for t in PLAN.threads for s in t.steps if "id" in s}
    for rendering in rendered.values():
        assert rendering.probes
        used: dict[str, set[str]] = {}
        for document in rendering.documents.values():
            owner = thread_of.get(document.lines[0].ref)
            used.setdefault(owner or "", set()).update(document.forms)
        for probe in rendering.probes:
            assert probe.pattern not in used.get(probe.thread, set()), probe

    # Two renders and two writes give the same bytes, and the manifest names the level and the form
    # of every document. The projection digest is the projection file's, which no render writes.
    twice = render(PLAN, CATALOGUE, BANK, "unmarked")
    assert twice.manifest() == rendered["unmarked"].manifest()
    digests = {level: r.write(PLAN, tmp_path / level) for level, r in rendered.items()} | {
        "again": twice.write(PLAN, tmp_path / "again")
    }
    assert digests["again"] == digests["unmarked"] and len(set(digests.values())) == 3
    assert (tmp_path / "again" / "manifest.json").read_bytes() == (
        tmp_path / "unmarked" / "manifest.json"
    ).read_bytes()
    manifest = rendered["paraphrased"].manifest()
    assert all(a["forms"] and a["hash"] for a in manifest["artefacts"])
    assert PLAN.manifest(manifest)["projection_digest"] is None

    # The written world, its documents and the bank pass the privacy gate and the lint.
    written = [BANK_FILE, *sorted((tmp_path / "paraphrased").rglob("*.*"))]
    assert [f for f in written if scan_artifact(f, label=f.name)] == []
    errors: list[str] = []
    for f in written:
        _lint_vocabulary(f.read_text(encoding="utf-8"), f.name, errors)
    assert errors == []


# -- the operations -------------------------------------------------------------------------------


def test_each_operation_is_announced_in_a_document_that_says_what_it_did() -> None:
    backbone = PLAN.backbone
    reserved = {r for t in PLAN.threads for r in t.reserved}
    attribute = next(a for a in sorted(backbone.attributes) if a not in reserved)
    first, second = backbone.things()[:2]
    holder_a, holder_b = backbone.holders()[:2]
    audience = sorted(backbone.audiences)[0]
    old, new = backbone.attributes[attribute].schema.domain.values()[:2]
    former = backbone.entities[first].name
    d = [week_date(w) for w in (3, 4, 5, 6)]
    steps = (
        {
            "op": "state",
            "id": "x.said",
            "by": holder_a,
            "about": attribute,
            "value": old,
            "kind": "fact",
            "at": d[0],
        },
        {
            "op": "correct",
            "id": "x.fix",
            "by": holder_b,
            "statement": "x.said",
            "value": new,
            "at": d[1],
        },
        {"op": "withdraw", "id": "x.drop", "by": holder_a, "statement": "x.said", "at": d[2]},
        {"op": "tell", "id": "x.tell", "holder": holder_b, "statement": "x.said", "at": d[1]},
        {
            "op": "restrict",
            "id": "x.limit",
            "target": "x.said",
            "audience": audience,
            "by": holder_a,
            "at": d[2],
            "until": d[3],
        },
        {
            "op": "name",
            "id": "x.rename",
            "entity": first,
            "name": "Zorbel",
            "replaces": former,
            "at": d[1],
        },
        {"op": "merge", "id": "x.merge", "entities": [first, second], "at": d[3]},
    )
    # A thread whose key reads the attribute and nothing else, so that its own steps need no recipe.
    extra = Thread(
        "x",
        "persistence.permanent",
        "persistence.permanent",
        "persistence",
        PLANTED,
        {"item": attribute},
        {"probe": d[3]},
        steps,
        (),
        {},
    )
    plan = dataclasses.replace(PLAN, threads=(*PLAN.threads, extra))
    texts = {k: v.text for k, v in render(plan, CATALOGUE, BANK, STATED).documents.items()}
    # The same operations, as steps of the planted change, at the other levels.
    changed = tuple({**step, "world": PLANTED} for step in steps)
    plan_changed = dataclasses.replace(
        plan, threads=(*PLAN.threads, dataclasses.replace(extra, steps=changed))
    )
    paraphrased = render(plan_changed, CATALOGUE, BANK, "paraphrased").documents
    assert all(
        paraphrased[f"x.{op}.doc"].level == "paraphrased"
        for op in ("fix", "drop", "tell", "limit", "rename", "merge")
    )
    assert all(paraphrased[k].text != v for k, v in texts.items() if k.startswith("x."))
    # A withdrawal, a telling, a restriction and a merger have no `unmarked` form: with no new content
    # to show, the bank names the gap in place of writing a marked line.
    with pytest.raises(RenderRefused) as unmarked:
        render(plan_changed, CATALOGUE, BANK, "unmarked")
    assert {(r.code, r.item) for r in unmarked.value.refusals} == {
        ("no-form", f"x.{op}.doc") for op in ("drop", "tell", "limit", "merge")
    }

    def names(*ids: str) -> list[str]:
        return [backbone.entities[i].name for i in ids]

    assert all(
        holds(texts["x.fix.doc"], v) for v in (str(old), str(new))
    )  # what it corrects, and the correction
    assert holds(texts["x.drop.doc"], str(old))
    assert all(holds(texts["x.tell.doc"], n) for n in (*names(holder_a, holder_b), str(old)))
    assert all(
        holds(texts["x.limit.doc"], n)
        for n in (*names(*backbone.audiences[audience]), d[3].isoformat())
    )
    assert all(holds(texts["x.rename.doc"], n) for n in (former, "Zorbel"))
    assert all(
        holds(texts["x.merge.doc"], n) for n in ("Zorbel", *names(second))
    )  # the survivor, renamed
    assert (
        len({texts[f"x.{op}.doc"] for op in ("fix", "drop", "tell", "limit", "rename", "merge")})
        == 6
    )


# -- one failing case for each check ----------------------------------------------------------------


def test_a_form_that_leaves_out_the_name_of_the_subject_fails_the_anchor_check(
    tmp_path: Path,
) -> None:
    def drop_subject(raw: dict[str, Any]) -> None:
        raw["forms"].remove(form(raw, "fact-stated-2"))
        form(raw, "fact-stated-1")["text"] = "{by} confirms that the {about} is {value}."

    with pytest.raises(RenderRefused) as refused:
        render(PLAN, CATALOGUE, bank_with(tmp_path, drop_subject), STATED)
    first = refused.value.refusals[0]
    assert first.code == "anchor" and first.item.endswith(".doc")
    assert "is missing" in first.detail and "fact-stated-1" in first.detail


def test_a_paraphrased_form_that_holds_the_stated_sentence_is_an_explicit_form(
    tmp_path: Path,
) -> None:
    def embed_the_stated_sentence(raw: dict[str, Any]) -> None:
        for n in (1, 2):
            stated_text = form(raw, "decision-stated-1")["text"]
            form(raw, f"decision-paraphrased-{n}")["text"] = f"{stated_text} Again {n}."

    assert refused_codes(bank_with(tmp_path, embed_the_stated_sentence)) == {"explicit-form"}


def test_a_probe_that_can_only_use_a_form_of_its_own_thread_shares_a_surface_form(
    tmp_path: Path,
) -> None:
    def one_form_for_both(raw: dict[str, Any]) -> None:
        raw["forms"].remove(form(raw, "probe-state-1"))
        shared = form(raw, "question-stated-1")
        shared["serves"], shared["text"] = (
            ["statement.question", "probe.state"],
            "Which {about} for {subject}?",
        )

    with pytest.raises(RenderRefused) as refused:
        render(PLAN, CATALOGUE, bank_with(tmp_path, one_form_for_both), STATED)
    assert [r.code for r in refused.value.refusals] == ["shared-form"]
    assert (
        "t22" in refused.value.refusals[0].item
        and "question-stated-1" in refused.value.refusals[0].detail
    )


def test_a_bank_of_another_split_cannot_render_a_world() -> None:
    train = plan_world(RoundSpec(SLICE_ONE), CATALOGUE, PACK, VOCABULARY, "train", 7)
    assert refused_codes(BANK, train, STATED) == {"wrong-bank"}


def test_a_text_that_holds_a_value_it_must_not_hold_is_named() -> None:
    assert anchor_problems("Note, 2025-03-14\nThe limit is 5.", ["2025-03-14", "5"], ["4"]) == []
    problems = anchor_problems(
        "Note, 2025-03-14\nThe limit was 4, now 5.", ["2025-03-14", "9"], ["4"]
    )
    assert problems == ["`9` is missing", "`4` is a value that the document must not hold"]


def test_the_loader_refuses_a_form_that_serves_nothing_a_repeated_text_and_an_unbuilt_level(
    tmp_path: Path,
) -> None:
    def break_three(raw: dict[str, Any]) -> None:
        form(raw, "fact-stated-1")["serves"] = ["statement.no-such-kind"]
        form(raw, "fact-stated-2")["text"] = form(raw, "decision-stated-1")["text"]
        form(raw, "result-stated-1")["level"] = "implied"

    with pytest.raises(BankRefused) as refused:
        bank_with(tmp_path, break_three)
    assert {(r.code, r.item) for r in refused.value.refusals} == {
        ("unknown-reference", "form fact-stated-1"),
        ("duplicate-pattern", "form decision-stated-1"),
        ("undeclared-value", "form result-stated-1"),
    }


def test_relation_snapshots_preserve_original_text_and_never_repeat_endpoint_content() -> None:
    """Relation evidence must not restate an implicit statement at the stated level."""
    from membench.ids import sentinel, sentinels_in, stable_id

    from exomem.markdown_relations import parse_markdown_relations
    from exomem.semantic_units import parse_semantic_units

    rendering = render(PLAN, CATALOGUE, BANK, "unmarked")
    world = PLAN.world()
    for artefact in world.artefacts.values():
        document = rendering.documents[artefact.id]
        assert not sentinels_in(document.text.splitlines()[0])
        for line in document.lines:
            if line.ref in world.statements:
                token = sentinel(stable_id("SRC", line.ref, digest_chars=64))
                assert f"{line.text} {token}" in document.text.splitlines()
        units = parse_semantic_units(document.text, path="source.txt")
        relations = parse_markdown_relations(document.text)
        assert not units.units and not units.errors
        assert not relations.relations and not relations.errors
        for ref in artefact.relations:
            assert artefact.by is None and document.level == STATED
            if isinstance(ref, core.EdgeRef):
                assert artefact.at == max(world.statements[s].at for s in (ref.source, ref.target))
                assert document.lines[0].text == next(
                    f.text for f in BANK.forms if f.id == document.forms[0]
                ).format(
                    source=sentinel(stable_id("SRC", ref.source, digest_chars=64)),
                    target=sentinel(stable_id("SRC", ref.target, digest_chars=64)),
                )
                for endpoint in (ref.source, ref.target):
                    original = rendering.documents[f"{endpoint}.doc"]
                    assert original.lines[0].text not in document.text
            else:
                assert artefact.at == week_date(1)
                assert PLAN.backbone.context_names[ref.child] in document.lines[0].text
                assert (
                    PLAN.backbone.context_names[world.contexts[ref.child].parent]
                    in document.lines[0].text
                )


def test_a_removed_relation_sentence_fails_source_coverage_even_when_references_remain(
    monkeypatch: Any,
) -> None:
    """Opaque refs alone do not convey support, defeat or context containment."""
    from benchmarks.membench.world.render import _Renderer

    original = _Renderer.document

    def omit(self: Any, artefact: Any, level: str) -> Any:
        document, items = original(self, artefact, level)
        if artefact.relations:
            anchors = " ".join(a for item in items for a in item.anchors)
            document = dataclasses.replace(
                document, text=document.text.splitlines()[0] + "\n" + anchors + "\n"
            )
        return document, items

    monkeypatch.setattr(_Renderer, "document", omit)
    assert "source-coverage" in refused_codes(BANK, level=STATED)


def test_edge_endpoints_in_one_original_have_distinct_citations_on_their_actual_statement_lines(
    monkeypatch: Any,
) -> None:
    """A shared document must preserve the direction of a link between its two statement records."""
    from membench.ids import sentinels_in

    from benchmarks.membench.world.manifest import artefact_hashes
    from benchmarks.membench.world.mechanisms import Oracle
    from benchmarks.membench.world.proof import Prover, artefacts
    from benchmarks.membench.world.validator import validate

    plan = dataclasses.replace(PLAN, threads=(), facts=())
    world = core.World(plan.name, VOCABULARY)
    plan.backbone.declare(world)
    about = next(iter(plan.backbone.attributes))
    first, second = plan.backbone.values(plan.backbone.attributes[about].schema.domain)[:2]
    holders = plan.backbone.holders()
    at = week_date(1)
    world.op_state("source-record", by=holders[0], about=about, value=first, kind="fact", at=at)
    world.op_state("target-record", by=holders[1], about=about, value=second, kind="fact", at=at)
    world.op_support(None, **{"from": "source-record", "to": "target-record"})
    shared = world.add_artefact(
        "shared-original",
        at,
        carries=("source-record", "target-record"),
        kind="note",
        by=holders[0],
    )
    ref = core.EdgeRef("source-record", "target-record", None)
    link = world.add_artefact("link-snapshot", at, kind="register", relations=(ref,))
    assert world.resolve_artefact(ref.source) == world.resolve_artefact(ref.target) == shared
    assert validate(world) == []
    assert {a["id"] for a in artefact_hashes(world)} == {shared.id, link.id}
    monkeypatch.setattr(plan, "world", lambda: world)
    rendering = render(plan, CATALOGUE, BANK, STATED)
    original = rendering.documents[shared.id]
    relation = rendering.documents[link.id]
    lines = {line.ref: line for line in original.lines}
    assert len(sentinels_in(relation.lines[0].text)) == 2
    source, target = core.source_ref(ref.source), core.source_ref(ref.target)
    assert source != target
    for endpoint, token in ((ref.source, source), (ref.target, target)):
        assert f"{lines[endpoint].text} {token}" in original.text.splitlines()
        assert token not in lines[endpoint].text
    assert not sentinels_in(original.text.splitlines()[0])
    form = next(f for f in BANK.forms if f.id == relation.forms[0])
    assert relation.lines[0].text == form.text.format(source=source, target=target)
    assert ref.source not in relation.text and ref.target not in relation.text
    selected = Prover(Oracle(world)).expand((ref.target,), at=at)
    assert set(artefacts(world, selected, at=at)) == {shared.id, link.id}
