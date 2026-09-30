"""The recall sidecar records the vector space it holds, and nothing else reads it.

A sidecar written by one encoder is meaningless to another: two models' vectors
are not comparable even when their widths agree, and when the widths differ a
mixed matrix cannot even be built. So the sidecar records which encoder wrote
it (the model, the encoder's fingerprint when it was resident, and the width),
reads its width from that record instead of a module constant, refuses rows of
another width, and a query from an encoder it was not written by leaves the
vector lane out while the lexical lanes serve (on a personal server a sidecar
of another model is served by that model until it is re-embedded; see
tests/test_embedding_migration.py). A sidecar written before the record existed
was written by the English model at 768 dimensions: that is the only encoder
recall ever shipped.

Which sidecar serves is named by an active pointer beside it, so a new space
can be built next to the serving one and swapped in atomically. The claims
sidecar holds vectors in the same space and follows it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from exomem import claims, embedding_backend, embeddings, index_paths, readiness, recall_space
from exomem import find as find_module
from exomem.embedding_index import EmbeddingIndex
from exomem.kbdir import kb_dirname
from exomem.reserved_paths import PathDisposition, classify_logical

M3 = "BAAI/bge-m3"
BASE = "BAAI/bge-base-en-v1.5"
SPACE_A = "fake/space-a"
SPACE_B = "fake/space-b"
_FAKE_DIMS = {SPACE_A: 8, SPACE_B: 12}


def _unit_rows(seed: int, n: int, dim: int) -> np.ndarray:
    rows = np.random.default_rng(seed).standard_normal((n, dim)).astype(np.float32)
    rows /= np.linalg.norm(rows, axis=1, keepdims=True)
    return rows


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    (root / kb_dirname()).mkdir(parents=True)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    # No encoder is resident unless a test says so: a real one an earlier test
    # loaded would stamp its fingerprint on every record.
    monkeypatch.setattr(embeddings, "_MODEL", None)
    embeddings.clear_embedding_indexes()
    claims.clear_claim_indexes()
    find_module.clear_cache()
    yield root
    embeddings.clear_embedding_indexes()
    claims.clear_claim_indexes()
    find_module.clear_cache()


@pytest.fixture(params=["numpy", "sqlite-vec"])
def backend(request, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "sqlite-vec":
        pytest.importorskip("sqlite_vec")
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", request.param)
    return request.param


# --------------------------------------------------------------- the sidecar


def test_a_1024_dim_sidecar_round_trips_with_its_width_read_from_the_sidecar(
    vault: Path, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "MODEL_NAME", M3)
    first = _unit_rows(1, 2, 1024)
    second = _unit_rows(2, 1, 1024)
    writer = EmbeddingIndex(vault)
    writer.upsert_file("Knowledge Base/a.md", ["alpha", "beta"], first, 1.0)
    writer.upsert_file("Knowledge Base/b.md", ["gamma"], second, 2.0)

    reader = EmbeddingIndex(vault)
    assert reader.identity == recall_space.SpaceIdentity(M3, None, 1024)
    assert reader.dim == 1024
    hits = reader.search(first[1], k=2)
    assert (hits[0][0], hits[0][1], hits[0][2]) == ("Knowledge Base/a.md", 1, "beta")
    if backend == "sqlite-vec":
        assert reader._vec.dim == 1024 and reader._vec_ready
    metadata, matrix = reader.all_vectors()
    assert matrix.shape == (3, 1024) and len(metadata) == 3
    [[best, *_rest]] = reader.search_many(second, k=1, admits=lambda _path: True)
    assert best[:2] == ("Knowledge Base/b.md", 0)
    chunk_vectors, _units = reader.stored_text_vectors("Knowledge Base/a.md")
    assert set(chunk_vectors) == {"alpha", "beta"}
    assert chunk_vectors["beta"].shape == (1024,)


def test_an_empty_sidecar_has_no_space_until_its_first_write(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "MODEL_NAME", M3)
    index = EmbeddingIndex(vault)
    assert index.identity is None
    # An empty sidecar answers at the width the current encoder produces.
    assert index.dim == 1024
    metadata, matrix = index.all_vectors()
    assert metadata == [] and matrix.shape == (0, 1024)


def test_a_sidecar_written_before_the_record_existed_is_the_english_space(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "MODEL_NAME", BASE)
    EmbeddingIndex(vault).upsert_file("Knowledge Base/a.md", ["alpha"], _unit_rows(3, 1, 768), 1.0)
    path = index_paths.sidecar_path(vault)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'embedding_%'")

    legacy = EmbeddingIndex(vault)
    assert legacy.identity == recall_space.SpaceIdentity(BASE, None, 768)
    assert legacy.dim == 768


def test_a_write_of_another_width_is_refused_and_leaves_the_sidecar_alone(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "MODEL_NAME", M3)
    index = EmbeddingIndex(vault)
    kept = _unit_rows(4, 1, 1024)
    index.upsert_file("Knowledge Base/a.md", ["alpha"], kept, 1.0)

    with pytest.raises(recall_space.VectorSpaceMismatch):
        index.upsert_file("Knowledge Base/a.md", ["alpha"], _unit_rows(5, 1, 768), 2.0)

    vectors, _units = EmbeddingIndex(vault).stored_text_vectors("Knowledge Base/a.md")
    np.testing.assert_array_equal(vectors["alpha"], kept[0])


def test_the_record_names_the_resident_encoder_fingerprint(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(SPACE_A, digest="aaaa")
    monkeypatch.setattr(embeddings, "MODEL_NAME", SPACE_A)
    monkeypatch.setattr(embeddings, "_MODEL", _Resident(profile))

    EmbeddingIndex(vault).upsert_file("Knowledge Base/a.md", ["alpha"], _unit_rows(6, 1, 8), 1.0)

    assert EmbeddingIndex(vault).identity == recall_space.SpaceIdentity(
        SPACE_A, profile.fingerprint(), 8
    )


# ------------------------------------------------------------------- the query


_PAGES = {
    "Notes/retry-with-backoff.md": (
        "Retry with backoff",
        "Retries wait an exponentially growing delay so failing clients do not hammer a service.",
    ),
    "Notes/incident-review.md": (
        "Incident review",
        "The incident review found that clients did not wait for a recovering service.",
    ),
}


class _Resident:
    """A resident encoder as the backends expose it: a profile and nothing else."""

    def __init__(self, profile: embedding_backend.EncoderProfile) -> None:
        self.profile = profile


def _profile(model: str, *, digest: str) -> embedding_backend.EncoderProfile:
    return embedding_backend.EncoderProfile(
        model=model,
        pooling="cls",
        query_prefix="",
        passage_prefix="",
        max_seq=512,
        pad_token="<pad>",
        revision="0" * 40,
        quantization="ort-dynamic-int8",
        file_format="onnx-external-data",
        artifact_digest=digest,
    )


class _Encoder:
    """Every text lands on the same unit vector of the current model's width."""

    def __init__(self) -> None:
        self.queries = 0

    def __call__(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        if is_query:
            self.queries += 1
        dim = _FAKE_DIMS[embeddings.MODEL_NAME]
        return np.full((len(texts), dim), 1.0 / dim**0.5, dtype=np.float32)


def _write_pages(vault: Path) -> None:
    for rel, (title, body) in _PAGES.items():
        page = vault / kb_dirname() / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            f"---\ntype: note\ntitle: {title}\nupdated: 2026-09-01\n---\n\n# {title}\n\n{body}\n",
            encoding="utf-8",
        )


