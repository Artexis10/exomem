#!/usr/bin/env python
"""Deterministic synthetic benchmark for the referents envelope block."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from exomem import commands, epistemic_graph, public_artifact_privacy
from exomem import find as find_module
from exomem.entity_types import ENTITY_TYPES_BY_ID

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "tests" / "fixtures" / "referent_resolution" / "manifest.json"
FLOORS = {
    "set_accuracy": 0.9,
    "false_resolution_rate": 0.0,
    "abstention_accuracy": 1.0,
    "partial_accuracy": 1.0,
    "graph_incremental_value": 1,
}


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("manifest_version") != 1 or not isinstance(data.get("cases"), list):
        raise ValueError("unsupported referent benchmark manifest")
    return data


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entity(
    root: Path,
    entity_id: str,
    title: str,
    *,
    entity_type: str = "person",
    status: str = "active",
    relationship: str = "",
    tags: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
    attributes: tuple[tuple[str, str], ...] = (),
    body: str = "A synthetic benchmark identity.",
    relations: tuple[str, ...] = (),
) -> str:
    definition = ENTITY_TYPES_BY_ID.get(entity_type)
    if definition is None:
        raise ValueError(f"unknown entity type: {entity_type}")
    folder = definition.folder
    rel = f"Knowledge Base/Entities/{folder}/{entity_id}.md"
    optional = ""
    if relationship:
        optional += f"relationship: {relationship}\n"
    if tags:
        optional += f"tags: [{', '.join(tags)}]\n"
    else:
        optional += "tags: [synthetic]\n"
    if aliases:
        optional += f"aliases: [{', '.join(aliases)}]\n"
    for key, value in attributes:
        if key not in definition.optional_frontmatter:
            raise ValueError(f"unsupported {entity_type} attribute: {key}")
        optional += f"{key}: {value}\n"
    relation_text = ""
    if relations:
        relation_text = (
            "\n## Relations\n"
            + "\n".join(f"- relates_to [[{target[:-3]}]]" for target in relations)
            + "\n"
        )
    _write(
        root,
        rel,
        "---\n"
        "type: entity\n"
        f"title: {title}\n"
        f"entity_type: {entity_type}\n"
        f"status: {status}\n"
        f"{optional}"
        "updated: 2026-08-01\n"
        "---\n\n"
        f"# {title}\n\n{body}\n{relation_text}",
    )
    return rel


def _note(
    root: Path,
    note_id: str,
    body: str,
    *,
    status: str = "active",
    relations: tuple[str, ...] = (),
) -> str:
    rel = f"Knowledge Base/Notes/Research/{note_id}.md"
    relation_text = ""
    if relations:
        relation_text = (
            "\n## Relations\n"
            + "\n".join(f"- about_entity [[{target[:-3]}]]" for target in relations)
            + "\n"
        )
    _write(
        root,
        rel,
        "---\n"
        "type: research-note\n"
        f"title: Synthetic topic {note_id}\n"
        f"status: {status}\n"
        "updated: 2026-08-01\n"
        "---\n\n"
        f"# Synthetic topic {note_id}\n\n{body}\n{relation_text}",
    )
    return rel


def _render_cases(root: Path) -> dict[str, str]:
    paths: dict[str, str] = {}

    a_topic = _note(
        root,
        "a-aerian",
        (
            "Who are my two aerian friends? Seasonal routes were compared across "
            "a synthetic archive with neutral measurements."
        ),
    )
    paths["a-one"] = _entity(
        root,
        "a-one",
        "Aven Sol",
        relationship="friend",
        tags=("aerian",),
        body="An aerian identity in the synthetic benchmark.",
        relations=(a_topic,),
    )
    paths["a-two"] = _entity(
        root,
        "a-two",
        "Bex Tor",
        relationship="friend",
        tags=("aerian",),
        body="Another aerian identity in the synthetic benchmark.",
        relations=(a_topic,),
    )
    for index in range(12):
        _note(
            root,
            f"friend-noise-{index:02d}",
            "My two friends discussed two friend routes in a synthetic archive.",
        )

    paths["b-one"] = _entity(
        root,
        "b-one",
        "Cira Venn",
        relationship="colleague",
        tags=("tundran",),
        body="A tundran colleague working on synthetic instruments.",
    )

    c_org = _entity(
        root,
        "c-org",
        "Orbel Array",
        entity_type="organization",
        body="An observatory colleague network for synthetic sky measurements.",
    )
    paths["c-one"] = _entity(
        root, "c-one", "Daro Quen", relationship="colleague", relations=(c_org,)
    )

    paths["d-one"] = _entity(root, "d-one", "Velyn Rook", aliases=("Velix",))
    paths["d-two"] = _entity(
        root,
        "d-two",
        "Neral Pike",
        relationship="friend",
        tags=("zephyric",),
    )

    e_topic = _note(root, "e-pelagic", "Two pelagic friends discussed currents.")
    paths["e-one"] = _entity(
        root, "e-one", "Eris Noll", relationship="friend", tags=("pelagic",), relations=(e_topic,)
    )

    paths["f-one"] = _entity(
        root,
        "f-one",
        "Fara Wex",
        relationship="colleague",
        tags=("ember",),
        body="My one ember colleague in the synthetic trial.",
    )
    paths["f-two"] = _entity(
        root,
        "f-two",
        "Garo Yul",
        relationship="colleague",
        tags=("ember",),
        body="My one ember colleague in another synthetic trial.",
    )

    h_active = _note(
        root, "h-active", "Two old-harbour friends were expected; one remains represented."
    )
    h_old = _note(
        root, "h-old", "An old-harbour friend from an obsolete account.", status="superseded"
    )
    paths["h-one"] = _entity(
        root,
        "h-one",
        "Hesa Zor",
        relationship="friend",
        tags=("old-harbour",),
        relations=(h_active,),
    )
    paths["h-old"] = _entity(
        root, "h-old", "Iven Cal", relationship="friend", tags=("old-harbour",), relations=(h_old,)
    )

    paths["i-one"] = _entity(root, "i-one", "Inara Quill", status="superseded")

    j_topic = _note(root, "j-crystal", "The crystal friend project records a social connection.")
    paths["j-one"] = _entity(
        root, "j-one", "Jora Pell", relationship="friend", relations=(j_topic,)
    )

    paths["k-one"] = _entity(
        root,
        "k-one",
        "Orvane Scope",
        entity_type="organization",
        tags=("observatory",),
    )
    k_topic = _note(
        root,
        "k-observatory",
        "The two observatory companies we evaluated supplied synthetic instruments.",
        relations=(paths["k-one"],),
    )
    paths["k-two"] = _entity(
        root,
        "k-two",
        "Pelune Array",
        entity_type="organization",
        tags=("observatory",),
        relations=(k_topic,),
    )

    l_topic = _note(
        root,
        "l-atlas",
        "The rendering library we picked for the atlas project handled synthetic maps.",
    )
    paths["l-one"] = _entity(
        root,
        "l-one",
        "Lumera Draw",
        entity_type="library",
        tags=("rendering", "atlas"),
        attributes=(("language", "Luma"),),
        relations=(l_topic,),
    )

    paths["m-one"] = _entity(
        root,
        "m-one",
        "Tariff Boundary",
        entity_type="decision",
        tags=("harbour", "tariff"),
        attributes=(("decision_status", "accepted"),),
    )
    _note(
        root,
        "m-tariff",
        "That decision about the harbour tariff fixed a synthetic boundary.",
        relations=(paths["m-one"],),
    )

    paths["n-one"] = _entity(
        root,
        "n-one",
        "Tidal Coupling",
        entity_type="concept",
        tags=("tide", "model"),
    )
    _note(
        root,
        "n-tide",
        "The two concepts behind the tide model explain a synthetic forecast.",
        relations=(paths["n-one"],),
    )
    paths["n-noise"] = _entity(
        root,
        "n-noise",
        "Tide Residue",
        entity_type="concept",
        tags=("tide",),
        body="A background concept with one matching attribute only.",
    )

    paths["o-person-one"] = _entity(
        root,
        "o-person-one",
        "Quen Loris",
        tags=("quay",),
        relationship="colleague",
    )
    paths["o-person-two"] = _entity(
        root,
        "o-person-two",
        "Ralen Voss",
        tags=("quay",),
        relationship="colleague",
    )
    _note(
        root,
        "o-library-negative",
        "The quay library we reviewed was discussed only by synthetic people.",
        relations=(paths["o-person-one"], paths["o-person-two"]),
    )
    return paths


def _corpus_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.md")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def render_fixture(manifest: dict[str, Any], root: Path) -> SimpleNamespace:
    root = Path(root)
    rng = random.Random(int(manifest["seed"]))
    id_to_path = _render_cases(root)
    for index in range(int(manifest["filler_notes"])):
        _note(
            root,
            f"filler-{index:02d}-{rng.randint(0, 9999):04d}",
            "Background synthetic material about neutral archive routines.",
        )
    for index in range(int(manifest["noise_people"])):
        key = f"noise-person-{index:02d}"
        id_to_path[key] = _entity(
            root,
            key,
            f"Synthetic Person {index:02d}",
            body="A background person identity with no case descriptor.",
        )
    for index in range(int(manifest["noise_organizations"])):
        key = f"noise-org-{index:02d}"
        id_to_path[key] = _entity(
            root,
            key,
            f"Synthetic Organization {index:02d}",
            entity_type="organization",
            body="A background organization with no case descriptor.",
        )
    return SimpleNamespace(
        root=root,
        id_to_path=dict(sorted(id_to_path.items())),
        corpus_hash=_corpus_hash(root),
    )


def scan_public_artifacts(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(Path(root).rglob("*.md")):
        label = path.relative_to(root).as_posix()
        findings.extend(
            str(finding) for finding in public_artifact_privacy.scan_artifact(path, label=label)
        )
    return findings


def _arm(vault: Path, case: dict[str, Any], *, graph: bool) -> dict[str, Any]:
    result = commands.op_find(
        vault,
        query=str(case["query"]),
        mode="hybrid",
        graph=graph,
        rerank=False,
        limit=15,
        include_timings=True,
    )
    block = result.get("referents") if isinstance(result, dict) else None
    if not isinstance(block, dict):
        block = {"status": "unresolved", "resolved": [], "candidates": []}
    expected_paths = sorted(case["_expected_paths"])
    resolved_paths = sorted(str(item.get("path") or "") for item in block.get("resolved", []))
    candidate_paths = sorted(
        str(item.get("path") or "") for item in block.get("candidates", [])
    )
    expected_status = str(case["status"])
    expected_unresolved = case.get("unresolved_count")
    actual_unresolved = block.get("unresolved_count")
    expected = (
        resolved_paths == expected_paths
        and block.get("status") == expected_status
        and (expected_unresolved is None or actual_unresolved == expected_unresolved)
    )
    stage = ((result.get("timings") or {}).get("stages") or {}).get("referents", {})
    return {
        "status": str(block.get("status") or "unresolved"),
        "resolved": resolved_paths,
        "candidates": candidate_paths,
        "unresolved_count": actual_unresolved,
        "reasons": dict(block.get("reasons") or {}),
        "expected": expected,
        "false_resolution": bool(set(resolved_paths) - set(expected_paths)),
        "referents_stage_ms": stage.get("ms"),
    }


def _run_benchmark(manifest_path: Path, *, work_root: Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    rendered = render_fixture(manifest, Path(work_root) / "referent-fixture")
    epistemic_graph.EpistemicGraphIndex(rendered.root).rebuild_all()
    case_results: list[dict[str, Any]] = []
    for raw_case in manifest["cases"]:
        case = dict(raw_case)
        case["_expected_paths"] = [rendered.id_to_path[key] for key in case["resolved"]]
        graph_on = _arm(rendered.root, case, graph=True)
        graph_off = _arm(rendered.root, case, graph=False)
        case_results.append(
            {
                "case_id": case["id"],
                "graph_required": bool(case.get("graph_required")),
                "graph_on": graph_on,
                "graph_off": graph_off,
            }
        )

    total = len(case_results)
    abstention_ids = {"F", "G", "I", "O"}
    partial_ids = {"E", "H", "N"}
    stage_samples = [
        float(case["graph_on"]["referents_stage_ms"])
        for case in case_results
        if isinstance(case["graph_on"]["referents_stage_ms"], (int, float))
    ]
    metrics = {
        "set_accuracy": sum(case["graph_on"]["expected"] for case in case_results) / total,
        "false_resolution_rate": sum(case["graph_on"]["false_resolution"] for case in case_results)
        / total,
        "abstention_accuracy": sum(
            case["graph_on"]["expected"]
            for case in case_results
            if case["case_id"] in abstention_ids
        )
        / len(abstention_ids),
        "partial_accuracy": sum(
            case["graph_on"]["expected"] for case in case_results if case["case_id"] in partial_ids
        )
        / len(partial_ids),
        "graph_incremental_value": sum(
            int(case["graph_on"]["expected"] and not case["graph_off"]["expected"])
            for case in case_results
            if case["graph_required"]
        ),
    }
    timings = {
        "median_ms": statistics.median(stage_samples) if stage_samples else None,
        "p95_ms": sorted(stage_samples)[max(0, int(len(stage_samples) * 0.95) - 1)]
        if stage_samples
        else None,
    }
    return {
        "manifest_version": manifest["manifest_version"],
        "case_count": total,
        "corpus_hash": rendered.corpus_hash,
        "metrics": metrics,
        "referents_stage": timings,
        "_case_results": case_results,
    }


def run_benchmark(manifest_path: Path = DEFAULT_MANIFEST, *, work_root: Path) -> dict[str, Any]:
    """Run without leaking recall cache state into the caller's process."""
    find_module.clear_cache()
    try:
        return _run_benchmark(manifest_path, work_root=work_root)
    finally:
        find_module.clear_cache()


