"""Destination-only derivative adapters for governed consolidation."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import numpy as np

from .. import (
    access,
    claims,
    due_state,
    embeddings,
    epistemic_graph,
    extract,
    freshness,
    index_paths,
    lexstore,
    media_worker,
    memory_refs,
    recall_policy,
    scene_frames,
    semantic_index,
)
from ..kbdir import kb_dirname
from . import consolidation_rebuild, projections

DERIVATIVE_ARTIFACT_SCHEMA = "exomem.consolidation-derivative-artifact/v1"
MEDIA_REBUILD_TIMEOUT_SECONDS = 900.0

_ARTIFACT_DOMAIN = DERIVATIVE_ARTIFACT_SCHEMA.encode("ascii")
_PARENT_MEDIA = re.compile(r"(?m)^parent_media\s*:")

RebuildService = Callable[[Path], Mapping[str, object]]

__all__ = [
    "DERIVATIVE_ARTIFACT_SCHEMA",
    "MEDIA_REBUILD_TIMEOUT_SECONDS",
    "DestinationRebuildServices",
    "MediaCandidate",
    "destination_component_rebuilder",
    "production_destination_rebuild_services",
]


def _fail() -> NoReturn:
    raise consolidation_rebuild.DerivativeRebuildUnavailable from None


def _matrix_sha256(matrix: object) -> str:
    try:
        values = np.ascontiguousarray(matrix, dtype="<f4")
    except (TypeError, ValueError):
        _fail()
    return hashlib.sha256(values.tobytes()).hexdigest()


def _file_sha256(path: Path) -> str:
    """Hash one stable regular file without following a final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _fail()
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
        if identity(before) != identity(after):
            _fail()
        return digest.hexdigest()
    except consolidation_rebuild.DerivativeRebuildUnavailable:
        raise
    except OSError:
        _fail()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _artifact_fingerprint(
    *,
    component: str,
    canonical_census_digest: str,
    observation: Mapping[str, object],
) -> str:
    if type(observation) is not dict or any(type(key) is not str for key in observation):
        _fail()
    try:
        payload = projections.canonical_jcs(
            {
                "schema": DERIVATIVE_ARTIFACT_SCHEMA,
                "component": component,
                "canonical_census_digest": canonical_census_digest,
                "observation": observation,
            }
        )
    except Exception:  # noqa: BLE001 - one stable rebuild refusal
        _fail()
    framed = (
        len(_ARTIFACT_DOMAIN).to_bytes(4, "big")
        + _ARTIFACT_DOMAIN
        + len(payload).to_bytes(8, "big")
        + payload
    )
    return hashlib.sha256(framed).hexdigest()


@dataclass(frozen=True, slots=True)
class MediaCandidate:
    binary_path: Path
    sidecar_path: Path
    relative_path: str
    media_type: str


@dataclass(frozen=True, slots=True)
class DestinationRebuildServices:
    media: RebuildService
    lexical: RebuildService
    embedding: RebuildService
    semantic_unit: RebuildService
    graph: RebuildService
    freshness: RebuildService
    identity: RebuildService
    review: RebuildService


def _service_for(
    services: DestinationRebuildServices,
    component: str,
) -> RebuildService:
    values = {
        "media": services.media,
        "lexical": services.lexical,
        "embedding": services.embedding,
        "semantic-unit": services.semantic_unit,
        "graph": services.graph,
        "freshness": services.freshness,
        "identity": services.identity,
        "review": services.review,
    }
    selected = values.get(component)
    if selected is None:
        _fail()
    return selected


def destination_component_rebuilder(
    *,
    services: DestinationRebuildServices | None = None,
) -> Callable[
    [str, consolidation_rebuild.DerivativeRebuildContext],
    consolidation_rebuild.DerivativeRebuildTerminal,
]:
    """Return the closed destination-only adapter used by the saga coordinator."""

    selected_services = services or production_destination_rebuild_services()

    def rebuild(
        component: str,
        context: consolidation_rebuild.DerivativeRebuildContext,
    ) -> consolidation_rebuild.DerivativeRebuildTerminal:
        if component not in consolidation_rebuild.DERIVATIVE_COMPONENTS:
            _fail()
        try:
            observation = _service_for(selected_services, component)(context.vault_root)
            fingerprint = _artifact_fingerprint(
                component=component,
                canonical_census_digest=context.canonical_census_digest,
                observation=observation,
            )
        except consolidation_rebuild.DerivativeRebuildUnavailable:
            raise
        except Exception:  # noqa: BLE001 - do not disclose component internals
            _fail()
        return consolidation_rebuild.DerivativeRebuildTerminal(
            schema=consolidation_rebuild.DERIVATIVE_REBUILD_TERMINAL_SCHEMA,
            component=component,
            canonical_census_digest=context.canonical_census_digest,
            artifact_fingerprint=fingerprint,
        )

    return rebuild


