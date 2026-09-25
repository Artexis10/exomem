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

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
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
#: Every served model reads at most this many tokens, whatever its repository
#: declares (bge-m3 declares 8192). Passages are chunked below it upstream, and
#: attention cost grows with the square of the window.
SERVED_MAX_SEQ = 512

#: Where a served model's built artefact lives. Unset, it sits beside the hub
#: cache (`HF_HOME`), so an image that mounts that as a volume keeps it too.
ARTIFACT_DIR_ENV = "EXOMEM_MODEL_ARTIFACT_DIR"
#: ONNX Runtime's dynamic quantiser, int8 weights (`QInt8`, `MatMulConstBOnly`).
ORT_DYNAMIC_INT8 = "ort-dynamic-int8"
#: An ONNX graph whose weights live in one external `<graph>.data` file.
ONNX_EXTERNAL_DATA = "onnx-external-data"
_ARTIFACT_FILES = ("model.onnx", "model.onnx.data")
_ARTIFACT_MANIFEST = "artifact.json"
#: Where a served model's published artefact is downloaded from: a base URL the
#: asset name (`artifact_asset_name`) is appended to. Empty or ``off`` disables
#: the download, and so does `HF_HUB_OFFLINE`; the host then builds locally.
ARTIFACT_URL_ENV = "EXOMEM_MODEL_ARTIFACT_URL"
DEFAULT_ARTIFACT_URL = "https://github.com/Artexis10/exomem/releases/download/served-models"
_URL_OFF = {"", "0", "off", "none", "false", "no"}
#: A published asset is bounded; a response past this is not an artefact.
_MAX_ASSET_BYTES = 2 << 30
#: The most an asset's members may unpack to, summed: the asset's own cap, since
#: a plain tar of regular files is never smaller than what it holds.
_MAX_UNPACKED_BYTES = _MAX_ASSET_BYTES
#: How long a failed acquisition (no download, no build) is remembered before
#: a load in the same process tries again: a day, so a host that cannot build
#: does not start the 8.7 GB build every few minutes. A restart tries at once.
ACQUIRE_RETRY_SECONDS = 24 * 3600.0
_ACQUIRE_FAILED: dict[tuple[str, str], tuple[float, str]] = {}
#: The memory a local build must find available before it starts: the quantiser
#: peaks at about 8.7 GB for bge-m3. Below it the kernel kills the child, after
#: the 2.2 GB export was downloaded and copied for nothing.
BUILD_MIN_AVAILABLE_BYTES = 10 << 30
_MEMINFO = Path("/proc/meminfo")
#: Intra-op threads a served model's session uses while `EXOMEM_CPU_THREADS` is
#: unset: the 40-token turn's p95 was 132 ms at two against 304 at one.
SERVED_DEFAULT_THREADS = 2


class ModelFilesUnavailable(RuntimeError):
    """A model file the load needs is neither resident nor fetchable."""


class Encoder(Protocol):
    """What `embeddings` needs from a serving runtime, and nothing more."""

    #: Backend identifier, for logging and readiness reporting.
    backend: str
    #: Device the model actually landed on, in torch's vocabulary.
    device: str
    #: How the model encodes — pooling, prefixes, limit — and so which vectors.
    profile: EncoderProfile
    #: Whether two threads may encode on this one instance at once. ONNX
    #: Runtime's `InferenceSession.run` may; sentence-transformers reconfigures
    #: its tokenizer per call, so the torch lane may not.
    concurrent_encodes: bool

    def encode(
        self,
        texts: list[str],
        *,
        batch_size: int,
        normalize_embeddings: bool = True,
        convert_to_numpy: bool = True,
        show_progress_bar: bool = False,
        max_tokens: int | None = None,
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
    "BAAI/bge-m3": ("", "", _POOLING_CLS, "<pad>"),
}
_UNDECLARED = ("", "", _POOLING_CLS, "[PAD]")


