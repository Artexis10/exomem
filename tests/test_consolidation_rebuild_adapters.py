from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from exomem.governance import consolidation_rebuild
from exomem.governance import consolidation_rebuild_adapters as adapters


def _context(vault: Path) -> consolidation_rebuild.DerivativeRebuildContext:
    return consolidation_rebuild.DerivativeRebuildContext(
        vault_root=vault,
        canonical_census_digest="a" * 64,
    )


def test_closed_adapter_set_receives_only_the_destination_root(tmp_path: Path) -> None:
    calls: list[tuple[str, Path]] = []

    def service(component: str):
        def rebuild(vault_root: Path) -> dict[str, object]:
            calls.append((component, vault_root))
            return {"component": component, "rows": 1}

        return rebuild

    services = adapters.DestinationRebuildServices(
        media=service("media"),
        lexical=service("lexical"),
        embedding=service("embedding"),
        semantic_unit=service("semantic-unit"),
        graph=service("graph"),
        freshness=service("freshness"),
        identity=service("identity"),
        review=service("review"),
    )
    rebuild = adapters.destination_component_rebuilder(services=services)
    context = _context(tmp_path / "destination")

    terminals = [rebuild(component, context) for component in consolidation_rebuild.DERIVATIVE_COMPONENTS]

    assert calls == [
        (component, context.vault_root)
        for component in consolidation_rebuild.DERIVATIVE_COMPONENTS
    ]
    assert [terminal.component for terminal in terminals] == list(
        consolidation_rebuild.DERIVATIVE_COMPONENTS
    )
    assert all(terminal.canonical_census_digest == "a" * 64 for terminal in terminals)
    assert len({terminal.artifact_fingerprint for terminal in terminals}) == len(terminals)
    assert not hasattr(services, "source_root")
    assert not hasattr(services, "source_database")


def test_evidence_fingerprint_binds_component_census_and_observation(tmp_path: Path) -> None:
    services = adapters.DestinationRebuildServices(
        **{
            field: (lambda _root: {"rows": 1})
            for field in (
                "media",
                "lexical",
                "embedding",
                "semantic_unit",
                "graph",
                "freshness",
                "identity",
                "review",
            )
        }
    )
    rebuild = adapters.destination_component_rebuilder(services=services)
    first = rebuild("lexical", _context(tmp_path))
    second = rebuild(
        "lexical",
        consolidation_rebuild.DerivativeRebuildContext(tmp_path, "b" * 64),
    )

    assert first.artifact_fingerprint != second.artifact_fingerprint
    with pytest.raises(consolidation_rebuild.DerivativeRebuildUnavailable):
        rebuild("unknown", _context(tmp_path))


def test_embedding_rebuild_replaces_rows_and_binds_vector_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Index:
        def __init__(self) -> None:
            self.purged: list[str] = []

        def all_vectors(self):
            if not self.purged:
                return [
                    ("Knowledge Base/old.md", 0),
                ], np.asarray([[0.0, 1.0]], dtype=np.float32)
            return [
                ("Knowledge Base/new.md", 0),
            ], np.asarray([[1.0, 2.0]], dtype=np.float32)

        def purge_paths_if_present(self, paths: list[str]) -> int:
            self.purged.extend(paths)
            return len(paths)

        def rebuild_all(self) -> int:
            return 1

    index = Index()
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(adapters.embeddings, "get_embedding_index", lambda _root: index)

    evidence = adapters._rebuild_embedding(tmp_path)

    assert index.purged == ["Knowledge Base/old.md"]
    assert evidence == {
        "chunk_rows": 1,
        "matrix_sha256": hashlib.sha256(
            np.asarray([[1.0, 2.0]], dtype=np.float32).tobytes()
        ).hexdigest(),
        "paths": ["Knowledge Base/new.md#0"],
        "rebuilt_rows": 1,
    }


def test_embedding_rebuild_refuses_when_the_model_lane_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    with pytest.raises(consolidation_rebuild.DerivativeRebuildUnavailable):
        adapters._rebuild_embedding(tmp_path)


def test_semantic_unit_adapter_refuses_any_sidecar_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(adapters, "_semantic_states", lambda _root: {"a.md": object()})
    monkeypatch.setattr(
        adapters.semantic_index,
        "audit_semantic_unit_sidecars",
        lambda *_args, **_kwargs: (object(),),
    )

    with pytest.raises(consolidation_rebuild.DerivativeRebuildUnavailable):
        adapters._rebuild_semantic_units(tmp_path)


def test_identity_adapter_refuses_duplicate_or_malformed_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Index:
        def rebuild_all(self) -> dict[str, int]:
            return {"indexed": 2, "duplicates": 1, "malformed": 0}

    monkeypatch.setattr(adapters.memory_refs, "ReferenceIndex", lambda _root: Index())

    with pytest.raises(consolidation_rebuild.DerivativeRebuildUnavailable):
        adapters._rebuild_identity(tmp_path)