def _build_sidecar(vault: Path, encoder: _Encoder) -> None:
    index = EmbeddingIndex(vault)
    for rel, (title, body) in _PAGES.items():
        index.upsert_file(f"{kb_dirname()}/{rel}", [f"{title}. {body}"], encoder([body]), 1.0)


@pytest.fixture
def pages(vault: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, _Encoder]:
    _write_pages(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embeddings, "MODEL_NAME", SPACE_A)
    monkeypatch.setattr(embeddings, "_MODEL", None)
    monkeypatch.setattr(embeddings, "get_model", lambda: embeddings._MODEL or object())
    readiness.reset()
    encoder = _Encoder()
    monkeypatch.setattr(embeddings, "embed_texts", encoder)
    yield vault, encoder
    readiness.reset()


@pytest.fixture
def served(pages: tuple[Path, _Encoder]) -> tuple[Path, _Encoder]:
    vault, encoder = pages
    _build_sidecar(vault, encoder)
    return vault, encoder


def _explained(vault: Path, query: str) -> dict:
    from exomem import commands

    find_module.clear_cache()
    return commands.op_ask_memory(
        vault,
        query=query,
        limit=10,
        mode="hybrid",
        scope="kb-only",
        graph=False,
        rerank=False,
        detail="compact",
        explain=True,
    )


def test_a_query_in_the_sidecar_space_serves_the_vector_lane(served) -> None:
    vault, encoder = served
    result = _explained(vault, "retry backoff")
    assert result["retrieval_profile"]["lanes"]["vector"]["status"] == "participated"
    assert encoder.queries == 1