@dataclass(frozen=True)
class ServedArtifact:
    """The exact bytes a declared model is served from, built once per host.

    ``revision`` pins the repository commit every file of the model is read at,
    so a moving ``main`` never moves the vectors. ``source`` is the published
    ONNX export at that commit, graph first; it is quantised as
    ``quantization`` says into ``file_format``.
    """

    revision: str
    source: tuple[str, ...]
    quantization: str
    file_format: str
    #: sha256 of the published artefact's bytes (`artifact_sha256`); None when
    #: nothing is published and every host builds its own.
    digest: str | None = None
    #: sha256 of the packed release asset itself, checked before it is unpacked;
    #: None when there is no asset to fetch.
    asset_digest: str | None = None


#: The one model a personal server serves for activation and recall alike, at
#: about 0.65 GB resident. Its int8 anchor-contact verdicts equal fp32's on the
#: multilingual fixture, which no other candidate's did (step-4 design §16.6).
_SERVED: dict[str, ServedArtifact] = {
    "BAAI/bge-m3": ServedArtifact(
        revision="5617a9f61b028005a4858fdac845db406aefb181",
        source=("onnx/model.onnx", "onnx/model.onnx_data", "onnx/Constant_7_attr__value"),
        quantization=ORT_DYNAMIC_INT8,
        file_format=ONNX_EXTERNAL_DATA,
        digest="7b9a0b3b0b292643752db19f9ae29113d632ac30393c9a56b97d4b075dc81c44",
        asset_digest="71e5f90fa019c2de0471b158e408da6ca7f43c93188308095242dfe46525ef76",
    ),
}


def served_artifact(model_name: str) -> ServedArtifact | None:
    """The artefact `model_name` is served from, or None for a hub-file model."""
    return _SERVED.get(model_name)


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
    #: A served model's pinned commit, quantisation, file format and the digest
    #: of the built bytes. None for a model served from its hub files.
    revision: str | None = None
    quantization: str | None = None
    file_format: str | None = None
    artifact_digest: str | None = None

    def fingerprint(self) -> str:
        """Identity of the vector space: model, pooling, prefixes, limit, L2,
        and for a served model the exact bytes it runs: revision, quantisation,
        file format and their digest.

        Never the backend, the padding token or the ONNX file: those are how a
        runtime computes a vector, not which vector it computes, and a backend
        swap must not look like a model change. A model with no served artefact
        keeps the fingerprint it always had.
        """
        fields: dict[str, object] = {
            "model": self.model,
            "pooling": self.pooling,
            "query_prefix": self.query_prefix,
            "passage_prefix": self.passage_prefix,
            "max_seq": self.max_seq,
            "normalize": "l2",
        }
        for name in ("revision", "quantization", "file_format", "artifact_digest"):
            value = getattr(self, name)
            if value is not None:
                fields[name] = value
        identity = json.dumps(fields, sort_keys=True)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return f"{self.model}|{self.pooling}|l2|{digest}"


