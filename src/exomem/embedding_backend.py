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

import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from . import accel, model_cache, runtime_resources

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
    #: How the model encodes — pooling, prefixes, limit — and so which vectors.
    profile: EncoderProfile

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


#: What a model repository does not say about itself: the prefixes each model was
#: trained with, and the pooling and padding token to assume when a cached copy
#: predates the files that do say (an ONNX cache from before profiles existed
#: holds no `1_Pooling/config.json`). Recall's model keeps exactly what the
#: shipped encoder always did: CLS, `[PAD]`, and its query prefix.
_DECLARED: dict[str, tuple[str, str, str, str]] = {
    # model: (query prefix, passage prefix, pooling, padding token)
    "BAAI/bge-base-en-v1.5": (
        "Represent this sentence for searching relevant passages: ",
        "",
        _POOLING_CLS,
        "[PAD]",
    ),
    "intfloat/multilingual-e5-small": ("query: ", "passage: ", "mean", "<pad>"),
    "intfloat/multilingual-e5-base": ("query: ", "passage: ", "mean", "<pad>"),
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": ("", "", "mean", "<pad>"),
}
_UNDECLARED = ("", "", _POOLING_CLS, "[PAD]")


@dataclass(frozen=True)
class EncoderProfile:
    """How one model turns text into a vector: read from the model, never guessed.

    Pooling, the sequence limit and the padding token come from the repository's
    own `1_Pooling/config.json`, `sentence_bert_config.json` and
    `tokenizer_config.json` (or from the loaded sentence-transformers model, which
    read the same files). The prefixes are the model's training convention, which
    no file records, so they come from `_DECLARED`.
    """

    model: str
    pooling: str
    query_prefix: str
    passage_prefix: str
    max_seq: int
    pad_token: str
    onnx_file: str = "onnx/model.onnx"

    def fingerprint(self) -> str:
        """Identity of the vector space: model, pooling, prefixes, limit, L2.

        Never the backend, the padding token or the ONNX file: those are how a
        runtime computes a vector, not which vector it computes, and a backend
        swap must not look like a model change.
        """
        identity = json.dumps(
            {
                "model": self.model,
                "pooling": self.pooling,
                "query_prefix": self.query_prefix,
                "passage_prefix": self.passage_prefix,
                "max_seq": self.max_seq,
                "normalize": "l2",
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return f"{self.model}|{self.pooling}|l2|{digest}"


def _read_json(model_name: str, filename: str) -> dict | None:
    try:
        with open(_resolve(model_name, filename), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — an absent config falls back to the declared value
        log.debug("no %s for %s; using the declared value", filename, model_name)
        return None
    return data if isinstance(data, dict) else None


def _pooling_from_config(config: dict | None) -> str | None:
    if not config:
        return None
    if config.get("pooling_mode_cls_token"):
        return _POOLING_CLS
    if config.get("pooling_mode_mean_tokens"):
        return "mean"
    return None


def _pad_from_config(config: dict | None) -> str | None:
    value = (config or {}).get("pad_token")
    if isinstance(value, dict):
        value = value.get("content")
    return str(value) if value else None


def read_profile(model_name: str) -> EncoderProfile:
    """The profile of `model_name`, read from its repository files.

    Called where a model loads, never on a request thread: it resolves up to
    three small files from the local hub cache, offline-first.
    """
    query, passage, pooling, pad = _DECLARED.get(model_name, _UNDECLARED)
    return EncoderProfile(
        model=model_name,
        pooling=_pooling_from_config(_read_json(model_name, "1_Pooling/config.json")) or pooling,
        query_prefix=query,
        passage_prefix=passage,
        max_seq=_max_seq_length(model_name),
        pad_token=_pad_from_config(_read_json(model_name, "tokenizer_config.json")) or pad,
    )


def profile_from_sentence_transformer(model_name: str, model) -> EncoderProfile:
    """The same profile, read off a loaded sentence-transformers model.

    That model already parsed the repository's pooling and tokenizer files, so
    reading them back costs no I/O and gives the torch lane exactly the profile
    `read_profile` gives the ONNX lane.
    """
    query, passage, pooling, pad = _DECLARED.get(model_name, _UNDECLARED)
    try:
        for module in model:
            mode = getattr(module, "get_pooling_mode_str", None)
            if callable(mode):
                pooling = str(mode())
                break
    except TypeError:  # not a module sequence — keep the declared pooling
        pass
    max_seq = getattr(model, "max_seq_length", None)
    tokenizer_pad = getattr(getattr(model, "tokenizer", None), "pad_token", None)
    return EncoderProfile(
        model=model_name,
        pooling=pooling,
        query_prefix=query,
        passage_prefix=passage,
        max_seq=int(max_seq) if isinstance(max_seq, int) and max_seq > 0 else _DEFAULT_MAX_SEQ,
        pad_token=str(tokenizer_pad) if isinstance(tokenizer_pad, str) and tokenizer_pad else pad,
    )


def _pool(hidden: np.ndarray, mask: np.ndarray, pooling: str) -> np.ndarray:
    """Pool token states the way the model was trained to: CLS, or the mean of
    the tokens the attention mask keeps (padding never counts)."""
    if pooling == _POOLING_CLS:
        return hidden[:, 0]
    if pooling == "mean":
        weights = mask[..., None].astype(hidden.dtype)
        return (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)
    raise ValueError(f"unsupported pooling {pooling!r}")


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
        runtime_resources.configure_torch()
        from sentence_transformers import SentenceTransformer

        model = model_cache.load_offline_first(
            model_name,
            lambda **kw: SentenceTransformer(model_name, device=device, **kw),
        )
        self.profile = profile_from_sentence_transformer(model_name, model)
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

    Reimplements only what sentence-transformers does: tokenize, run the encoder,
    pool the way the model's profile says (CLS for bge, the masked mean for e5 and
    MiniLM), L2-normalise. Padding uses the tokenizer's own padding token, which
    is `[PAD]` (id 0) for BERT vocabularies and `<pad>` (id 1) for XLM-R ones.
    There is no torch here, which is the entire reason the hosted image can shed
    ~300 MiB per cell.
    """

    backend = ONNX

    def __init__(self, model_name: str, device: str) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.profile = read_profile(model_name)
        onnx_path = _resolve(model_name, self.profile.onnx_file)
        tokenizer_path = _resolve(model_name, "tokenizer.json")

        self._tokenizer = Tokenizer.from_file(tokenizer_path)
        self._tokenizer.enable_truncation(max_length=self.profile.max_seq)
        pad_id = self._tokenizer.token_to_id(self.profile.pad_token)
        if pad_id is None:
            raise ValueError(f"{model_name}: padding token {self.profile.pad_token!r} is not in its tokenizer")
        self._tokenizer.enable_padding(pad_id=pad_id, pad_token=self.profile.pad_token)

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        runtime_resources.configure_onnx_session_options(options)
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
        # Collapse whitespace first. A sentencepiece tokenizer (XLM-R: e5, MiniLM)
        # strips and collapses it in its own normaliser, which the exported
        # `tokenizer.json` does not reproduce: a trailing space became an extra
        # word-boundary token and moved e5's vector to cosine 0.96 against torch.
        # A BERT tokenizer uses whitespace only as a separator, so for bge this
        # changes no token id.
        encodings = self._tokenizer.encode_batch([" ".join(text.split()) for text in batch])
        feed = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
        }
        hidden = self._session.run(None, {k: v for k, v in feed.items() if k in self._inputs})[0]
        pooled = _pool(hidden, feed["attention_mask"], self.profile.pooling)
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
