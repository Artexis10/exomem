#!/usr/bin/env python
"""Deterministic graph-value benchmark for Exomem and Basic Memory."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "tests" / "fixtures" / "graph_value" / "manifest.json"
COMMON_DIMENSIONS = (
    "one_hop_reachability",
    "multi_hop_reachability",
    "distractor_precision",
    "relation_type_fidelity",
    "direction_fidelity",
)
GOVERNED_DIMENSIONS = (
    "provenance_traceability",
    "supersession_handling",
    "semantic_block_precision",
)
# Exomem-only: a must-pass fixture invariant (score_run/dominance_report's
# fixture_failures check covers every dimension in ALL_DIMENSIONS), but
# DELIBERATELY excluded from COMMON_DIMENSIONS/GOVERNED_DIMENSIONS so it never
# enters dominance_report's Basic-Memory comparison `checks` loop — find()'s
# hit-envelope graph-provenance annotation has no build_context equivalent.
EXOMEM_ONLY_DIMENSIONS = ("recall_visibility",)
ALL_DIMENSIONS = (
    *COMMON_DIMENSIONS,
    "traversal_lens_filtering",
    *GOVERNED_DIMENSIONS,
    *EXOMEM_ONLY_DIMENSIONS,
)


@dataclass(frozen=True)
class EdgeFact:
    source: str
    target: str
    relation_type: str
    origin: str | None = None
    source_anchor: str | None = None


@dataclass(frozen=True)
class BlockFact:
    note: str
    block_id: str
    kind: str


@dataclass
class CaseResult:
    case_id: str
    dimension: str
    reached_nodes: list[str]
    edges: list[EdgeFact]
    blocks: list[BlockFact]
    statuses: dict[str, str]
    response_bytes: int
    latency_ms: float
    unsupported: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "dimension": self.dimension,
            "reached_nodes": sorted(self.reached_nodes),
            "edges": [asdict(item) for item in sorted(self.edges, key=_edge_sort_key)],
            "blocks": [asdict(item) for item in sorted(self.blocks, key=_block_sort_key)],
            "statuses": dict(sorted(self.statuses.items())),
            "response_bytes": self.response_bytes,
            "latency_ms": round(self.latency_ms, 3),
            "unsupported": dict(sorted(self.unsupported.items())),
        }


@dataclass
class ContenderRun:
    contender: str
    available: bool
    version: str
    revision: str
    corpus_hash: str
    mutation_safe: bool
    cases: dict[str, CaseResult] = field(default_factory=dict)
    unavailable_reason: str | None = None
    notes: list[str] = field(default_factory=list)
    renderer_parity: dict[str, str] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "contender": self.contender,
            "available": self.available,
            "version": self.version,
            "revision": self.revision,
            "corpus_hash": self.corpus_hash,
            "mutation_safe": self.mutation_safe,
            "unavailable_reason": self.unavailable_reason,
            "notes": list(self.notes),
            "renderer_parity": dict(sorted(self.renderer_parity.items())),
        }


@dataclass
class MetricResult:
    dimension: str
    numerator: int
    denominator: int
    ratio: float
    supported: bool
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    reason: str | None = None
    case_ids: list[str] = field(default_factory=list)
    failed_case_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "ratio": round(self.ratio, 6),
            "supported": self.supported,
            "missing": sorted(self.missing),
            "unexpected": sorted(self.unexpected),
            "reason": self.reason,
            "case_ids": sorted(self.case_ids),
            "failed_case_ids": sorted(self.failed_case_ids),
        }


@dataclass(frozen=True)
class RenderedCorpus:
    contender: str
    root: Path
    manifest_version: int
    id_to_path: dict[str, str]
    title_to_id: dict[str, str]
    id_to_permalink: dict[str, str]
    parity: dict[str, str]
    corpus_hash: str


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"manifest_version", "notes", "relations", "provenance", "blocks", "tasks"}
    missing = required - set(data)
    if missing:
        raise ValueError(f"graph manifest missing fields: {sorted(missing)}")
    note_ids = [str(note["id"]) for note in data["notes"]]
    task_ids = [str(task["id"]) for task in data["tasks"]]
    if len(note_ids) != len(set(note_ids)):
        raise ValueError("graph manifest note ids must be unique")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("graph manifest task ids must be unique")
    unknown_dimensions = {str(task["dimension"]) for task in data["tasks"]} - set(ALL_DIMENSIONS)
    if unknown_dimensions:
        raise ValueError(f"graph manifest has unknown dimensions: {sorted(unknown_dimensions)}")
    known = set(note_ids)
    for edge in [*data["relations"], *data["provenance"]]:
        if str(edge["from"]) not in known or str(edge["to"]) not in known:
            raise ValueError(f"graph manifest edge references unknown note: {edge}")
    return data


def render_exomem(manifest: dict[str, Any], root: Path) -> RenderedCorpus:
    root = Path(root)
    shutil.copytree(
        REPO_ROOT / "src" / "exomem" / "_scaffold",
        root / "Knowledge Base",
        dirs_exist_ok=True,
    )
    notes = {str(note["id"]): note for note in manifest["notes"]}
    id_to_path = {note_id: _exomem_path(note) for note_id, note in notes.items()}
    title_to_id = {str(note["title"]): note_id for note_id, note in notes.items()}
    id_to_permalink = dict(id_to_path)
    relations_by_source = _group_by_source(manifest["relations"])
    provenance_by_source = _group_by_source(manifest["provenance"])
    blocks_by_note = _group_by(manifest["blocks"], "note")

    for note_id, note in notes.items():
        rel_path = id_to_path[note_id]
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        frontmatter = [
            "---",
            f"type: {_exomem_page_type(note)}",
            f"status: {note['status']}",
            "created: 2026-01-01",
            "updated: 2026-01-01",
            f"benchmark_id: {note_id}",
        ]
        for edge in provenance_by_source.get(note_id, []):
            frontmatter.extend(
                [
                    f"{edge['channel']}:",
                    f'  - "[[{id_to_path[str(edge["to"])]}]]"',
                ]
            )
        if note.get("supersedes"):
            frontmatter.extend(
                [
                    "supersedes:",
                    f'  - "[[{id_to_path[str(note["supersedes"])]}]]"',
                ]
            )
        if note.get("superseded_by"):
            frontmatter.extend(
                [
                    "superseded_by:",
                    f'  - "[[{id_to_path[str(note["superseded_by"])]}]]"',
                ]
            )
        frontmatter.append("---")
        body = [*frontmatter, "", f"# {note['title']}", "", "## Overview", "", note["text"]]
        for block in blocks_by_note.get(note_id, []):
            relation_text = ", ".join(
                f"{edge['type']}: [[{id_to_path[str(edge['to'])]}]]"
                for edge in block.get("relations", [])
            )
            body.extend(
                [
                    "",
                    f"## {str(block['kind']).replace('_', ' ').title()}",
                    f"- id: {block['id']}",
                ]
            )
            if relation_text:
                body.append(f"- relations: {relation_text}")
            body.extend(["", str(block["text"])])
        note_relations = relations_by_source.get(note_id, [])
        if note_relations:
            body.extend(["", "## Relations"])
            body.extend(
                f"- {edge['type']} [[{id_to_path[str(edge['to'])]}]]" for edge in note_relations
            )
        path.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")

    profile_path = root / "Knowledge Base" / "_Schema" / "traversal-profiles.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        """schema_version: 1