def _read_json(model_name: str, filename: str) -> dict | None:
    try:
        with open(_model_file(model_name, filename), encoding="utf-8") as fh:
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
    three small files from the local hub cache, offline-first, at the model's
    pinned revision when it has one. A served model's artefact digest is known
    only once the artefact is built, so the encoder that builds it adds it.
    """
    query, passage, pooling, pad = _DECLARED.get(model_name, _UNDECLARED)
    served = served_artifact(model_name)
    return EncoderProfile(
        model=model_name,
        pooling=_pooling_from_config(_read_json(model_name, "1_Pooling/config.json")) or pooling,
        query_prefix=query,
        passage_prefix=passage,
        max_seq=_max_seq_length(model_name),
        pad_token=_pad_from_config(_read_json(model_name, "tokenizer_config.json")) or pad,
        revision=served.revision if served else None,
        quantization=served.quantization if served else None,
        file_format=served.file_format if served else None,
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
        max_seq=min(int(max_seq), SERVED_MAX_SEQ) if isinstance(max_seq, int) and max_seq > 0 else _DEFAULT_MAX_SEQ,
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


def require_tokenizer(model_name: str) -> str:
    """The model's `tokenizer.json`, or a refusal to load the model at all.

    Without that file, transformers rebuilds a tokenizer from whatever else the
    snapshot holds, and offline it can turn CJK text into `<unk>` or into
    nothing, with no error: every vector of such text would be silently wrong.
    A load that cannot have the file, resident or fetched, does not happen.
    """
    try:
        return _model_file(model_name, "tokenizer.json")
    except Exception as error:  # noqa: BLE001 — any resolution failure is the same refusal
        raise ModelFilesUnavailable(
            f"{model_name}: tokenizer.json is neither in the model cache nor fetchable, "
            "and without it non-Latin text would encode wrongly; fetch the model with "
            "network access once, then offline loads work"
        ) from error


class _TorchEncoder:
    """sentence-transformers, unchanged in behaviour from the pre-seam path."""

    backend = TORCH
    concurrent_encodes = False

    def __init__(self, model_name: str, device: str, half: bool) -> None:
        # The tokenizer guard is the first thing a load does, before any import
        # that a lean install may not have.
        require_tokenizer(model_name)
        # Heavy import stays local — keyword-mode and a lean install must not pay it.
        runtime_resources.configure_torch()
        from sentence_transformers import SentenceTransformer

        model = model_cache.load_offline_first(
            model_name,
            lambda **kw: SentenceTransformer(model_name, device=device, **kw),
        )
        self.profile = profile_from_sentence_transformer(model_name, model)
        limit = getattr(model, "max_seq_length", None)
        if isinstance(limit, int) and limit > self.profile.max_seq:
            model.max_seq_length = self.profile.max_seq
        self._model = _maybe_half(model, device) if half else model
        self.device = device

    def encode(self, texts, *, max_tokens: int | None = None, **kwargs) -> np.ndarray:
        kwargs.setdefault("convert_to_numpy", True)
        kwargs.setdefault("normalize_embeddings", True)
        kwargs.setdefault("show_progress_bar", False)
        if max_tokens is None:
            return self._model.encode(texts, **kwargs)
        # Only safe because this lane never encodes concurrently: every caller
        # holds the execution slot that serialises this instance. The limit
        # counts the model's special tokens, the cap does not.
        specials = getattr(getattr(self._model, "tokenizer", None), "num_special_tokens_to_add", None)
        limit = self._model.max_seq_length
        self._model.max_seq_length = min(limit, max_tokens + (specials(pair=False) if callable(specials) else 0))
        try:
            return self._model.encode(texts, **kwargs)
        finally:
            self._model.max_seq_length = limit

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

    A served model runs its built artefact instead of the hub export, on CPU
    only: its int8 parity with fp32 was measured there, and nowhere else.
    """

    backend = ONNX
    concurrent_encodes = True

    def __init__(self, model_name: str, device: str) -> None:
        # The tokenizer guard is the first thing a load does, before any import
        # that a lean install may not have.
        tokenizer_path = require_tokenizer(model_name)
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.profile = read_profile(model_name)
        served = served_artifact(model_name)
        if served is None:
            onnx_path = _model_file(model_name, self.profile.onnx_file)
            providers = _providers(device)
        else:
            onnx_path, digest = ensure_artifact(model_name, served)
            self.profile = dataclasses.replace(self.profile, artifact_digest=digest)
            device, providers = "cpu", ["CPUExecutionProvider"]

        self._tokenizer = Tokenizer.from_file(tokenizer_path)
        self._tokenizer.enable_truncation(max_length=self.profile.max_seq)
        pad_id = self._tokenizer.token_to_id(self.profile.pad_token)
        if pad_id is None:
            raise ValueError(f"{model_name}: padding token {self.profile.pad_token!r} is not in its tokenizer")
        self._tokenizer.enable_padding(pad_id=pad_id, pad_token=self.profile.pad_token)
        self._pad_id = pad_id

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if served is None:
            runtime_resources.configure_onnx_session_options(options)
        else:
            runtime_resources.configure_onnx_session_options(options, default_threads=SERVED_DEFAULT_THREADS)
        self._session = ort.InferenceSession(onnx_path, sess_options=options, providers=providers)
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
        max_tokens: int | None = None,
    ) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        # Dynamic int8 quantises each activation tensor with one scale, so in a
        # shared batch a text's vector moves with its neighbours (up to 0.02
        # cosine on bge-m3). One text per run keeps a vector a function of its
        # text alone, which reuse by text assumes; on CPU it is also faster
        # than padding a batch (measured 396 against 624 s per 1,000 anchors).
        step = 1 if self.profile.quantization == ORT_DYNAMIC_INT8 else max(1, batch_size)
        out: list[np.ndarray] = []
        for start in range(0, len(texts), step):
            out.append(self._encode_batch(texts[start : start + step], normalize_embeddings, max_tokens))
        return np.vstack(out)

    def _encode_batch(self, batch: list[str], normalize: bool, max_tokens: int | None = None) -> np.ndarray:
        # Collapse whitespace first. A sentencepiece tokenizer (XLM-R: e5, MiniLM)
        # strips and collapses it in its own normaliser, which the exported
        # `tokenizer.json` does not reproduce: a trailing space became an extra
        # word-boundary token and moved e5's vector to cosine 0.96 against torch.
        # A BERT tokenizer uses whitespace only as a separator, so for bge this
        # changes no token id.
        encodings = self._tokenizer.encode_batch([" ".join(text.split()) for text in batch])
        if max_tokens is not None:
            feed = self._capped_feed(encodings, max_tokens)
        else:
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

    def _capped_feed(self, encodings, limit: int) -> dict[str, np.ndarray]:
        """The batch with each text read at its first `limit` tokens.

        The model's leading and trailing special tokens stay on top of the
        `limit`, which is what the tokenizer's own right-hand truncation of a
        single sequence at `limit` plus those tokens gives. Truncation is the
        tokenizer's shared state, and setting it while another thread encodes
        raises; so the cap is applied to the token rows instead.
        """
        rows: list[list[int]] = []
        for encoding in encodings:
            kept = [i for i, attended in enumerate(encoding.attention_mask) if attended]
            ids = [encoding.ids[i] for i in kept]
            special = [encoding.special_tokens_mask[i] for i in kept]
            head = next((i for i, flag in enumerate(special) if not flag), len(ids))
            tail = next((i for i, flag in enumerate(reversed(special)) if not flag), 0)
            if len(ids) - head - tail > limit:
                ids = ids[: head + limit] + ids[len(ids) - tail :]
            rows.append(ids)
        width = max(len(row) for row in rows)
        return {
            "input_ids": np.array([row + [self._pad_id] * (width - len(row)) for row in rows], dtype=np.int64),
            "attention_mask": np.array([[1] * len(row) + [0] * (width - len(row)) for row in rows], dtype=np.int64),
            "token_type_ids": np.zeros((len(rows), width), dtype=np.int64),
        }

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


