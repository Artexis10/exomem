"""Serving backend for the bi-encoder: torch/sentence-transformers or ONNX Runtime.

The model is fixed; only the runtime that serves it is chosen here. That
distinction is the whole point of the seam — a backend swap must not change
which vectors a vault holds, so stored embeddings stay valid and nothing is
re-indexed. `fingerprint()` records the model identity that *would* invalidate
them, deliberately excluding the backend name.

Why this exists: `sentence-transformers` pulls the full PyTorch runtime into
every process that embeds. In a hosted cell that is the binding constraint on
how many tenants a node carries — measured on the identical model, ONNX Runtime
imports ~80 MiB against torch's ~400 and holds a smaller warm resident, with
vectors interchangeable to a cosine similarity far tighter than the fp16 drift
`embeddings._maybe_half` already accepts as harmless for ranking.

Backends are equivalent in output, not in reach: torch also serves the reranker
and CLIP, so only the hosted lane — which withholds both — can drop it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Protocol

import numpy as np

from . import accel, model_cache

log = logging.getLogger(__name__)

#: Selects the serving runtime. ``auto`` prefers torch when it is importable so a
#: developer box keeps its existing behaviour, and falls back to ONNX Runtime —
#: which is what a torch-free hosted image lands on without configuration.
BACKEND_ENV = "EXOMEM_EMBED_BACKEND"
TORCH = "torch"
ONNX = "onnx"
_VALID = (TORCH, ONNX)

#: BGE pools the CLS token rather than averaging; see the model's 1_Pooling
#: config. Getting this wrong yields plausible-looking vectors that rank badly,
#: which is exactly the failure a similarity gate is meant to catch.
_POOLING_CLS = "cls"
_DEFAULT_MAX_SEQ = 512


class Encoder(Protocol):
    """What `embeddings` needs from a serving runtime, and nothing more."""

    #: Backend identifier, for logging and readiness reporting.
    backend: str
    #: Device the model actually landed on, in torch's vocabulary.
    device: str

    def encode(
        self,
        texts: list[str],
        *,
        batch_size: int,
        normalize_embeddings: bool = True,
        convert_to_numpy: bool = True,
        show_progress_bar: bool = False,
    ) -> np.ndarray: ...

    def release(self) -> None:
        """Drop runtime-held memory. Called after the singleton is dropped."""


def resolve_backend(*, is_available: Callable[[str], bool] | None = None) -> str:
    """Which runtime should serve, honouring an explicit choice over detection.

    `is_available` lets a caller supply its own import probe so detection agrees
    with whatever that caller reports elsewhere — `doctor` passes its own, so a
    single probe decides both the profile it infers and the dependencies it lists.
    """
    raw = (os.environ.get(BACKEND_ENV) or "").strip().lower()
    if raw in _VALID:
        return raw
    if raw and raw != "auto":
        raise ValueError(f"unknown {BACKEND_ENV}: {raw!r}. Valid: {list(_VALID)} or 'auto'")
    probe = is_available or _importable
    if probe("sentence_transformers"):
        return TORCH
    # ONNX only when it is genuinely the installed lane. With neither present the
    # answer is torch, so an install that has no embedding backend at all is told
    # to install the default one rather than the specialised hosted alternative.
    return ONNX if probe("onnxruntime") else TORCH


def _importable(module: str) -> bool:
    """Whether `module` can be imported, without importing it.

    An already-imported module counts even when it carries no import spec —
    something injected into `sys.modules` is importable by definition, and
    `find_spec` raises rather than answering for that case.
    """
    import importlib.util
    import sys

    if module in sys.modules:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # namespace oddities → treat as absent
        return False


def fingerprint(model_name: str) -> str:
    """Identity of the vectors a vault holds — model and pooling, not backend.

    Recorded alongside a sidecar so a future *model* change is detected instead
    of silently mixing two vector spaces. A backend substitution must leave this
    unchanged, which is what makes it a substitution rather than a migration.
    """
    return f"{model_name}|{_POOLING_CLS}|l2"


class _TorchEncoder:
    """sentence-transformers, unchanged in behaviour from the pre-seam path."""

    backend = TORCH

    def __init__(self, model_name: str, device: str, half: bool) -> None:
        # Heavy import stays local — keyword-mode and a lean install must not pay it.
        from sentence_transformers import SentenceTransformer

        model = model_cache.load_offline_first(
            model_name,
            lambda **kw: SentenceTransformer(model_name, device=device, **kw),
        )
        self._model = _maybe_half(model, device) if half else model
        self.device = device

    def encode(self, texts, **kwargs) -> np.ndarray:
        kwargs.setdefault("convert_to_numpy", True)
        kwargs.setdefault("normalize_embeddings", True)
        kwargs.setdefault("show_progress_bar", False)
        return self._model.encode(texts, **kwargs)

    def release(self) -> None:
        self._model = None
        accel.empty_cache()


class _OnnxEncoder:
    """ONNX Runtime over the model's published ONNX export.

    Reimplements only what sentence-transformers did for this model: tokenize,
    run the encoder, take the CLS vector, L2-normalise. There is no torch here,
    which is the entire reason the hosted image can shed ~300 MiB per cell.
    """

    backend = ONNX

    def __init__(self, model_name: str, device: str) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        onnx_path = _resolve(model_name, "onnx/model.onnx")
        tokenizer_path = _resolve(model_name, "tokenizer.json")

        self._tokenizer = Tokenizer.from_file(tokenizer_path)
        self._tokenizer.enable_truncation(max_length=_max_seq_length(model_name))
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # One intra-op thread per cell core. Left at the runtime default here;
        # the cell sets OMP_NUM_THREADS, which onnxruntime honours.
        self._session = ort.InferenceSession(
            onnx_path, sess_options=options, providers=_providers(device)
        )
        self._inputs = {spec.name for spec in self._session.get_inputs()}
        self.device = device

    def encode(
        self,
        texts: list[str],
        *,
        batch_size: int = 8,
        normalize_embeddings: bool = True,
        convert_to_numpy: bool = True,  # noqa: ARG002 — always numpy; kept for parity
        show_progress_bar: bool = False,  # noqa: ARG002 — no progress bar to suppress
    ) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        out: list[np.ndarray] = []
        for start in range(0, len(texts), max(1, batch_size)):
            out.append(self._encode_batch(texts[start : start + batch_size], normalize_embeddings))
        return np.vstack(out)

    def _encode_batch(self, batch: list[str], normalize: bool) -> np.ndarray:
        encodings = self._tokenizer.encode_batch(batch)
        feed = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
        }
        hidden = self._session.run(None, {k: v for k, v in feed.items() if k in self._inputs})[0]
        pooled = hidden[:, 0]  # CLS
        if normalize:
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            pooled = pooled / np.maximum(norms, 1e-12)
        return pooled.astype(np.float32, copy=False)

    def release(self) -> None:
        self._session = None
        self._tokenizer = None


def _providers(device: str) -> list[str]:
    """Map a torch-shaped device onto ONNX Runtime execution providers.

    Providers are not torch devices: the list is an ordered preference and the
    runtime silently uses the first one it can construct. CPU is always appended
    so an unavailable accelerator degrades rather than raising — the same
    politeness `accel.gpu_usable` applies on the torch side.
    """
    import onnxruntime as ort

    available = set(ort.get_available_providers())
    preferred: list[str] = []
    if device.startswith("cuda") and "CUDAExecutionProvider" in available:
        preferred.append("CUDAExecutionProvider")
    elif device == "mps" and "CoreMLExecutionProvider" in available:
        preferred.append("CoreMLExecutionProvider")
    preferred.append("CPUExecutionProvider")
    return preferred


def _resolve(model_name: str, filename: str) -> str:
    """Path to a model file, from the local hub cache when the snapshot is resident."""
    from huggingface_hub import hf_hub_download

    return model_cache.load_offline_first(
        model_name,
        lambda **kw: hf_hub_download(model_name, filename, **kw),
    )


def _max_seq_length(model_name: str) -> int:
    """The model's declared sequence limit, defaulting to BERT's 512."""
    try:
        with open(_resolve(model_name, "sentence_bert_config.json"), encoding="utf-8") as fh:
            return int(json.load(fh).get("max_seq_length") or _DEFAULT_MAX_SEQ)
    except Exception:  # noqa: BLE001 — a missing config must not block loading
        log.debug("no sentence_bert_config for %s; using %d", model_name, _DEFAULT_MAX_SEQ)
        return _DEFAULT_MAX_SEQ


def _maybe_half(model, device: str):
    """fp16 on Apple Silicon only. Imported by `embeddings` for backwards parity."""
    if device != "mps" or os.environ.get("EXOMEM_MPS_FP16", "1") == "0":
        return model
    try:
        return model.half()
    except Exception:  # noqa: BLE001 — a precision tweak must never break model load
        log.warning("fp16 (MPS) conversion failed; staying fp32", exc_info=True)
        return model


def load_encoder(model_name: str, *, backend: str | None = None) -> Encoder:
    """Construct the configured backend for `model_name`.

    Device selection stays with `accel`, which already returns ``cpu`` when torch
    is absent — so a torch-free image resolves correctly without a special case.
    """
    chosen = backend or resolve_backend()
    device = accel.select_device(override_env="EXOMEM_EMBED_DEVICE")
    log.info("loading embedding model %s on %s via %s", model_name, device, chosen)
    if chosen == ONNX:
        return _OnnxEncoder(model_name, device)
    return _TorchEncoder(model_name, device, half=True)


def batch_size_for(device: str) -> int:
    """Encode batch size for a device, honouring an explicit override.

    Batch size sets peak resident memory, because activations scale with it while
    the weights do not. Measured on CPU with bge-base at ~280-token chunks: batch
    32 peaks at 1332 MiB and yields 1.8 chunks/s, batch 8 peaks at 918 MiB and
    yields 2.3 chunks/s. On CPU a large batch is strictly worse on both axes — it
    buys no parallelism the cores were not already giving and just costs cache
    locality. On an accelerator the opposite holds, so the choice follows the
    device rather than being one global constant.

    This matters most where memory is the binding constraint: a hosted cell's
    limit is sized from this peak, and peak per cell decides how many tenants a
    node carries.
    """
    override = os.environ.get("EXOMEM_EMBED_BATCH", "").strip()
    if override:
        try:
            parsed = int(override)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    normalized = device.lower()
    return 32 if (normalized.startswith("cuda") or normalized == "mps") else 8