def public_report(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "manifest_version": report["manifest_version"],
        "case_count": report["case_count"],
        "corpus_hash": report["corpus_hash"],
        "metrics": report["metrics"],
        "referents_stage": report["referents_stage"],
        "floors": FLOORS,
    }


def _passes(report: dict[str, Any]) -> bool:
    metrics = report["metrics"]
    return (
        metrics["set_accuracy"] >= FLOORS["set_accuracy"]
        and metrics["false_resolution_rate"] <= FLOORS["false_resolution_rate"]
        and metrics["abstention_accuracy"] >= FLOORS["abstention_accuracy"]
        and metrics["partial_accuracy"] >= FLOORS["partial_accuracy"]
        and metrics["graph_incremental_value"] >= FLOORS["graph_incremental_value"]
    )


def _markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    timing = report["referents_stage"]
    return "\n".join(
        [
            "# Referent resolution benchmark",
            "",
            f"Cases: {report['case_count']}",
            f"Set accuracy: {metrics['set_accuracy']:.3f}",
            f"False-resolution rate: {metrics['false_resolution_rate']:.3f}",
            f"Abstention accuracy: {metrics['abstention_accuracy']:.3f}",
            f"Partial accuracy: {metrics['partial_accuracy']:.3f}",
            f"Graph incremental value: {metrics['graph_incremental_value']}",
            f"Referents stage median/p95 ms: {timing['median_ms']:.3f}/{timing['p95_ms']:.3f}",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    env_names = ("EXOMEM_DISABLE_EMBEDDINGS", "EXOMEM_DISABLE_CLIP")
    previous_env = {name: os.environ.get(name) for name in env_names}
    try:
        for name in env_names:
            os.environ[name] = "1"
        with tempfile.TemporaryDirectory(prefix="exomem-referents-") as temp:
            report = run_benchmark(args.manifest, work_root=Path(temp))
    finally:
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    public = public_report(report)
    print(json.dumps(public, sort_keys=True, indent=2) if args.json else _markdown(public))
    return 0 if not args.check or _passes(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