def _resolve(model_name: str, filename: str, revision: str | None = None) -> str:
    """Path to a model file, from the local hub cache when the snapshot is resident."""
    from huggingface_hub import hf_hub_download

    return model_cache.load_offline_first(
        model_name,
        lambda **kw: hf_hub_download(model_name, filename, revision=revision, **kw),
    )


def _model_file(model_name: str, filename: str) -> str:
    """A model file at the model's pinned revision when it is a served model."""
    served = served_artifact(model_name)
    if served is None:
        return _resolve(model_name, filename)
    return _resolve(model_name, filename, served.revision)


def _max_seq_length(model_name: str) -> int:
    """The model's declared sequence limit, defaulting to BERT's 512 and never above it."""
    try:
        with open(_model_file(model_name, "sentence_bert_config.json"), encoding="utf-8") as fh:
            declared = int(json.load(fh).get("max_seq_length") or _DEFAULT_MAX_SEQ)
    except Exception:  # noqa: BLE001 — a missing config must not block loading
        log.debug("no sentence_bert_config for %s; using %d", model_name, _DEFAULT_MAX_SEQ)
        return _DEFAULT_MAX_SEQ
    return min(declared, SERVED_MAX_SEQ)


def artifact_dir(model_name: str, served: ServedArtifact) -> Path:
    """Where `model_name`'s served artefact is built: one directory per revision
    and quantisation, so a new pin never overwrites the bytes an old one named."""
    configured = os.environ.get(ARTIFACT_DIR_ENV, "").strip()
    root = Path(configured).expanduser() if configured else model_cache.hub_dir().parent / "exomem-artifacts"
    return root / model_cache.snapshot_dirname(model_name) / served.revision / served.quantization