def _rebuild_lexical(vault_root: Path) -> dict[str, object]:
    index = lexstore.LexicalStore(vault_root)
    if index.rebuild_atomic() is not True:
        _fail()
    path = lexstore.lexical_path(vault_root)
    if not path.is_file():
        _fail()
    return {"database_sha256": _file_sha256(path), "published": True}


def _embedding_paths(metadata: object) -> list[str]:
    if not isinstance(metadata, list):
        _fail()
    values: list[str] = []
    for row in metadata:
        if (
            not isinstance(row, tuple)
            or len(row) != 2
            or type(row[0]) is not str
            or type(row[1]) is not int
            or row[1] < 0
        ):
            _fail()
        values.append(f"{row[0]}#{row[1]}")
    return values


def _rebuild_embedding(vault_root: Path) -> dict[str, object]:
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        _fail()
    index = embeddings.get_embedding_index(vault_root)
    prior_metadata, _prior_matrix = index.all_vectors()
    prior_paths = sorted({row[0] for row in prior_metadata})
    if prior_paths:
        index.purge_paths_if_present(prior_paths)
    rebuilt = index.rebuild_all()
    metadata, matrix = index.all_vectors()
    paths = _embedding_paths(metadata)
    if type(rebuilt) is not int or rebuilt < 0 or rebuilt != len(paths):
        _fail()
    return {
        "chunk_rows": len(paths),
        "matrix_sha256": _matrix_sha256(matrix),
        "paths": paths,
        "rebuilt_rows": rebuilt,
    }


def _semantic_states(vault_root: Path) -> dict[str, semantic_index.SemanticParentIndexState]:
    states: dict[str, semantic_index.SemanticParentIndexState] = {}
    for path in index_paths.iter_index_markdown(vault_root):
        if not index_paths.is_embeddable_path(path):
            continue
        relative = index_paths.rel_to_vault(vault_root, path)
        if relative is None or not access.is_indexable(vault_root, relative):
            continue
        try:
            state = semantic_index.build_parent_index_state(vault_root, path)
        except (OSError, UnicodeError, ValueError):
            _fail()
        if state.path in states:
            _fail()
        states[state.path] = state
    return states


def _rebuild_semantic_units(vault_root: Path) -> dict[str, object]:
    states = _semantic_states(vault_root)
    drift = semantic_index.audit_semantic_unit_sidecars(
        vault_root,
        states,
        include_lexical=True,
        include_vectors=True,
        include_graph=False,
    )
    if drift:
        _fail()
    parent_generations = sorted(
        f"{path}:{state.parent_generation}" for path, state in states.items()
    )
    unit_count = sum(
        unit.unit_ref is not None
        for state in states.values()
        for unit in state.document.units
    )
    return {
        "parent_generations": parent_generations,
        "parents": len(states),
        "semantic_units": unit_count,
    }


def _normalized_graph_value(value: object) -> object:
    if value is None:
        return {"none": True}
    if type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        return {"binary64": value.hex()}
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, (list, tuple)):
        return [_normalized_graph_value(item) for item in value]
    if isinstance(value, dict) and all(type(key) is str for key in value):
        return {key: _normalized_graph_value(value[key]) for key in sorted(value)}
    _fail()


def _rebuild_graph(vault_root: Path) -> dict[str, object]:
    if not epistemic_graph.graph_enabled():
        _fail()
    index = epistemic_graph.EpistemicGraphIndex(vault_root)
    report = index.rebuild_all()
    if type(report) is not dict or set(report) != {"indexed_files", "nodes", "edges"}:
        _fail()
    if any(type(report[key]) is not int or report[key] < 0 for key in report):
        _fail()
    topology = projections.canonical_jcs(
        {
            "nodes": _normalized_graph_value(index.nodes()),
            "edges": _normalized_graph_value(index.edges()),
        }
    )
    return {
        "edges": report["edges"],
        "indexed_files": report["indexed_files"],
        "nodes": report["nodes"],
        "topology_sha256": hashlib.sha256(topology).hexdigest(),
    }


def _media_candidates(vault_root: Path) -> tuple[MediaCandidate, ...]:
    kb = vault_root / kb_dirname()
    if not kb.is_dir():
        return ()
    candidates: list[MediaCandidate] = []
    for binary in media_worker.iter_kb_files(kb):
        media_type = extract.media_type_for(binary)
        if media_type not in {"image", "video"}:
            continue
        sidecar = binary.with_name(binary.name + ".md")
        if binary.is_symlink() or sidecar.is_symlink():
            _fail()
        if (
            not sidecar.is_file()
            or recall_policy.is_structured_only_path(vault_root, sidecar)
            or not recall_policy.is_recall_candidate(vault_root, sidecar)
        ):
            continue
        try:
            header = sidecar.read_text(encoding="utf-8")[:4096]
            relative = binary.resolve().relative_to(vault_root.resolve()).as_posix()
        except (OSError, UnicodeError, ValueError):
            _fail()
        if _PARENT_MEDIA.search(header):
            continue
        candidates.append(
            MediaCandidate(binary, sidecar, relative, media_type)
        )
    return tuple(sorted(candidates, key=lambda item: item.relative_path))


