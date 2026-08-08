"""Backend seam: selection policy, batch policy, and cross-backend equivalence.

The equivalence case is opt-in because it loads the model twice under two
runtimes and needs the weights present. Everything else runs everywhere and is
what guards the policy a lean install actually exercises.
"""

from __future__ import annotations

import os
import sys
import types

import numpy as np
import pytest

from exomem import embedding_backend

RUN_EQUIVALENCE = os.environ.get("RUN_EMBED_EQUIVALENCE_TEST") == "1"

#: Fixed text set for the equivalence gate. Deliberately includes the shapes a
#: tokenizer is most likely to disagree on: empty, whitespace-only, non-ASCII,
#: emoji, over-length (truncation), and code-shaped text.
EQUIVALENCE_TEXTS = [
    "",
    " ",
    "a",
    "The mitochondrion is the powerhouse of the cell.",
    "Exomem is a governed long-term memory store for durable conclusions.",
    "  leading and trailing whitespace  ",
    "Ünïcödé ñoñ-ÄSCII — em-dash, curly ’quotes‘, and 中文字符 mixed in.",
    "🧠🔬 emoji only 🚀",
    "repeat " * 300,
    "\n\nnewlines\n\tand\ttabs\n\n",
    "SELECT * FROM notes WHERE id = 42; -- code-shaped text",
    "The quick brown fox jumps over the lazy dog. " * 20,
]

#: The measured floor. The observed minimum on this set is 0.99999994; the bound
#: is set well below that so ordinary runtime jitter does not fail the gate,
#: while a pooling or tokenizer mistake — which lands orders of magnitude lower —
#: still does.
MIN_COSINE = 0.9999


def test_explicit_backend_wins_over_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(embedding_backend.BACKEND_ENV, "onnx")
    assert embedding_backend.resolve_backend() == embedding_backend.ONNX
    monkeypatch.setenv(embedding_backend.BACKEND_ENV, "torch")
    assert embedding_backend.resolve_backend() == embedding_backend.TORCH


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(embedding_backend.BACKEND_ENV, "tensorflow")
    with pytest.raises(ValueError, match="unknown"):
        embedding_backend.resolve_backend()


def test_auto_prefers_torch_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(embedding_backend.BACKEND_ENV, "auto")
    monkeypatch.setitem(sys.modules, "sentence_transformers", types.ModuleType("st"))
    assert embedding_backend.resolve_backend() == embedding_backend.TORCH


def test_auto_selects_onnx_when_it_is_the_installed_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A torch-free hosted image must land on ONNX without being configured."""
    monkeypatch.delenv(embedding_backend.BACKEND_ENV, raising=False)
    assert (
        embedding_backend.resolve_backend(is_available=lambda name: name == "onnxruntime")
        == embedding_backend.ONNX
    )


def test_auto_defaults_to_torch_when_no_backend_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing installed, name the default lane — not the hosted alternative.

    This is what decides which `uv sync --extra` a fresh box is told to run.
    """
    monkeypatch.delenv(embedding_backend.BACKEND_ENV, raising=False)
    assert (
        embedding_backend.resolve_backend(is_available=lambda _name: False)
        == embedding_backend.TORCH
    )


def test_already_imported_module_counts_as_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A module injected into sys.modules has no spec but is importable."""
    injected = types.ModuleType("exomem_fake_backend_probe")
    assert injected.__spec__ is None
    monkeypatch.setitem(sys.modules, "exomem_fake_backend_probe", injected)
    assert embedding_backend._importable("exomem_fake_backend_probe") is True


@pytest.mark.parametrize(
    ("device", "expected"),
    [("cpu", 8), ("CPU", 8), ("cuda", 32), ("cuda:1", 32), ("mps", 32), ("", 8)],
)
def test_batch_size_follows_device(
    device: str, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_EMBED_BATCH", raising=False)
    assert embedding_backend.batch_size_for(device) == expected


def test_batch_size_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_EMBED_BATCH", "3")
    assert embedding_backend.batch_size_for("cuda") == 3


@pytest.mark.parametrize("bad", ["0", "-4", "not-a-number", "  "])
def test_batch_size_ignores_unusable_override(bad: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_EMBED_BATCH", bad)
    assert embedding_backend.batch_size_for("cpu") == 8


def test_fingerprint_excludes_the_backend() -> None:
    """A backend swap must not look like a model change, or it would re-index."""
    assert embedding_backend.ONNX not in embedding_backend.fingerprint("BAAI/bge-base-en-v1.5")
    assert embedding_backend.TORCH not in embedding_backend.fingerprint("BAAI/bge-base-en-v1.5")
    assert embedding_backend.fingerprint("model-a") != embedding_backend.fingerprint("model-b")


def test_providers_always_end_in_cpu() -> None:
    """An unavailable accelerator must degrade, never raise."""
    pytest.importorskip("onnxruntime")
    for device in ("cpu", "cuda", "cuda:1", "mps"):
        assert embedding_backend._providers(device)[-1] == "CPUExecutionProvider"


@pytest.mark.skipif(not RUN_EQUIVALENCE, reason="set RUN_EMBED_EQUIVALENCE_TEST=1")
def test_onnx_and_torch_produce_interchangeable_vectors() -> None:
    """Same model, two runtimes: vectors must be substitutable without re-indexing.

    Asserts the property that actually matters — that an existing vault's vectors
    stay comparable to newly encoded ones — rather than bitwise equality, which
    no two runtimes give.
    """
    pytest.importorskip("sentence_transformers")
    pytest.importorskip("onnxruntime")

    from exomem.embeddings import MODEL_NAME

    torch_encoder = embedding_backend.load_encoder(MODEL_NAME, backend=embedding_backend.TORCH)
    onnx_encoder = embedding_backend.load_encoder(MODEL_NAME, backend=embedding_backend.ONNX)

    left = torch_encoder.encode(EQUIVALENCE_TEXTS, batch_size=8)
    right = onnx_encoder.encode(EQUIVALENCE_TEXTS, batch_size=8)

    assert left.shape == right.shape
    assert left.shape[1] == 768
    assert right.dtype == np.float32
    assert np.allclose(np.linalg.norm(right, axis=1), 1.0, atol=1e-5)

    cosine = (left * right).sum(1) / (
        np.linalg.norm(left, axis=1) * np.linalg.norm(right, axis=1)
    )
    assert cosine.min() >= MIN_COSINE, f"minimum cosine {cosine.min():.9f} below {MIN_COSINE}"

    # Ranking is what retrieval actually consumes, so prove the ordering survives
    # rather than inferring it from similarity.
    left_rank = np.argsort(-(left @ left.T), axis=1)[:, 0]
    right_rank = np.argsort(-(right @ right.T), axis=1)[:, 0]
    assert (left_rank == right_rank).all()