def artifact_sha256(target: Path) -> str | None:
    """sha256 over the artefact's file names and bytes, or None when a file is
    missing. Its first 16 hex digits are the digest the fingerprint names."""
    digest = hashlib.sha256()
    try:
        for name in _ARTIFACT_FILES:
            digest.update(name.encode("utf-8") + b"\0")
            with open(target / name, "rb") as fh:
                while chunk := fh.read(8 << 20):
                    digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _artifact_digest(target: Path) -> str | None:
    full = artifact_sha256(target)
    return full[:16] if full else None


def artifact_asset_name(model_name: str, served: ServedArtifact, digest: str | None = None) -> str:
    """The release asset's file name, e.g. ``bge-m3-int8-7b9a0b3b.onnx.tar``."""
    quant = "int8" if served.quantization == ORT_DYNAMIC_INT8 else served.quantization
    return f"{model_name.rsplit('/', 1)[-1].lower()}-{quant}-{(digest or served.digest or '')[:8]}.onnx.tar"


def artifact_url(model_name: str, served: ServedArtifact) -> str | None:
    """The published artefact's URL, or None when there is none to fetch."""
    if not served.digest or not served.asset_digest:
        return None
    if model_cache._truthy(os.environ.get(model_cache.HF_OFFLINE_ENV)):
        return None
    base = os.environ.get(ARTIFACT_URL_ENV)
    base = DEFAULT_ARTIFACT_URL if base is None else base.strip()
    if base.lower() in _URL_OFF:
        return None
    return f"{base.rstrip('/')}/{artifact_asset_name(model_name, served)}"


def write_artifact_asset(model_name: str, served: ServedArtifact, out_dir: Path) -> Path:
    """Pack the built artefact as its release asset, byte for byte reproducible:
    two regular files, fixed order, zero owner and time, no extended headers."""
    source = artifact_dir(model_name, served)
    digest = artifact_sha256(source)
    if digest is None:
        raise ModelFilesUnavailable(f"{model_name}: no built artefact at {source}")
    out_dir.mkdir(parents=True, exist_ok=True)
    asset = out_dir / artifact_asset_name(model_name, served, digest)
    with tarfile.open(asset, "w", format=tarfile.USTAR_FORMAT) as tar:
        for name in _ARTIFACT_FILES:
            info = tarfile.TarInfo(name)
            info.size = (source / name).stat().st_size
            info.mode = 0o644
            with open(source / name, "rb") as fh:
                tar.addfile(info, fh)
    return asset