profiles:
  benchmark-outgoing:
    extends: all
    direction: outgoing
  benchmark-dependency:
    extends: all
    remove_families: [support, contradiction, refinement, duplication, supersession, derivation, evidence, implementation, mitigation, causality, blocking, resolution, answer, question, use, observation, mention, entity, relation, link, citation, testing, ownership]
    add_families: [dependency]
    direction: outgoing
""",
        encoding="utf-8",
    )
    return RenderedCorpus(
        contender="exomem",
        root=root,
        manifest_version=int(manifest["manifest_version"]),
        id_to_path=id_to_path,
        title_to_id=title_to_id,
        id_to_permalink=id_to_permalink,
        parity={
            "note_relations": "canonical ## Relations bullets",
            "provenance": "sources/evidence frontmatter with governed origin metadata",
            "lifecycle": "status plus supersedes/superseded_by frontmatter",
            "blocks": "semantic blocks with anchored relation metadata",
            "scaffold": "generic Exomem schema scaffold; excluded from neutral note identities",
        },
        corpus_hash=corpus_hash(root),
    )


def render_basic_memory(manifest: dict[str, Any], root: Path) -> RenderedCorpus:
    root = Path(root)
    notes = {str(note["id"]): note for note in manifest["notes"]}
    id_to_path = {note_id: f"notes/{note_id}.md" for note_id in notes}
    title_to_id = {str(note["title"]): note_id for note_id, note in notes.items()}
    id_to_permalink = {note_id: note_id for note_id in notes}
    relations_by_source = _group_by_source(manifest["relations"])
    provenance_by_source = _group_by_source(manifest["provenance"])
    blocks_by_note = _group_by(manifest["blocks"], "note")

    for note_id, note in notes.items():
        path = root / id_to_path[note_id]
        path.parent.mkdir(parents=True, exist_ok=True)
        body = [
            "---",
            f"title: {note['title']}",
            f"type: {note['kind']}",
            f"permalink: {note_id}",
            f"status: {note['status']}",
            f"benchmark_id: {note_id}",
            "---",
            "",
            f"# {note['title']}",
            "",
            "## Observations",
            f"- [{note['kind']}] {note['text']}",
        ]
        for block in blocks_by_note.get(note_id, []):
            body.append(f"- [{block['kind']}] {block['text']}")
        edges = [*relations_by_source.get(note_id, []), *provenance_by_source.get(note_id, [])]
        if note.get("supersedes"):
            edges.append({"from": note_id, "to": str(note["supersedes"]), "type": "supersedes"})
        for block in blocks_by_note.get(note_id, []):
            edges.extend(
                {"from": note_id, "to": str(edge["to"]), "type": str(edge["type"])}
                for edge in block.get("relations", [])
            )
        if edges:
            body.extend(["", "## Relations"])
            body.extend(f"- {edge['type']} [[{notes[str(edge['to'])]['title']}]]" for edge in edges)
        path.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")

    return RenderedCorpus(
        contender="basic-memory",
        root=root,
        manifest_version=int(manifest["manifest_version"]),
        id_to_path=id_to_path,
        title_to_id=title_to_id,
        id_to_permalink=id_to_permalink,
        parity={
            "note_relations": "documented open relation bullets",
            "provenance": "closest native typed relation; no origin/source-anchor contract",
            "lifecycle": "custom status metadata plus supersedes relation",
            "blocks": "atomic observation plus page-level relation; no relation-bearing block anchor",
        },
        corpus_hash=corpus_hash(root),
    )


def corpus_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*.md")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def run_exomem_fixture(
    manifest: dict[str, Any], corpus: RenderedCorpus, *, revision: str = ""
) -> ContenderRun:
    from exomem import epistemic_graph

    before = corpus_hash(corpus.root)
    epistemic_graph.EpistemicGraphIndex(corpus.root).rebuild_all()
    cases: dict[str, CaseResult] = {}
    for task in manifest["tasks"]:
        if str(task["dimension"]) == "recall_visibility":
            cases[str(task["id"])] = _recall_visibility_case(task, corpus)
            continue
        started = time.perf_counter()
        payload = epistemic_graph.graph_context(
            corpus.root,
            path=corpus.id_to_path[str(task["seed"])],
            depth=int(task.get("depth", 1)),
            relation_types=list(task.get("relation_types") or []) or None,
            max_nodes=100,
            max_edges=200,
            traversal_profile=task.get("profile"),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        cases[str(task["id"])] = normalize_exomem_context(
            task, payload, corpus, elapsed_ms=elapsed_ms
        )
    after = corpus_hash(corpus.root)
    return ContenderRun(
        contender="exomem",
        available=True,
        version=_package_version("exomem"),
        revision=revision or git_revision(REPO_ROOT),
        corpus_hash=before,
        mutation_safe=before == after,
        cases=cases,
        notes=["model-free in-process shared graph leaf"],
        renderer_parity=corpus.parity,
    )


def _recall_visibility_case(task: dict[str, Any], corpus: RenderedCorpus) -> CaseResult:
    """EXOMEM-ONLY dimension: does plain `find()` (lexical + graph lanes) surface
    the typed neighbour in top-K WITH a graph-provenance annotation naming the
    authored relation? No Basic Memory equivalent — `build_context` has no
    hit-envelope/graph-annotation concept, so this never touches that path
    (see EXOMEM_ONLY_DIMENSIONS).

    "Embeddings off" is the ambient state this benchmark already runs under:
    `test_graph_value_benchmark.py` has no heavy-model dependency today, and
    `find()`'s vector lane self-degrades via ImportError when
    sentence-transformers/torch aren't installed — the same soft-fail path
    every other keyword-mode deployment relies on. No env-var toggle is needed
    (or reliable: EXOMEM_DISABLE_EMBEDDINGS only gates the writer path, not a
    query-time model that's already importable).
    """
    from exomem import find as find_module

    started = time.perf_counter()
    hits = find_module.find(
        corpus.root,
        query=str(task["query"]),
        limit=10,
        mode="hybrid",
        graph=True,
        prefer_compiled=False,
        prefer_active=False,
        temporal=False,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    seed_id = str(task["seed"])
    edges: set[EdgeFact] = set()
    reached: set[str] = set()
    path_to_id = {path: note_id for note_id, path in corpus.id_to_path.items()}
    for hit in hits:
        note_id = path_to_id.get(hit.path)
        if note_id is None or note_id == seed_id:
            continue
        annotation = hit.as_dict().get("graph")
        if annotation is None:
            continue
        reached.add(note_id)
        edges.add(
            EdgeFact(seed_id, note_id, str(annotation.get("relation_type") or ""))
        )

    encoded = json.dumps(
        {"query": task["query"], "hits": [h.path for h in hits]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return CaseResult(
        case_id=str(task["id"]),
        dimension="recall_visibility",
        reached_nodes=sorted(reached),
        edges=sorted(edges, key=_edge_sort_key),
        blocks=[],
        statuses={},
        response_bytes=len(encoded),
        latency_ms=elapsed_ms,
    )


def normalize_exomem_context(
    task: dict[str, Any], payload: dict[str, Any], corpus: RenderedCorpus, *, elapsed_ms: float
) -> CaseResult:
    path_to_id = {path: note_id for note_id, path in corpus.id_to_path.items()}
    node_by_key = {str(node["node_key"]): node for node in payload.get("nodes", [])}
    key_to_neutral: dict[str, str] = {}
    reached: set[str] = set()
    statuses: dict[str, str] = {}
    blocks: set[BlockFact] = set()
    seed = str(task["seed"])
    for key, node in node_by_key.items():
        note_id = path_to_id.get(str(node.get("path") or ""))
        if note_id is None:
            continue
        kind = str(node.get("kind") or "")
        if kind == "file":
            key_to_neutral[key] = note_id
            if note_id != seed:
                reached.add(note_id)
            metadata = node.get("metadata") or {}
            if metadata.get("status"):
                statuses[note_id] = str(metadata["status"])
        elif kind != "unresolved":
            anchor = str(node.get("anchor") or "")
            key_to_neutral[key] = f"{note_id}#{anchor}"
            blocks.add(BlockFact(note_id, anchor, kind))
    edges: set[EdgeFact] = set()
    for edge in payload.get("edges", []):
        source = key_to_neutral.get(str(edge.get("src_key") or ""))
        target = key_to_neutral.get(str(edge.get("dst_key") or ""))
        relation_type = edge.get("relation_type")
        if source and target and relation_type:
            edges.add(
                EdgeFact(
                    source,
                    target,
                    str(relation_type),
                    str(edge["origin"]) if edge.get("origin") else None,
                    str(edge["source_anchor"]) if edge.get("source_anchor") else None,
                )
            )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return CaseResult(
        case_id=str(task["id"]),
        dimension=str(task["dimension"]),
        reached_nodes=sorted(reached),
        edges=sorted(edges, key=_edge_sort_key),
        blocks=sorted(blocks, key=_block_sort_key),
        statuses=statuses,
        response_bytes=len(encoded),
        latency_ms=elapsed_ms,
    )


def normalize_basic_memory_context(
    task: dict[str, Any], payload: dict[str, Any], corpus: RenderedCorpus, *, elapsed_ms: float
) -> CaseResult:
    reached: set[str] = set()
    edges: set[EdgeFact] = set()
    seed = str(task["seed"])
    for result in payload.get("results", []):
        primary = result.get("primary_result") or {}
        primary_id = corpus.title_to_id.get(str(primary.get("title") or ""), seed)
        for related in result.get("related_results", []):
            item_type = related.get("type")
            if item_type == "entity":
                note_id = corpus.title_to_id.get(str(related.get("title") or ""))
                if note_id and note_id != seed:
                    reached.add(note_id)
            elif item_type == "relation":
                source = corpus.title_to_id.get(str(related.get("from_entity") or ""))
                target = corpus.title_to_id.get(
                    str(related.get("to_entity") or related.get("to_name") or "")
                )
                relation_type = related.get("relation_type")
                if source and target and relation_type:
                    edges.add(EdgeFact(source, target, str(relation_type)))
        if primary_id != seed:
            reached.add(primary_id)
    unsupported: dict[str, str] = {}
    dimension = str(task["dimension"])
    if dimension == "provenance_traceability":
        unsupported[dimension] = "build_context relations omit authored origin and source anchor"
    elif dimension == "supersession_handling":
        unsupported[dimension] = "build_context entities omit lifecycle status metadata"
    elif dimension == "semantic_block_precision":
        unsupported[dimension] = "observations cannot own graph relations or stable block anchors"
    elif dimension == "traversal_lens_filtering":
        unsupported[dimension] = "build_context exposes no relation-family traversal lens"
    elif dimension == "recall_visibility":
        unsupported[dimension] = (
            "find()/hit-envelope graph-provenance annotation has no build_context equivalent"
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return CaseResult(
        case_id=str(task["id"]),
        dimension=dimension,
        reached_nodes=sorted(reached),
        edges=sorted(edges, key=_edge_sort_key),
        blocks=[],
        statuses={},
        response_bytes=len(encoded),
        latency_ms=elapsed_ms,
        unsupported=unsupported,
    )


def score_run(manifest: dict[str, Any], run: ContenderRun) -> dict[str, MetricResult]:
    grouped: dict[str, list[MetricResult]] = {dimension: [] for dimension in ALL_DIMENSIONS}
    tasks = {str(task["id"]): task for task in manifest["tasks"]}
    for case_id, task in tasks.items():
        result = run.cases.get(case_id)
        if result is None:
            grouped[str(task["dimension"])].append(
                MetricResult(
                    str(task["dimension"]),
                    0,
                    1,
                    0.0,
                    False,
                    missing=[f"{case_id}:missing-result"],
                    reason=run.unavailable_reason or "contender returned no case result",
                    case_ids=[case_id],
                    failed_case_ids=[case_id],
                )
            )
        else:
            metric = score_case(task, result)
            metric.case_ids = [case_id]
            if metric.ratio < 1.0 or not metric.supported:
                metric.failed_case_ids = [case_id]
            grouped[result.dimension].append(metric)
    return {
        dimension: _aggregate_dimension(dimension, values)
        for dimension, values in grouped.items()
        if values
    }


def score_case(task: dict[str, Any], result: CaseResult) -> MetricResult:
    dimension = str(task["dimension"])
    if dimension in result.unsupported:
        return MetricResult(
            dimension,
            0,
            1,
            0.0,
            False,
            missing=[str(task["id"])],
            reason=result.unsupported[dimension],
        )
    if dimension in {"one_hop_reachability", "multi_hop_reachability"}:
        expected = set(map(str, task.get("expected_nodes") or []))
        reached = set(result.reached_nodes)
        return _metric_from_checks(
            dimension, expected, expected & reached, expected - reached, set()
        )
    if dimension == "distractor_precision":
        expected = set(map(str, task.get("expected_nodes") or []))
        reached = set(result.reached_nodes)
        denominator = max(1, len(reached))
        numerator = len(reached & expected)
        return MetricResult(
            dimension,
            numerator,
            denominator,
            numerator / denominator,
            True,
            missing=sorted(expected - reached),
            unexpected=sorted(reached - expected),
        )
    if dimension == "relation_type_fidelity":
        expected = [_expected_edge(edge) for edge in task.get("expected_edges") or []]
        checks = [
            any(
                {item.source, item.target} == {edge.source, edge.target}
                and item.relation_type == edge.relation_type
                for item in result.edges
            )
            for edge in expected
        ]
        metric = _metric_from_bools(dimension, checks, expected)
        metric.unexpected = [
            str(item)
            for expected_edge in expected
            for item in result.edges
            if {item.source, item.target} == {expected_edge.source, expected_edge.target}
            and item.relation_type != expected_edge.relation_type
        ]
        return metric
    if dimension == "direction_fidelity":
        expected = [_expected_edge(edge) for edge in task.get("expected_edges") or []]
        edge_checks = [
            any(item.source == edge.source and item.target == edge.target for item in result.edges)
            for edge in expected
        ]
        forbidden = list(map(str, task.get("forbidden_nodes") or []))
        forbidden_checks = [node not in result.reached_nodes for node in forbidden]
        checks = [*edge_checks, *forbidden_checks]
        denominator = max(1, len(checks))
        numerator = sum(checks)
        return MetricResult(
            dimension,
            numerator,
            denominator,
            numerator / denominator,
            True,
            missing=[
                str(edge) for edge, passed in zip(expected, edge_checks, strict=False) if not passed
            ],
            unexpected=[
                node
                for node, passed in zip(forbidden, forbidden_checks, strict=False)
                if not passed
            ],
        )
    if dimension == "traversal_lens_filtering":
        observed = {edge.relation_type for edge in result.edges}
        expected_types = list(map(str, task.get("expected_relation_types") or []))
        forbidden_types = list(map(str, task.get("forbidden_relation_types") or []))
        expected_checks = [item in observed for item in expected_types]
        forbidden_checks = [item not in observed for item in forbidden_types]
        checks = [*expected_checks, *forbidden_checks]
        denominator = max(1, len(checks))
        numerator = sum(checks)
        return MetricResult(
            dimension,
            numerator,
            denominator,
            numerator / denominator,
            True,
            missing=[
                item
                for item, passed in zip(expected_types, expected_checks, strict=False)
                if not passed
            ],
            unexpected=[
                item
                for item, passed in zip(forbidden_types, forbidden_checks, strict=False)
                if not passed
            ],
        )
    if dimension == "provenance_traceability":
        requirements = task.get("edge_requirements") or []
        checks = [
            _matching_edge_requirement(requirement, result.edges) for requirement in requirements
        ]
        return _metric_from_bools(dimension, checks, requirements)
    if dimension == "supersession_handling":
        expected_edges = [_expected_edge(edge) for edge in task.get("expected_edges") or []]
        checks = [_matching_expected_edge(edge, result.edges) for edge in expected_edges]
        labels: list[Any] = list(expected_edges)
        for note_id, status in (task.get("expected_statuses") or {}).items():
            checks.append(result.statuses.get(str(note_id)) == str(status))
            labels.append(f"{note_id}:{status}")
        return _metric_from_bools(dimension, checks, labels)
    if dimension == "semantic_block_precision":
        expected_blocks = [
            BlockFact(str(item["note"]), str(item["id"]), str(item["kind"]))
            for item in task.get("expected_blocks") or []
        ]
        expected_edges = [_expected_edge(edge) for edge in task.get("expected_edges") or []]
        checks = [block in result.blocks for block in expected_blocks]
        checks.extend(_matching_expected_edge(edge, result.edges) for edge in expected_edges)
        return _metric_from_bools(dimension, checks, [*expected_blocks, *expected_edges])
    if dimension == "recall_visibility":
        expected_edges = [_expected_edge(edge) for edge in task.get("expected_edges") or []]
        checks = [_matching_expected_edge(edge, result.edges) for edge in expected_edges]
        return _metric_from_bools(dimension, checks, expected_edges)
    raise ValueError(f"unsupported graph benchmark dimension: {dimension}")


def dominance_report(
    exomem_scores: dict[str, MetricResult],
    basic_scores: dict[str, MetricResult] | None,
) -> dict[str, Any]:
    fixture_failures = [
        dimension
        for dimension, metric in exomem_scores.items()
        if metric.ratio < 1.0 or not metric.supported
    ]
    if basic_scores is None:
        return {
            "dominant": None,
            "scope": "graph-dependent fixture tasks",
            "fixture_passed": not fixture_failures,
            "failed_criteria": [*fixture_failures, "basic-memory-unavailable"],
            "checks": [],
        }
    checks: list[dict[str, Any]] = []
    for dimension in COMMON_DIMENSIONS:
        left_metric = exomem_scores[dimension]
        right_metric = basic_scores[dimension]
        left = left_metric.ratio
        right = right_metric.ratio
        passed = left >= right
        checks.append(
            {
                "criterion": f"no-regression:{dimension}",
                "passed": passed,
                "exomem": round(left, 6),
                "basic_memory": round(right, 6),
                "cases": []
                if passed
                else sorted(set(left_metric.failed_case_ids or left_metric.case_ids)),
            }
        )
    for dimension in GOVERNED_DIMENSIONS:
        left_metric = exomem_scores[dimension]
        right_metric = basic_scores[dimension]
        left = left_metric.ratio
        right = right_metric.ratio
        passed = left > right
        checks.append(
            {
                "criterion": f"strict-win:{dimension}",
                "passed": passed,
                "exomem": round(left, 6),
                "basic_memory": round(right, 6),
                "cases": [] if passed else sorted(set(left_metric.case_ids)),
            }
        )
    failed = [item["criterion"] for item in checks if not item["passed"]]
    failed.extend(f"fixture:{dimension}" for dimension in fixture_failures)
    return {
        "dominant": not failed,
        "scope": "graph-dependent fixture tasks",
        "fixture_passed": not fixture_failures,
        "failed_criteria": failed,
        "checks": checks,
    }


def build_report(
    manifest: dict[str, Any],
    exomem_run: ContenderRun,
    basic_run: ContenderRun | None = None,
) -> dict[str, Any]:
    exomem_scores = score_run(manifest, exomem_run)
    basic_scores = score_run(manifest, basic_run) if basic_run and basic_run.available else None
    return {
        "report_version": 1,
        "manifest_version": int(manifest["manifest_version"]),
        "claim_scope": "graph-dependent fixture tasks only",
        "fairness": {
            "one_manifest": True,
            "native_markdown_renderers": True,
            "persistent_mcp_direct_mode": True,
            "model_free": True,
            "weighted_aggregate": False,
            "latency_is_informational": True,
        },
        "contenders": {
            "exomem": exomem_run.public_dict(),
            "basic_memory": (
                basic_run.public_dict()
                if basic_run
                else {
                    "contender": "basic-memory",
                    "available": False,
                    "unavailable_reason": "run with --direct and provide a checkout or executable",
                }
            ),
        },
        "dimensions": {
            "exomem": {key: value.as_dict() for key, value in sorted(exomem_scores.items())},
            "basic_memory": (
                {key: value.as_dict() for key, value in sorted(basic_scores.items())}
                if basic_scores
                else None
            ),
        },
        "dominance": dominance_report(exomem_scores, basic_scores),
        "efficiency": {
            "exomem": _efficiency(exomem_run),
            "basic_memory": _efficiency(basic_run) if basic_run and basic_run.available else None,
        },
        "reproduce": {
            "fixture": "python scripts/graph_value_benchmark.py",
            "direct": "python scripts/graph_value_benchmark.py --direct --basic-memory-root ../basic-memory",
        },
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Graph value benchmark",
        "",
        f"Claim scope: **{report['claim_scope']}**",
        "",
        f"Report version: `{report['report_version']}`  ",
        f"Manifest version: `{report['manifest_version']}`",
        "",
        "No weighted aggregate is used. Correctness dimensions remain independent.",
        "",
        "## Contenders",
        "",
        "| Contender | Available | Version | Revision | Corpus hash | Markdown unchanged |",
        "|---|---:|---|---|---|---:|",
    ]
    for key in ("exomem", "basic_memory"):
        item = report["contenders"][key]
        lines.append(
            "| {name} | {available} | {version} | {revision} | {corpus_hash} | {safe} |".format(
                name=item.get("contender", key),
                available="yes" if item.get("available") else "no",
                version=item.get("version") or "—",
                revision=item.get("revision") or "—",
                corpus_hash=item.get("corpus_hash") or "—",
                safe=("yes" if item.get("mutation_safe") else "no")
                if item.get("available")
                else "—",
            )
        )
    for key in ("exomem", "basic_memory"):
        item = report["contenders"][key]
        if not item.get("available") and item.get("unavailable_reason"):
            lines.extend(
                [
                    "",
                    f"{item.get('contender', key)} unavailable: {item['unavailable_reason']}",
                ]
            )
    lines.extend(
        [
            "",
            "## Fairness",
            "",
            "- One product-neutral manifest is rendered through native Markdown grammars.",
            "- Direct mode uses one persistent MCP session per contender.",
            "- Model features are disabled; latency and response size are informational.",
            "- Correctness dimensions are independent; no weighted aggregate is computed.",
            "",
            "| Representation | Exomem renderer | Basic Memory renderer |",
            "|---|---|---|",
        ]
    )
    exomem_parity = report["contenders"]["exomem"].get("renderer_parity") or {}
    basic_parity = report["contenders"]["basic_memory"].get("renderer_parity") or {}
    for concern in sorted(set(exomem_parity) | set(basic_parity)):
        lines.append(
            f"| {concern} | {exomem_parity.get(concern, '—')} | {basic_parity.get(concern, '—')} |"
        )
    lines.extend(
        [
            "",
            "## Independent dimensions",
            "",
            "| Dimension | Exomem | Basic Memory |",
            "|---|---:|---:|",
        ]
    )
    basic = report["dimensions"]["basic_memory"] or {}
    for dimension, metric in report["dimensions"]["exomem"].items():
        basic_metric = basic.get(dimension)
        right = (
            f"{basic_metric['numerator']}/{basic_metric['denominator']}" if basic_metric else "—"
        )
        lines.append(f"| {dimension} | {metric['numerator']}/{metric['denominator']} | {right} |")
    lines.extend(
        [
            "",
            "## Informational efficiency",
            "",
            "| Contender | Response bytes | Total latency (ms) |",
            "|---|---:|---:|",
        ]
    )
    for key in ("exomem", "basic_memory"):
        efficiency = report["efficiency"].get(key)
        name = report["contenders"][key].get("contender", key)
        if efficiency:
            lines.append(
                f"| {name} | {efficiency['response_bytes']} | {efficiency['latency_ms']} |"
            )
        else:
            lines.append(f"| {name} | — | — |")
    dominance = report["dominance"]
    lines.extend(
        [
            "",
            "## Dominance",
            "",
            f"Result: **{_dominance_label(dominance['dominant'])}**",
            "",
        ]
    )
    if dominance.get("checks"):
        lines.extend(
            [
                "| Criterion | Exomem | Basic Memory | Passed |",
                "|---|---:|---:|---:|",
            ]
        )
        for check in dominance["checks"]:
            lines.append(
                f"| {check['criterion']} | {check['exomem']:.3f} | "
                f"{check['basic_memory']:.3f} | {'yes' if check['passed'] else 'no'} |"
            )
        lines.append("")
    if dominance["failed_criteria"]:
        lines.append("Failed or unavailable criteria:")
        failed_checks = {
            item["criterion"]: item for item in dominance.get("checks", []) if not item["passed"]
        }
        for criterion in dominance["failed_criteria"]:
            cases = failed_checks.get(criterion, {}).get("cases") or []
            suffix = f" (cases: {', '.join(cases)})" if cases else ""
            lines.append(f"- `{criterion}`{suffix}")
    else:
        lines.append("All no-regression and strict governed-graph criteria passed.")
    lines.extend(
        [
            "",
            "## Reproduce",
            "",
            f"- Fixture: `{report['reproduce']['fixture']}`",
            f"- Direct: `{report['reproduce']['direct']}`",
            "",
        ]
    )
    return "\n".join(lines)


def unavailable_basic_memory(reason: str, corpus: RenderedCorpus) -> ContenderRun:
    return ContenderRun(
        contender="basic-memory",
        available=False,
        version="",
        revision="",
        corpus_hash=corpus.corpus_hash,
        mutation_safe=True,
        unavailable_reason=reason,
        renderer_parity=corpus.parity,
    )


def git_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # noqa: BLE001 - benchmark metadata is best-effort
        return "source"


def _group_by_source(values: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return _group_by(values, "from")


def _group_by(values: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        grouped.setdefault(str(value[key]), []).append(value)
    return grouped


def _exomem_path(note: dict[str, Any]) -> str:
    note_id = str(note["id"])
    if note["kind"] == "source":
        return f"Knowledge Base/Sources/Articles/{note_id}.md"
    if note["kind"] == "evidence":
        return f"Knowledge Base/Evidence/Cases/{note_id}.md"
    return f"Knowledge Base/Notes/Insights/{note_id}.md"


def _exomem_page_type(note: dict[str, Any]) -> str:
    if note["kind"] in {"source", "evidence"}:
        return str(note["kind"])
    return "insight"


def _expected_edge(value: dict[str, Any]) -> EdgeFact:
    return EdgeFact(
        str(value["from"]),
        str(value["to"]),
        str(value["type"]),
        str(value["origin"]) if value.get("origin") else None,
        str(value["source_anchor"]) if value.get("source_anchor") else None,
    )


def _matching_edge_requirement(value: dict[str, Any], edges: list[EdgeFact]) -> bool:
    expected = _expected_edge(value)
    return any(
        edge.source == expected.source
        and edge.target == expected.target
        and edge.relation_type == expected.relation_type
        and (expected.origin is None or edge.origin == expected.origin)
        and (expected.source_anchor is None or edge.source_anchor == expected.source_anchor)
        for edge in edges
    )


def _matching_expected_edge(expected: EdgeFact, edges: list[EdgeFact]) -> bool:
    return any(
        edge.source == expected.source
        and edge.target == expected.target
        and edge.relation_type == expected.relation_type
        and (expected.origin is None or edge.origin == expected.origin)
        and (expected.source_anchor is None or edge.source_anchor == expected.source_anchor)
        for edge in edges
    )


def _metric_from_checks(
    dimension: str,
    expected: set[str],
    matched: set[str],
    missing: set[str],
    unexpected: set[str],
) -> MetricResult:
    denominator = max(1, len(expected))
    numerator = len(matched)
    return MetricResult(
        dimension,
        numerator,
        denominator,
        numerator / denominator,
        True,
        missing=sorted(missing),
        unexpected=sorted(unexpected),
    )


def _metric_from_bools(dimension: str, checks: list[bool], labels: list[Any]) -> MetricResult:
    denominator = max(1, len(checks))
    numerator = sum(bool(item) for item in checks)
    missing = [str(label) for label, passed in zip(labels, checks, strict=False) if not passed]
    return MetricResult(
        dimension,
        numerator,
        denominator,
        numerator / denominator,
        True,
        missing=missing,
    )


def _aggregate_dimension(dimension: str, values: list[MetricResult]) -> MetricResult:
    numerator = sum(value.numerator for value in values)
    denominator = max(1, sum(value.denominator for value in values))
    supported = all(value.supported for value in values)
    reasons = sorted({value.reason for value in values if value.reason})
    return MetricResult(
        dimension,
        numerator,
        denominator,
        numerator / denominator,
        supported,
        missing=[item for value in values for item in value.missing],
        unexpected=[item for value in values for item in value.unexpected],
        reason="; ".join(reasons) if reasons else None,
        case_ids=sorted({item for value in values for item in value.case_ids}),
        failed_case_ids=sorted({item for value in values for item in value.failed_case_ids}),
    )


def _efficiency(run: ContenderRun | None) -> dict[str, float] | None:
    if run is None or not run.available or not run.cases:
        return None
    return {
        "response_bytes": sum(item.response_bytes for item in run.cases.values()),
        "latency_ms": round(sum(item.latency_ms for item in run.cases.values()), 3),
    }


def _edge_sort_key(edge: EdgeFact) -> tuple[str, str, str, str, str]:
    return (
        edge.source,
        edge.target,
        edge.relation_type,
        edge.origin or "",
        edge.source_anchor or "",
    )


def _block_sort_key(block: BlockFact) -> tuple[str, str, str]:
    return (block.note, block.block_id, block.kind)


def _dominance_label(value: bool | None) -> str:
    if value is True:
        return "EXOMEM DOMINATES"
    if value is False:
        return "NOT DOMINANT"
    return "DIRECT CONTENDER NOT RUN"


async def run_direct_comparison(
    manifest: dict[str, Any],
    exomem_corpus: RenderedCorpus,
    basic_corpus: RenderedCorpus,
    args: argparse.Namespace,
) -> tuple[ContenderRun, ContenderRun]:
    exomem_run = await _run_exomem_mcp(
        manifest,
        exomem_corpus,
        python=Path(args.exomem_python),
        timeout=float(args.request_timeout),
    )
    basic_run = await _run_basic_memory_mcp(
        manifest,
        basic_corpus,
        root=args.basic_memory_root,
        executable=args.basic_memory_executable,
        uv=str(args.uv),
        timeout=float(args.request_timeout),
    )
    return exomem_run, basic_run


async def _run_exomem_mcp(
    manifest: dict[str, Any],
    corpus: RenderedCorpus,
    *,
    python: Path,
    timeout: float,
) -> ContenderRun:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    from exomem import epistemic_graph

    before = corpus_hash(corpus.root)
    epistemic_graph.EpistemicGraphIndex(corpus.root).rebuild_all()
    state = corpus.root.parent / "exomem-state"
    state.mkdir(parents=True, exist_ok=True)
    env = _child_env(state)
    env.update(
        {
            "EXOMEM_VAULT_PATH": str(corpus.root),
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
            "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
            "EXOMEM_DISABLE_CLIP": "1",
            "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
            "EXOMEM_DISABLE_QUERY_LOG": "1",
            "EXOMEM_DISABLE_RANKING_CONFIG": "1",
            "EXOMEM_DISABLE_WARMUP": "1",
            "EXOMEM_DISABLE_FILE_WATCHER": "1",
            "EXOMEM_DISABLE_MODE_WATCH": "1",
            "EXOMEM_CONFIG_PATH": str(state / "config.json"),
            "EXOMEM_LOG_DIR": str(state / "logs"),
            "PYTHONPATH": str(REPO_ROOT / "src"),
        }
    )
    transport = StdioTransport(
        command=str(python),
        args=["-m", "exomem", "--transport", "stdio"],
        env=env,
        cwd=str(REPO_ROOT),
        keep_alive=False,
        log_file=state / "stdio.log",
    )
    cases: dict[str, CaseResult] = {}
    client = Client(transport, timeout=timeout, init_timeout=max(timeout, 60.0))
    async with asyncio.timeout(max(timeout * (len(manifest["tasks"]) + 4), 120.0)):
        async with client:
            tools = {tool.name for tool in await asyncio.wait_for(client.list_tools(), timeout)}
            if "connect_memory" not in tools:
                raise RuntimeError("Exomem MCP server does not expose connect_memory")
            for task in manifest["tasks"]:
                arguments: dict[str, Any] = {
                    "operation": "context",
                    "path": corpus.id_to_path[str(task["seed"])],
                    "depth": int(task.get("depth", 1)),
                    "max_nodes": 100,
                    "max_edges": 200,
                }
                if task.get("relation_types"):
                    arguments["relation_types"] = list(task["relation_types"])
                if task.get("profile"):
                    arguments["traversal_profile"] = str(task["profile"])
                started = time.perf_counter()
                payload = await _mcp_call(client, "connect_memory", arguments, timeout)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                graph = payload.get("graph", payload) if isinstance(payload, dict) else {}
                cases[str(task["id"])] = normalize_exomem_context(
                    task, graph, corpus, elapsed_ms=elapsed_ms
                )
    after = corpus_hash(corpus.root)
    return ContenderRun(
        contender="exomem",
        available=True,
        version=_package_version("exomem"),
        revision=git_revision(REPO_ROOT),
        corpus_hash=before,
        mutation_safe=before == after,
        cases=cases,
        notes=["persistent stdio MCP", "model features disabled"],
        renderer_parity=corpus.parity,
    )


async def _run_basic_memory_mcp(
    manifest: dict[str, Any],
    corpus: RenderedCorpus,
    *,
    root: Path | None,
    executable: Path | None,
    uv: str,
    timeout: float,
) -> ContenderRun:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    if executable is None and root is None:
        raise RuntimeError(
            "direct comparison requires --basic-memory-root or --basic-memory-executable"
        )
    if executable is not None and not executable.is_file():
        raise RuntimeError(f"Basic Memory executable not found: {executable}")
    if root is not None and not (root / "pyproject.toml").is_file():
        raise RuntimeError(f"Basic Memory checkout is invalid: {root}")

    state = corpus.root.parent / "basic-memory-state"
    home = state / "home"
    state.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    config = _basic_memory_config(corpus)
    (state / "config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    env = _child_env(home)
    env.update(
        {
            "BASIC_MEMORY_CONFIG_DIR": str(state),
            "BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED": "false",
            "BASIC_MEMORY_SYNC_CHANGES": "false",
            "BASIC_MEMORY_DISABLE_PERMALINKS": "true",
            "BASIC_MEMORY_ENSURE_FRONTMATTER_ON_SYNC": "false",
            "BASIC_MEMORY_FORCE_LOCAL": "true",
            "BASIC_MEMORY_EXPLICIT_ROUTING": "true",
        }
    )
    if executable is not None:
        command = str(executable)
        launcher_args: list[str] = []
        command_args = [
            "mcp",
            "--transport",
            "stdio",
            "--project",
            "graph-benchmark",
        ]
        cwd = state
        version = _command_version(executable)
        revision = git_revision(root) if root is not None else "installed"
    else:
        assert root is not None
        uv_path = shutil.which(uv) if not Path(uv).is_file() else uv
        if not uv_path:
            raise RuntimeError(
                "uv is required for --basic-memory-root; pass --uv /path/to/uv or "
                "--basic-memory-executable"
            )
        command = str(uv_path)
        launcher_args = [
            "run",
            "--frozen",
            "--project",
            str(root),
            "basic-memory",
        ]
        command_args = [
            *launcher_args,
            "mcp",
            "--transport",
            "stdio",
            "--project",
            "graph-benchmark",
        ]
        cwd = root
        version = _git_describe(root)
        revision = git_revision(root)

    before = corpus_hash(corpus.root)
    _index_basic_memory_corpus(
        command=command,
        launcher_args=launcher_args,
        env=env,
        cwd=cwd,
        project="graph-benchmark",
        timeout=timeout,
    )
    transport = StdioTransport(
        command=command,
        args=command_args,
        env=env,
        cwd=str(cwd),
        keep_alive=False,
        log_file=state / "stdio.log",
    )
    cases: dict[str, CaseResult] = {}
    client = Client(transport, timeout=timeout, init_timeout=max(timeout, 120.0))
    async with asyncio.timeout(max(timeout * (len(manifest["tasks"]) + 8), 180.0)):
        async with client:
            tools = {tool.name for tool in await asyncio.wait_for(client.list_tools(), timeout)}
            if "build_context" not in tools:
                raise RuntimeError("Basic Memory MCP server does not expose build_context")
            for task in manifest["tasks"]:
                arguments = {
                    "url": f"memory://{corpus.id_to_permalink[str(task['seed'])]}",
                    "project": "graph-benchmark",
                    "depth": int(task.get("depth", 1)),
                    "timeframe": None,
                    "page_size": 10,
                    "max_related": 200,
                }
                started = time.perf_counter()
                payload = await _mcp_call(client, "build_context", arguments, timeout)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                cases[str(task["id"])] = normalize_basic_memory_context(
                    task,
                    payload if isinstance(payload, dict) else {},
                    corpus,
                    elapsed_ms=elapsed_ms,
                )
    after = corpus_hash(corpus.root)
    return ContenderRun(
        contender="basic-memory",
        available=True,
        version=version,
        revision=revision,
        corpus_hash=before,
        mutation_safe=before == after,
        cases=cases,
        notes=[
            "explicit full filesystem index before measurement",
            "persistent stdio MCP",
            "semantic search disabled",
            "isolated config and SQLite state",
            "file mutation disabled",
        ],
        renderer_parity=corpus.parity,
    )


def _basic_memory_config(corpus: RenderedCorpus) -> dict[str, Any]:
    return {
        "env": "test",
        "projects": {"graph-benchmark": {"path": str(corpus.root), "mode": "local"}},
        "default_project": "graph-benchmark",
        "semantic_search_enabled": False,
        "default_search_type": "text",
        "sync_changes": False,
        "disable_permalinks": True,
        "ensure_frontmatter_on_sync": False,
        "auto_update": False,
        "log_level": "WARNING",
    }


def _index_basic_memory_corpus(
    *,
    command: str,
    launcher_args: list[str],
    env: dict[str, str],
    cwd: Path,
    project: str,
    timeout: float,
) -> None:
    """Populate Basic Memory's rebuildable DB before measuring graph queries."""
    try:
        result = subprocess.run(
            [
                command,
                *launcher_args,
                "reindex",
                "--full",
                "--search",
                "--project",
                project,
            ],
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(timeout, 60.0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Basic Memory corpus indexing failed to start") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        summary = detail[-1] if detail else f"exit {result.returncode}"
        raise RuntimeError(f"Basic Memory corpus indexing failed: {summary}")


async def _mcp_call(client: Any, name: str, arguments: dict[str, Any], timeout: float) -> Any:
    result = await asyncio.wait_for(client.call_tool(name, arguments), timeout=timeout)
    if getattr(result, "is_error", False):
        raise RuntimeError(f"MCP tool {name} returned an error: {result}")
    data = getattr(result, "data", None)
    if data is not None:
        return _unwrap_result(data)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return _unwrap_result(structured)
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text:
            try:
                return _unwrap_result(json.loads(text))
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f"MCP tool {name} returned no structured result")


def _unwrap_result(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict) and "result" in value:
        return value["result"]
    return value


def _child_env(home: Path) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONUTF8": "1",
        "NO_COLOR": "1",
    }
    if sys.platform == "win32":
        env["USERPROFILE"] = str(home)
        if os.environ.get("SYSTEMROOT"):
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def _command_version(executable: Path) -> str:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "installed"
    return (result.stdout or result.stderr).strip().splitlines()[0] or "installed"


def _git_describe(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "source"
    return result.stdout.strip() or "source"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--basic-memory-root", type=Path)
    parser.add_argument("--basic-memory-executable", type=Path)
    parser.add_argument("--exomem-python", default=sys.executable)
    parser.add_argument("--uv", default=shutil.which("uv") or "uv")
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--work-dir", type=Path)
    return parser.parse_args(argv)


def _execute(args: argparse.Namespace, manifest: dict[str, Any], work: Path) -> int:
    if args.direct and args.basic_memory_root is None and args.basic_memory_executable is None:
        raise ValueError("--direct requires --basic-memory-root or --basic-memory-executable")
    exomem_corpus = render_exomem(manifest, work / "exomem-vault")
    basic_corpus = render_basic_memory(manifest, work / "basic-memory-project")
    if args.direct:
        exomem_run, basic_run = asyncio.run(
            run_direct_comparison(manifest, exomem_corpus, basic_corpus, args)
        )
    else:
        exomem_run = run_exomem_fixture(manifest, exomem_corpus)
        basic_run = unavailable_basic_memory(
            "direct comparison not requested; pass --direct with --basic-memory-root or "
            "--basic-memory-executable",
            basic_corpus,
        )
    report = build_report(manifest, exomem_run, basic_run)
    rendered = render_markdown_report(report)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(rendered, encoding="utf-8")
    print(rendered)
    fixture_passed = bool(report["dominance"]["fixture_passed"])
    if not fixture_passed:
        return 1
    if args.direct and report["dominance"]["dominant"] is not True:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = load_manifest(args.manifest)
    if args.work_dir:
        work = args.work_dir.resolve()
        work.mkdir(parents=True, exist_ok=True)
        occupied = [
            name for name in ("exomem-vault", "basic-memory-project") if (work / name).exists()
        ]
        if occupied:
            raise ValueError(f"benchmark work directory is not empty: {occupied}")
        return _execute(args, manifest, work)
    with tempfile.TemporaryDirectory(prefix="exomem-graph-value-") as raw:
        return _execute(args, manifest, Path(raw))


if __name__ == "__main__":
    raise SystemExit(main())