def test_a_cell_leaves_the_vector_lane_out_for_another_models_sidecar(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A personal server serves such a sidecar with the model that wrote it
    # while it re-embeds (tests/test_embedding_migration.py); a hosted or cloud
    # cell runs no second encoder, so the sidecar is refused there.
    vault, encoder = served
    monkeypatch.setattr(embeddings, "MODEL_NAME", SPACE_B)
    monkeypatch.setattr(recall_space, "cell_mode", lambda env=None: True)

    result = _explained(vault, "retry backoff")

    vector = result["retrieval_profile"]["lanes"]["vector"]
    assert vector["status"] == "unavailable"
    assert vector["reason"] == "vector_space_mismatch"
    assert result["retrieval_profile"]["lanes"]["bm25"]["status"] == "participated"
    assert result["hits"][0]["path"] == f"{kb_dirname()}/Notes/retry-with-backoff.md"
    # The query was never encoded for a space nothing here can read.
    assert encoder.queries == 0


def test_a_query_from_another_build_of_the_same_model_is_refused(
    pages: tuple[Path, _Encoder], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same model name, but the resident encoder runs other bytes than the
    # ones the sidecar was written with: another vector space.
    vault, encoder = pages
    monkeypatch.setattr(embeddings, "_MODEL", _Resident(_profile(SPACE_A, digest="aaaa")))
    _build_sidecar(vault, encoder)
    assert _explained(vault, "retry backoff")["retrieval_profile"]["lanes"]["vector"][
        "status"
    ] == "participated"
    queries = encoder.queries
    monkeypatch.setattr(embeddings, "_MODEL", _Resident(_profile(SPACE_A, digest="bbbb")))

    vector = _explained(vault, "retry backoff")["retrieval_profile"]["lanes"]["vector"]

    assert (vector["status"], vector["reason"]) == ("unavailable", "vector_space_mismatch")
    assert encoder.queries == queries


# ----------------------------------------------------------- the active pointer


def test_the_active_pointer_names_the_serving_sidecar(vault: Path) -> None:
    state = index_paths.legacy_sidecar_path(vault).parent
    assert index_paths.sidecar_path(vault) == state / ".embeddings.sqlite"
    legacy_index = embeddings.get_embedding_index(vault)

    name = index_paths.space_sidecar_name(f"{M3}|cls|l2|0123456789abcdef")
    assert name.startswith(".embeddings.") and name.endswith(".sqlite")
    index_paths.publish_active_sidecar(vault, name)

    assert index_paths.sidecar_path(vault) == state / name
    served_index = embeddings.get_embedding_index(vault)
    assert served_index is not legacy_index
    assert served_index.path == state / name
    served_index.upsert_file("Knowledge Base/a.md", ["alpha"], _unit_rows(7, 1, 768), 1.0)
    assert (state / name).is_file()
    assert not (state / ".embeddings.sqlite").exists()


@pytest.mark.parametrize(
    "name",
    ["../escape.sqlite", ".lexical.sqlite", ".embeddings.sqlite-wal", "notes.md", ""],
)
def test_a_pointer_that_names_no_embedding_sidecar_is_ignored(vault: Path, name: str) -> None:
    pointer = index_paths.legacy_sidecar_path(vault).parent / index_paths.ACTIVE_SIDECAR_POINTER
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(name + "\n", encoding="utf-8")
    assert index_paths.sidecar_path(vault) == index_paths.legacy_sidecar_path(vault)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_space_sidecars_and_the_pointer_are_reserved_for_the_embedding_index(suffix: str) -> None:
    name = index_paths.space_sidecar_name("any fingerprint") + suffix
    classified = classify_logical(name)
    assert classified.disposition is PathDisposition.RESERVED
    assert classified.descriptor_id == "embeddings-store"
    pointer = classify_logical(index_paths.ACTIVE_SIDECAR_POINTER)
    assert (pointer.disposition, pointer.descriptor_id) == (
        PathDisposition.RESERVED,
        "embeddings-store",
    )


# ------------------------------------------------------------------- the claims


_CLAIM_PAGE = "Notes/Decisions/retry-budget.md"


def _write_claim_page(vault: Path) -> Path:
    page = vault / kb_dirname() / _CLAIM_PAGE
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: insight\ntitle: Retry budget\nstatus: active\nupdated: 2026-09-01\n---\n\n"
        "# Retry budget\n\nClients retry at most three times before they give up.\n",
        encoding="utf-8",
    )
    return page


def test_the_claims_sidecar_follows_the_recall_space(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _write_claim_page(vault)
    rel = f"{kb_dirname()}/{_CLAIM_PAGE}"
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_CLAIM_LEVEL", "1")
    monkeypatch.setattr(embeddings, "MODEL_NAME", SPACE_A)
    monkeypatch.setattr(embeddings, "_MODEL", None)
    monkeypatch.setattr(embeddings, "embed_texts", _Encoder())
    index = claims.get_claim_index(vault)
    assert index.rebuild_all() == 1
    _metadata, matrix = index.all_claims()
    assert matrix.shape == (1, 8)
    assert index.get_row(rel)[1].shape == (8,)
    assert index.checksums()

    monkeypatch.setattr(embeddings, "MODEL_NAME", SPACE_B)
    # Rows of the old space are not current in the new one: nothing is served,
    # and the incremental pass sees nothing it may skip.
    assert index.checksums() == {}
    assert index.get_row(rel) is None
    metadata, matrix = index.all_claims()
    assert metadata == [] and len(matrix) == 0

    claims.upsert_claims_after_write(vault, [page])
    assert index._get_row_unchecked(rel)[1].shape == (12,)
    assert index.rebuild_all() == 1
    _metadata, matrix = index.all_claims()
    assert matrix.shape == (1, 12)