def _scene_file_observation(
    vault_root: Path,
    candidates: tuple[MediaCandidate, ...],
) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for candidate in candidates:
        if candidate.media_type != "video":
            continue
        directory = scene_frames.frames_dir_for(candidate.binary_path)
        if not directory.exists():
            continue
        if not directory.is_dir() or directory.is_symlink():
            _fail()
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file():
                _fail()
            frame_name = path.name.removesuffix(".md")
            if scene_frames.parse_frame_ts(frame_name) is None:
                _fail()
            try:
                relative = path.relative_to(vault_root).as_posix()
                size = path.stat().st_size
            except OSError:
                _fail()
            values.append(
                {"path": relative, "sha256": _file_sha256(path), "size": size}
            )
    return values


def _rebuild_media(vault_root: Path) -> dict[str, object]:
    candidates = _media_candidates(vault_root)
    index = embeddings.get_clip_index(vault_root)
    old_paths, _old_timestamps, _old_matrix = index.all_vectors()
    if old_paths:
        index.purge_paths_if_present(sorted(set(old_paths)))
    cleared = sum(
        scene_frames.clear_scene_frames(vault_root, item.binary_path)
        for item in candidates
        if item.media_type == "video"
    )
    if candidates and not embeddings.clip_enabled():
        _fail()
    if candidates:
        worker = media_worker.MediaWorker(vault_root, execution_mode="inline")
        worker.start()
        try:
            for item in candidates:
                worker.enqueue(
                    binary_path=item.binary_path,
                    sidecar_path=item.sidecar_path,
                    media_type=item.media_type,
                    do_ocr=False,
                    do_clip=True,
                )
            worker.join(timeout=MEDIA_REBUILD_TIMEOUT_SECONDS)
        finally:
            worker.stop()
    for item in candidates:
        ready = (
            index.has_frames(item.relative_path)
            if item.media_type == "video"
            else index.has(item.relative_path)
        )
        if not ready:
            _fail()
    paths, timestamps, matrix = index.all_vectors()
    expected = {item.relative_path for item in candidates}
    if set(paths) != expected or len(paths) != len(timestamps):
        _fail()
    frame_keys = [
        f"{path}#{'image' if timestamp is None else scene_frames.frame_timestamp_ms(timestamp)}"
        for path, timestamp in zip(paths, timestamps, strict=True)
    ]
    return {
        "candidate_paths": sorted(expected),
        "cleared_scene_files": cleared,
        "clip_rows": len(paths),
        "frame_keys": frame_keys,
        "matrix_sha256": _matrix_sha256(matrix),
        "scene_files": _scene_file_observation(vault_root, candidates),
    }


def _rebuild_freshness(vault_root: Path) -> dict[str, object]:
    result = freshness.rebaseline(vault_root)
    if type(result) is not dict or not result or any(
        type(scope) is not str or status is not True for scope, status in result.items()
    ):
        _fail()
    return {"scopes": sorted(result)}


def _rebuild_identity(vault_root: Path) -> dict[str, object]:
    result = memory_refs.ReferenceIndex(vault_root).rebuild_all()
    if (
        type(result) is not dict
        or set(result) != {"indexed", "duplicates", "malformed"}
        or any(type(result[key]) is not int or result[key] < 0 for key in result)
        or result["duplicates"]
        or result["malformed"]
    ):
        _fail()
    return dict(result)


def _rebuild_review(vault_root: Path) -> dict[str, object]:
    payload = due_state.reconcile(vault_root)
    if due_state.load(vault_root) != payload:
        _fail()
    due_path = due_state.state_path(vault_root)
    if not due_path.is_file():
        _fail()
    due_digest = _file_sha256(due_path)
    if not claims.claim_level_enabled():
        index = claims.get_claim_index(vault_root)
        index.replace_all(
            [],
            identity=recall_policy.recall_policy_identity(vault_root),
        )
        return {
            "claim_index": "not-configured-cleared",
            "due_state_sha256": due_digest,
        }
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        _fail()
    index = claims.get_claim_index(vault_root)
    rebuilt = index.rebuild_all()
    metadata, matrix = index.all_claims()
    if type(rebuilt) is not int or rebuilt < 0 or rebuilt != len(metadata):
        _fail()
    paths: list[str] = []
    for row in metadata:
        if not isinstance(row, tuple) or len(row) != 4 or type(row[0]) is not str:
            _fail()
        paths.append(row[0])
    return {
        "claim_rows": len(paths),
        "due_state_sha256": due_digest,
        "matrix_sha256": _matrix_sha256(matrix),
        "paths": paths,
    }


def production_destination_rebuild_services() -> DestinationRebuildServices:
    return DestinationRebuildServices(
        media=_rebuild_media,
        lexical=_rebuild_lexical,
        embedding=_rebuild_embedding,
        semantic_unit=_rebuild_semantic_units,
        graph=_rebuild_graph,
        freshness=_rebuild_freshness,
        identity=_rebuild_identity,
        review=_rebuild_review,
    )