def test_media_rebuild_purges_old_rows_then_requires_every_destination_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image = adapters.MediaCandidate(
        binary_path=tmp_path / "Knowledge Base/photo.jpg",
        sidecar_path=tmp_path / "Knowledge Base/photo.jpg.md",
        relative_path="Knowledge Base/photo.jpg",
        media_type="image",
    )
    video = adapters.MediaCandidate(
        binary_path=tmp_path / "Knowledge Base/talk.mp4",
        sidecar_path=tmp_path / "Knowledge Base/talk.mp4.md",
        relative_path="Knowledge Base/talk.mp4",
        media_type="video",
    )

    class Clip:
        def __init__(self) -> None:
            self.purged: list[str] = []

        def all_vectors(self):
            paths = ["Knowledge Base/stale.png"] if not self.purged else [
                image.relative_path,
                video.relative_path,
            ]
            frames = [None] if not self.purged else [None, 1.0]
            return paths, frames, np.ones((len(paths), 2), dtype=np.float32)

        def purge_paths_if_present(self, paths: list[str]) -> int:
            self.purged.extend(paths)
            return len(paths)

        def has(self, path: str) -> bool:
            return path == image.relative_path

        def has_frames(self, path: str) -> bool:
            return path == video.relative_path

    class Worker:
        def __init__(self, *_args, **_kwargs) -> None:
            self.enqueued: list[tuple[Path, str]] = []

        def start(self) -> None:
            pass

        def enqueue(self, *, binary_path: Path, media_type: str, **_kwargs) -> None:
            self.enqueued.append((binary_path, media_type))

        def join(self, timeout: float) -> None:
            assert timeout == adapters.MEDIA_REBUILD_TIMEOUT_SECONDS

        def stop(self) -> None:
            pass

    clip = Clip()
    worker = Worker()
    cleared: list[Path] = []
    monkeypatch.setattr(adapters, "_media_candidates", lambda _root: (image, video))
    monkeypatch.setattr(adapters.embeddings, "clip_enabled", lambda: True)
    monkeypatch.setattr(adapters.embeddings, "get_clip_index", lambda _root: clip)
    monkeypatch.setattr(adapters.media_worker, "MediaWorker", lambda *_a, **_kw: worker)
    monkeypatch.setattr(
        adapters.scene_frames,
        "clear_scene_frames",
        lambda _root, path: cleared.append(path) or 2,
    )

    evidence = adapters._rebuild_media(tmp_path)

    assert clip.purged == ["Knowledge Base/stale.png"]
    assert worker.enqueued == [
        (image.binary_path, "image"),
        (video.binary_path, "video"),
    ]
    assert cleared == [video.binary_path]
    assert evidence["candidate_paths"] == [image.relative_path, video.relative_path]
    assert evidence["cleared_scene_files"] == 2
    assert evidence["clip_rows"] == 2
    assert evidence["scene_files"] == []


def test_freshness_and_review_respect_their_configured_lanes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    due_path = tmp_path / "Knowledge Base/.due-state.json"
    due_path.parent.mkdir(parents=True)
    due_path.write_bytes(b'{"version":1}\n')

    class ClaimIndex:
        def replace_all(self, rows: list[object], *, identity: tuple[str, str]) -> None:
            assert rows == []
            assert identity == ("policy", "access")

    monkeypatch.setattr(
        adapters.freshness,
        "rebaseline",
        lambda _root: {"kb": True, "vault": True},
    )
    monkeypatch.setattr(adapters.claims, "claim_level_enabled", lambda: False)
    monkeypatch.setattr(adapters.claims, "get_claim_index", lambda _root: ClaimIndex())
    monkeypatch.setattr(
        adapters.recall_policy,
        "recall_policy_identity",
        lambda _root: ("policy", "access"),
    )
    monkeypatch.setattr(
        adapters.due_state,
        "reconcile",
        lambda _root: {"version": 1},
    )
    monkeypatch.setattr(adapters.due_state, "load", lambda _root: {"version": 1})
    monkeypatch.setattr(adapters.due_state, "state_path", lambda _root: due_path)

    assert adapters._rebuild_freshness(tmp_path) == {"scopes": ["kb", "vault"]}
    assert adapters._rebuild_review(tmp_path) == {
        "claim_index": "not-configured-cleared",
        "due_state_sha256": hashlib.sha256(b'{"version":1}\n').hexdigest(),
    }


def test_review_rebuild_refuses_when_due_state_did_not_persist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(adapters.claims, "claim_level_enabled", lambda: False)
    monkeypatch.setattr(
        adapters.claims,
        "get_claim_index",
        lambda _root: type(
            "Index",
            (),
            {"replace_all": lambda _self, _rows, *, identity: None},
        )(),
    )
    monkeypatch.setattr(
        adapters.recall_policy,
        "recall_policy_identity",
        lambda _root: ("policy", "access"),
    )
    monkeypatch.setattr(adapters.due_state, "reconcile", lambda _root: {"version": 1})
    monkeypatch.setattr(adapters.due_state, "load", lambda _root: None)

    with pytest.raises(consolidation_rebuild.DerivativeRebuildUnavailable):
        adapters._rebuild_review(tmp_path)


def test_real_model_free_adapters_rebuild_from_one_destination_vault(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault = tmp_path / "destination"
    kb = vault / "Knowledge Base"
    kb.mkdir(parents=True)
    (kb / "note.md").write_text(
        "---\n"
        "exomem_id: 11111111-1111-4111-8111-111111111111\n"
        "type: insight\n"
        "status: active\n"
        "---\n"
        "# Note\n\nDestination canonical bytes.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_GRAPH_INDEX", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_FRESHNESS_INDEX", raising=False)

    assert adapters._rebuild_media(vault)["candidate_paths"] == []
    assert adapters._rebuild_lexical(vault)["published"] is True
    assert adapters._rebuild_graph(vault)["indexed_files"] == 1
    assert adapters._rebuild_freshness(vault) == {"scopes": ["kb", "vault"]}
    assert adapters._rebuild_identity(vault) == {
        "indexed": 1,
        "duplicates": 0,
        "malformed": 0,
    }
    review = adapters._rebuild_review(vault)
    assert review["claim_index"] == "not-configured-cleared"
    assert len(str(review["due_state_sha256"])) == 64
