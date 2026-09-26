"""Recall runs one multilingual encoder on a personal server.

A personal server encodes recall (and, sharing the instance, activation) with
`BAAI/bge-m3`. A hosted or cloud cell keeps `BAAI/bge-base-en-v1.5` until the
node encoder serves it. What a trace or doctor reports as the model is the one
whose vectors actually served: while a re-embed has not cut over, that is the
model that wrote the serving sidecar.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from exomem import cloud_cell, embeddings, hosted_runtime, readiness, recall_space
from exomem import find as find_module
from exomem.embedding_index import EmbeddingIndex
from exomem.kbdir import kb_dirname

M3 = "BAAI/bge-m3"
BASE = "BAAI/bge-base-en-v1.5"


@pytest.mark.parametrize(
    ("env", "model"),
    [
        ({}, M3),
        ({"EXOMEM_HOSTED_CELL": "0"}, M3),
        ({"EXOMEM_HOSTED_CELL": "1"}, BASE),
        ({"EXOMEM_CLOUD_CELL": "true"}, BASE),
        ({"EXOMEM_HOSTED_CELL": "off", "EXOMEM_CLOUD_CELL": "yes"}, BASE),
        # A malformed flag is read as a cell, as content-private logging reads it.
        ({"EXOMEM_HOSTED_CELL": "maybe"}, BASE),
        # An explicit model wins over the deployment's default, either way.
        ({"EXOMEM_RECALL_MODEL": BASE}, BASE),
        ({"EXOMEM_RECALL_MODEL": M3, "EXOMEM_HOSTED_CELL": "1"}, M3),
    ],
)
def test_the_recall_encoder_follows_the_deployment(env: dict[str, str], model: str) -> None:
    assert recall_space.configured_recall_model(env) == model


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "1", "true", "yes", "on"])
def test_the_cell_flags_read_as_the_runtime_reads_them(value: str) -> None:
    hosted = {"EXOMEM_HOSTED_CELL": value}
    cloud = {"EXOMEM_CLOUD_CELL": value}
    assert recall_space.cell_mode(hosted) == hosted_runtime.hosted_mode_enabled(hosted)
    assert recall_space.cell_mode(cloud) == cloud_cell.cloud_mode_enabled(cloud)


def test_this_personal_process_encodes_recall_with_the_multilingual_model() -> None:
    assert embeddings.MODEL_NAME == M3
    assert embeddings.activation_encoder_is_shared()


# ------------------------------------------------------------------ the traces


class _Encoder:
    def __init__(self, dim: int) -> None:
        self.dim = dim

    def __call__(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        return np.full((len(texts), self.dim), 1.0 / self.dim**0.5, dtype=np.float32)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    page = root / kb_dirname() / "Notes/retry.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: note\ntitle: Retry\nupdated: 2026-09-01\n---\n\n# Retry\n\n"
        "Retries wait a growing delay so clients do not hammer a service.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    readiness.reset()
    find_module.clear_cache()
    embeddings.clear_embedding_indexes()
    yield root
    recall_space.unload_previous()
    embeddings.clear_embedding_indexes()
    find_module.clear_cache()
    readiness.reset()


def _profile(vault: Path) -> dict:
    from exomem import commands

    find_module.clear_cache()
    return commands.op_ask_memory(
        vault,
        query="retry delay",
        limit=5,
        mode="hybrid",
        scope="kb-only",
        graph=False,
        rerank=False,
        detail="compact",
        explain=True,
    )["retrieval_profile"]


def test_the_vector_lane_names_the_model_whose_vectors_served(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "embed_texts", _Encoder(1024))
    EmbeddingIndex(vault).upsert_file(
        f"{kb_dirname()}/Notes/retry.md", ["Retry. Retries wait."], _Encoder(1024)(["x"]), 1.0
    )

    lanes = _profile(vault)["lanes"]

    assert lanes["vector"]["status"] == "participated"
    assert lanes["vector"]["model"] == M3


def test_while_a_re_embed_runs_the_lane_names_the_serving_sidecars_model(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Legacy:
        profile = None

        def encode(self, texts, **_kwargs):
            return _Encoder(768)(list(texts))

    monkeypatch.setattr(embeddings, "MODEL_NAME", BASE)
    EmbeddingIndex(vault).upsert_file(
        f"{kb_dirname()}/Notes/retry.md", ["Retry. Retries wait."], _Encoder(768)(["x"]), 1.0
    )
    monkeypatch.setattr(embeddings, "MODEL_NAME", M3)
    monkeypatch.setattr(recall_space, "previous_resident", lambda model: _Legacy() if model == BASE else None)

    lanes = _profile(vault)["lanes"]

    assert lanes["vector"]["status"] == "participated"
    assert lanes["vector"]["model"] == BASE


def test_a_disabled_lane_names_the_recall_encoder(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    lanes = _profile(vault)["lanes"]
    assert lanes["vector"] == {"status": "disabled", "reason": "embeddings_disabled", "model": M3}


# ------------------------------------------------------------------- doctor


def test_doctor_probes_the_sidecar_with_the_encoder_that_serves_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import doctor

    loaded: list[str] = []

    class _Legacy:
        profile = None

        def encode(self, texts, **_kwargs):
            return _Encoder(768)(list(texts))

    monkeypatch.setattr(embeddings, "MODEL_NAME", BASE)
    EmbeddingIndex(vault).upsert_file(
        f"{kb_dirname()}/Notes/retry.md", ["Retry. Retries wait."], _Encoder(768)(["x"]), 1.0
    )
    monkeypatch.setattr(embeddings, "MODEL_NAME", M3)
    monkeypatch.setattr(doctor, "_resolved_embedding_backend", lambda: "onnx")
    monkeypatch.setattr(doctor, "_vector_stack_available", lambda _backend: True)
    monkeypatch.setattr(doctor, "_model_cached", lambda _hub, dirname: dirname.endswith("bge-base-en-v1.5"))
    monkeypatch.setattr(
        "exomem.embedding_backend.load_encoder",
        lambda name, **_kwargs: loaded.append(name) or _Legacy(),
    )

    check = doctor._check_embedding_sidecar(vault)

    assert check.status == "pass", check.message
    assert loaded == [BASE]
    assert check.details["model"] == BASE
    assert check.details["dim"] == 768