def _read_manifest(target: Path) -> dict:
    try:
        data = json.loads((target / _ARTIFACT_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def ensure_artifact(model_name: str, served: ServedArtifact) -> tuple[str, str]:
    """The served ONNX graph's path and the digest of its bytes, built on first use.

    The bytes are hashed on every load (about 0.4 s for bge-m3's 0.55 GB), so the
    fingerprint names what actually runs. Bytes that no longer match the digest
    they were built with are rebuilt; the build is deterministic, so a repaired
    artefact has its old digest back and nothing stored under it is re-embedded.

    Acquiring it happens under a lock beside it (`_acquisition_lock`), so two
    loads, in one process or in two, never fetch or build it at once: the second
    waits, then finds the first one's artefact. A failure is not tried again in
    this process for `ACQUIRE_RETRY_SECONDS`.
    """
    target = artifact_dir(model_name, served)
    expected = {
        "model": model_name,
        "revision": served.revision,
        "quantization": served.quantization,
        "file_format": served.file_format,
    }
    digest = _installed_digest(target, expected)
    if digest is not None:
        return str(target / _ARTIFACT_FILES[0]), digest
    key = (model_name, served.revision)
    _refuse_while_cooling(key)
    with _acquisition_lock(target):
        # Whoever held the lock before may have installed it meanwhile.
        digest = _installed_digest(target, expected)
        if digest is not None:
            return str(target / _ARTIFACT_FILES[0]), digest
        _refuse_while_cooling(key)
        _sweep_stages(target)
        try:
            if not _fetch_artifact(model_name, served, target):
                log.info("building %s %s artefact at %s", model_name, served.quantization, target)
                _build_artifact(model_name, served, target)
        except ModelFilesUnavailable as error:
            _ACQUIRE_FAILED[key] = (time.monotonic(), str(error))
            raise
        _ACQUIRE_FAILED.pop(key, None)
        digest = _artifact_digest(target)
        if digest is None:
            raise ModelFilesUnavailable(f"{model_name}: the built artefact at {target} is incomplete")
        if served.digest and not served.digest.startswith(digest):
            log.info("%s: the local build's digest %s differs from the published one", model_name, digest)
        (target / _ARTIFACT_MANIFEST).write_text(
            json.dumps({**expected, "digest": digest}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return str(target / _ARTIFACT_FILES[0]), digest


def _installed_digest(target: Path, expected: dict[str, str]) -> str | None:
    """The installed artefact's digest when its manifest vouches for exactly these bytes."""
    digest = _artifact_digest(target)
    manifest = _read_manifest(target)
    if digest is None or manifest.get("digest") != digest or any(manifest.get(k) != v for k, v in expected.items()):
        return None
    return digest


def _refuse_while_cooling(key: tuple[str, str]) -> None:
    failed = _ACQUIRE_FAILED.get(key)
    if failed is not None and time.monotonic() - failed[0] < ACQUIRE_RETRY_SECONDS:
        raise ModelFilesUnavailable(
            f"{failed[1]} (not tried again until the process restarts or {int(ACQUIRE_RETRY_SECONDS // 3600)} h pass)"
        )


@contextlib.contextmanager
def _acquisition_lock(target: Path):
    """Hold the lock that makes acquiring `target` one caller's work at a time.

    `.<name>.lock` beside `target`, taken with `flock` (a byte-range lock on
    Windows) on its own open file, so it excludes threads of one process as
    well as other processes. The kernel releases it when its holder dies, so a
    killed build never leaves it held. The file stays: removing a lock file
    races with the next waiter.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target.parent / f".{target.name}.lock", "a+b") as handle:
        if sys.platform == "win32":  # pragma: no cover - exercised on Windows hosts
            import msvcrt

            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.5)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _sweep_stages(target: Path) -> None:
    """Remove the stages a killed fetch or build of `target` left beside it.

    Called only under `target`'s acquisition lock, when no live fetch or build
    of it can exist, so every stage found belongs to a dead one: up to 2.2 GB of
    copied export each, which nothing else would ever reclaim. Another target's
    stages have their own lock and are left alone.
    """
    for pattern in (f".{target.name}-fetch-*", f".{target.name}-build-*"):
        for stage in target.parent.glob(pattern):
            if stage.is_dir():
                log.info("removing the stale stage %s a killed acquisition left", stage)
                shutil.rmtree(stage, ignore_errors=True)


def _available_memory() -> int | None:
    """Bytes of memory a new process can have, or None when the host does not say.

    `MemAvailable` where the kernel reports it (Linux); elsewhere the free
    physical pages, which leave out reclaimable cache and so read low.
    """
    try:
        with open(_MEMINFO, encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None


def ensure_served_artifact(model_name: str) -> None:
    """Fetch or build a served model's artefact without loading it (quiet-mode warm-up)."""
    served = served_artifact(model_name)
    if served is not None:
        ensure_artifact(model_name, served)


def _fetch_artifact(model_name: str, served: ServedArtifact, target: Path) -> bool:
    """Install the published artefact into `target` when it is the pinned one.

    False, and nothing installed, when there is none to fetch, the download
    fails, or the asset is not exactly the pinned artefact. The downloaded file
    must hash to `served.asset_digest` before it is opened at all; then it is
    read as a plain tar (no decompression) of exactly two regular files whose
    sizes sum within the cap, and what they unpack to must hash to
    `served.digest`. The caller then builds locally, which is always correct
    and only costs memory and time.
    """
    url = artifact_url(model_name, served)
    if url is None:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}-fetch-", dir=target.parent))
    try:
        asset = stage / "asset.tar"
        received_digest = hashlib.sha256()
        try:
            with urllib.request.urlopen(url, timeout=60) as response, open(asset, "wb") as out:  # noqa: S310 — https or an operator's mirror
                received = 0
                while chunk := response.read(8 << 20):
                    received += len(chunk)
                    if received > _MAX_ASSET_BYTES:
                        log.warning("%s: %s is larger than any artefact; building locally", model_name, url)
                        return False
                    received_digest.update(chunk)
                    out.write(chunk)
        except (OSError, ValueError) as error:
            log.info("%s: no published artefact at %s (%s); building locally", model_name, url, error)
            return False
        if received_digest.hexdigest() != served.asset_digest:
            log.warning("%s: %s is not the pinned asset; refused unopened, building locally", model_name, url)
            return False
        out_dir = stage / "out"
        out_dir.mkdir()
        try:
            # "r:" reads a plain tar only: a compressed stream is refused, never
            # inflated. Regular members only, so a sparse member cannot unpack
            # to more than it occupies.
            with tarfile.open(asset, "r:") as tar:
                members = tar.getmembers()
                if (
                    sorted(m.name for m in members) != sorted(_ARTIFACT_FILES)
                    or not all(m.type in (tarfile.REGTYPE, tarfile.AREGTYPE) for m in members)
                    or sum(m.size for m in members) > _MAX_UNPACKED_BYTES
                ):
                    log.warning("%s: %s holds other files than the artefact; refused", model_name, url)
                    return False
                for member in members:
                    src = tar.extractfile(member)
                    if src is None:
                        return False
                    with src, open(out_dir / member.name, "wb") as dst:
                        shutil.copyfileobj(src, dst, 8 << 20)
        except (OSError, tarfile.TarError) as error:
            log.warning("%s: %s is not a readable artefact (%s); refused", model_name, url, error)
            return False
        if artifact_sha256(out_dir) != served.digest:
            log.warning("%s: %s does not match the pinned digest; refused, building locally", model_name, url)
            return False
        target.mkdir(parents=True, exist_ok=True)
        for name in reversed(_ARTIFACT_FILES):
            os.replace(out_dir / name, target / name)
        log.info("installed the published %s artefact from %s", model_name, url)
        return True
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _build_artifact(model_name: str, served: ServedArtifact, target: Path) -> None:
    """Quantise the pinned export into `target`, via a stage beside it.

    The hub cache holds the export as symlinks into its blob store, and onnx
    refuses external data behind a symlink or a hard link, so the source files
    are copied into the stage first. Files move into `target` data first, so a
    graph never names data that is not there.
    """
    # Before the export is downloaded or copied: a build the host cannot hold
    # ends with the child killed, and the published artefact is the way out.
    # What this costs when it misreads a host: no build there, and semantic
    # evidence stays off until the artefact arrives. An unmeasurable host builds.
    available = _available_memory()
    if available is not None and available < BUILD_MIN_AVAILABLE_BYTES:
        raise ModelFilesUnavailable(
            f"{model_name}: not building the int8 model here: {available / (1 << 30):.1f} GiB of memory "
            f"is available and the build needs {BUILD_MIN_AVAILABLE_BYTES >> 30} GiB. Use the published "
            f"artefact {artifact_asset_name(model_name, served)} instead: it downloads from "
            f"{ARTIFACT_URL_ENV} (default {DEFAULT_ARTIFACT_URL}), which must be reachable and not off"
        )
    try:
        sources = [Path(_resolve(model_name, name, served.revision)).resolve() for name in served.source]
    except Exception as error:  # noqa: BLE001 — every hub failure is the same refusal
        # `LocalEntryNotFoundError` offline, the hub's not-found errors online:
        # each is a failed acquisition, remembered so no load retries it at once.
        raise ModelFilesUnavailable(
            f"{model_name}: the export to build from is neither in the model cache nor fetchable "
            f"({type(error).__name__})"
        ) from error
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}-build-", dir=target.parent))
    try:
        (stage / "source").mkdir()
        (stage / "out").mkdir()
        for name, path in zip(served.source, sources, strict=True):
            shutil.copyfile(path, stage / "source" / Path(name).name)
        _quantize(
            served,
            str(stage / "source" / Path(served.source[0]).name),
            str(stage / "out" / _ARTIFACT_FILES[0]),
        )
        target.mkdir(parents=True, exist_ok=True)
        for name in reversed(_ARTIFACT_FILES):
            os.replace(stage / "out" / name, target / name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


#: The quantiser, run as `python -c` with the source and output graph paths.
_QUANTIZE_CHILD = (
    "import sys\n"
    "from onnxruntime.quantization import QuantType, quantize_dynamic\n"
    "quantize_dynamic(sys.argv[1], sys.argv[2], weight_type=QuantType.QInt8,"
    " use_external_data_format=True, extra_options={'MatMulConstBOnly': True})\n"
)


def _quantize(served: ServedArtifact, source: str, out: str) -> None:
    """Quantise in a child process.

    The quantiser holds the fp32 graph and its int8 copy at once, 8.7 GB at peak
    for bge-m3. In a child, that memory goes back to the host when the child
    exits, and running out of it ends the build rather than the server.
    """
    if served.quantization != ORT_DYNAMIC_INT8 or served.file_format != ONNX_EXTERNAL_DATA:
        raise ValueError(f"no builder for {served.quantization} as {served.file_format}")
    result = subprocess.run(
        [sys.executable, "-c", _QUANTIZE_CHILD, source, out],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode < 0:
        # A signal, and on a build this size almost always the kernel's OOM killer.
        raise ModelFilesUnavailable(
            f"building the int8 model was killed by signal {-result.returncode} "
            "(likely out of memory: the build peaks near 9 GB)"
        )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()[-1:] or [f"exit {result.returncode}"]
        raise ModelFilesUnavailable(
            f"building the int8 model failed ({detail[0]}); it needs onnxruntime and onnx, "
            "which exomem[embeddings] installs"
        )


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
    A served model runs its ONNX artefact whatever backend is preferred: torch
    cannot run those bytes, and they are what its fingerprint names. It runs on
    CPU without probing for an accelerator.
    """
    served = served_artifact(model_name) is not None
    chosen = ONNX if served else backend or resolve_backend()
    device = "cpu" if served else accel.select_device(override_env="EXOMEM_EMBED_DEVICE")
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
