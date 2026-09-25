"""The activation encoder: its profile, fingerprint and own execution slot (step 4, T6),
and the one served model it becomes (T6b).

The activation index may run a different encoder from recall, which stays on
`BAAI/bge-base-en-v1.5`. Such an encoder carries a profile, read from the model
repository or from the loaded model and never guessed: its pooling, sequence
limit, padding token and the prefixes it was trained with. Its fingerprint
names the vector space and never the backend that computed it. A separate
activation model has its own guard and execution slot, so the interactive
query encode is never refused because a recall write holds the shared gate.
Nothing here loads a model on a request thread. Torch-free throughout: models
are fakes and repository files are temporary files.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

from exomem import (
    accel,
    embedding_backend,
    embeddings,
    mode,
    model_reaper,
    readiness,
    runtime_resources,
    warmup,
)

E5 = "intfloat/multilingual-e5-small"
M3 = "BAAI/bge-m3"
#: The real memory reading, before `_clean` stands a large host in for it.
_REAL_AVAILABLE_MEMORY = getattr(embedding_backend, "_available_memory", None)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv(embeddings.ACTIVATION_MODEL_ENV, raising=False)
    monkeypatch.setattr(embeddings, "_MODEL", None)
    monkeypatch.setattr(embeddings, "_ACTIVATION_MODEL", None)
    monkeypatch.setenv(embedding_backend.ARTIFACT_DIR_ENV, str(tmp_path / "artifacts"))
    # No test reaches a network: a published artefact is fetched from a local
    # directory that is empty unless a test publishes into it.
    monkeypatch.setenv("EXOMEM_MODEL_ARTIFACT_URL", (tmp_path / "no-assets").as_uri())
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setattr(embedding_backend, "_ACQUIRE_FAILED", {}, raising=False)
    # The tiny test builds need no 10 GiB; the tests of that gate set their own.
    monkeypatch.setattr(embedding_backend, "_available_memory", lambda: 64 << 30, raising=False)
    yield


def _repo(
    tmp_path: Path, files: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, str | None]]:
    """Serve `files` as a model repository through `embedding_backend._resolve`.

    Returns every `(filename, revision)` asked for, so a test can pin that a
    declared model is read at its pinned revision and never at a moving branch.
    """
    root = tmp_path / "repo"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(json.dumps(content) if not isinstance(content, str) else content, encoding="utf-8")
    asked: list[tuple[str, str | None]] = []

    def resolve(_model: str, filename: str, revision: str | None = None) -> str:
        asked.append((filename, revision))
        path = root / filename
        if not path.is_file():
            raise FileNotFoundError(filename)
        return str(path)

    monkeypatch.setattr(embedding_backend, "_resolve", resolve)
    return asked


# --------------------------------------------------------------------------- #
# Profile and fingerprint
# --------------------------------------------------------------------------- #


def test_profile_reads_mean_pooling_limit_and_pad_token_from_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(
        tmp_path,
        {
            "1_Pooling/config.json": {"pooling_mode_cls_token": False, "pooling_mode_mean_tokens": True},
            "sentence_bert_config.json": {"max_seq_length": 512},
            "tokenizer_config.json": {"pad_token": "<pad>"},
        },
        monkeypatch,
    )

    profile = embedding_backend.read_profile(E5)

    assert (profile.model, profile.pooling, profile.max_seq, profile.pad_token) == (E5, "mean", 512, "<pad>")
    assert (profile.query_prefix, profile.passage_prefix) == ("query: ", "passage: ")


def test_profile_reads_cls_pooling_from_the_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo(
        tmp_path,
        {
            "1_Pooling/config.json": {"pooling_mode_cls_token": True, "pooling_mode_mean_tokens": False},
            "sentence_bert_config.json": {"max_seq_length": 256},
            "tokenizer_config.json": {"pad_token": {"content": "[PAD]"}},
        },
        monkeypatch,
    )

    profile = embedding_backend.read_profile("some-org/cls-model")

    assert (profile.pooling, profile.max_seq, profile.pad_token) == ("cls", 256, "[PAD]")
    assert (profile.query_prefix, profile.passage_prefix) == ("", "")


def test_the_recall_model_keeps_its_cls_pooling_and_query_prefix_when_configs_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ONNX cache made before profiles existed holds no pooling config. Recall's
    own model must still load exactly as it did: CLS, `[PAD]`, its query prefix."""
    _repo(tmp_path, {}, monkeypatch)

    profile = embedding_backend.read_profile(embeddings.MODEL_NAME)

    assert (profile.pooling, profile.pad_token, profile.max_seq) == ("cls", "[PAD]", 512)
    assert profile.query_prefix == embeddings.QUERY_PREFIX
    assert profile.passage_prefix == ""


def test_fingerprint_moves_with_model_pooling_prefix_and_limit_only() -> None:
    base = embedding_backend.EncoderProfile(
        model=E5, pooling="mean", query_prefix="query: ", passage_prefix="passage: ", max_seq=512, pad_token="<pad>"
    )
    assert base.fingerprint() == base.fingerprint()
    for change in (
        {"model": "intfloat/multilingual-e5-base"},
        {"pooling": "cls"},
        {"query_prefix": ""},
        {"passage_prefix": ""},
        {"max_seq": 256},
    ):
        assert dataclasses.replace(base, **change).fingerprint() != base.fingerprint(), change
    # The padding token and the ONNX file are how a runtime computes the vector,
    # not which vector it computes.
    assert dataclasses.replace(base, pad_token="[PAD]").fingerprint() == base.fingerprint()
    assert dataclasses.replace(base, onnx_file="onnx/model_O4.onnx").fingerprint() == base.fingerprint()
    assert "onnx" not in base.fingerprint() and "torch" not in base.fingerprint()


def test_torch_and_onnx_read_the_same_profile_for_one_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fingerprint never names the backend: both lanes derive the same profile."""
    _repo(
        tmp_path,
        {
            "1_Pooling/config.json": {"pooling_mode_mean_tokens": True},
            "sentence_bert_config.json": {"max_seq_length": 512},
            "tokenizer_config.json": {"pad_token": "<pad>"},
        },
        monkeypatch,
    )

    class Pooling:
        def get_pooling_mode_str(self) -> str:
            return "mean"

    class Model:
        max_seq_length = 512
        tokenizer = types.SimpleNamespace(pad_token="<pad>")

        def __iter__(self):
            return iter([object(), Pooling()])

    from_torch = embedding_backend.profile_from_sentence_transformer(E5, Model())
    from_repo = embedding_backend.read_profile(E5)

    assert from_torch == from_repo
    assert from_torch.fingerprint() == from_repo.fingerprint()


# --------------------------------------------------------------------------- #
# ONNX pooling and padding follow the profile
# --------------------------------------------------------------------------- #


def test_onnx_mean_pooling_averages_only_the_attended_tokens() -> None:
    hidden = np.array([[[1.0, 0.0], [3.0, 0.0], [99.0, 99.0]]], dtype=np.float32)
    mask = np.array([[1, 1, 0]], dtype=np.int64)

    assert np.allclose(embedding_backend._pool(hidden, mask, "mean"), [[2.0, 0.0]])
    assert np.allclose(embedding_backend._pool(hidden, mask, "cls"), [[1.0, 0.0]])


def test_onnx_encoder_pads_and_pools_as_its_profile_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(
        tmp_path,
        {
            "onnx/model.onnx": "",
            "tokenizer.json": "",
            "1_Pooling/config.json": {"pooling_mode_mean_tokens": True},
            "sentence_bert_config.json": {"max_seq_length": 512},
            "tokenizer_config.json": {"pad_token": "<pad>"},
        },
        monkeypatch,
    )
    padding: dict[str, object] = {}

    class Encoding:
        def __init__(self, ids: list[int]) -> None:
            self.ids = ids
            self.attention_mask = [1 if token != 1 else 0 for token in ids]
            self.type_ids = [0] * len(ids)

    class Tokenizer:
        @staticmethod
        def from_file(_path: str) -> Tokenizer:
            return Tokenizer()

        def enable_truncation(self, max_length: int) -> None:
            padding["max_length"] = max_length

        def enable_padding(self, pad_id: int, pad_token: str) -> None:
            padding.update(pad_id=pad_id, pad_token=pad_token)

        def token_to_id(self, token: str) -> int | None:
            return {"<pad>": 1, "<s>": 0}.get(token)

        def encode_batch(self, texts: list[str]) -> list[Encoding]:
            padding["texts"] = list(texts)
            return [Encoding([0, 5, 6]), Encoding([0, 5, 1])]

    class Session:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_inputs(self):
            return [types.SimpleNamespace(name="input_ids"), types.SimpleNamespace(name="attention_mask")]

        def run(self, _outputs, feed):
            assert set(feed) == {"input_ids", "attention_mask"}
            hidden = np.zeros((2, 3, 2), dtype=np.float32)
            hidden[:, 0] = [1.0, 0.0]
            hidden[:, 1] = [0.0, 1.0]
            hidden[:, 2] = [0.0, 1.0]
            return [hidden]

    ort = types.ModuleType("onnxruntime")
    ort.SessionOptions = lambda: types.SimpleNamespace()
    ort.GraphOptimizationLevel = types.SimpleNamespace(ORT_ENABLE_ALL=99)
    ort.InferenceSession = Session
    ort.get_available_providers = lambda: ["CPUExecutionProvider"]
    tokenizers = types.ModuleType("tokenizers")
    tokenizers.Tokenizer = Tokenizer
    monkeypatch.setattr(accel, "select_device", lambda **_: "cpu")
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "tokenizers", tokenizers)

    encoder = embedding_backend.load_encoder(E5, backend=embedding_backend.ONNX)
    vectors = encoder.encode(["a  b ", "\tc\n"], batch_size=8)

    assert padding == {"max_length": 512, "pad_id": 1, "pad_token": "<pad>", "texts": ["a b", "c"]}
    assert encoder.profile.pooling == "mean"
    # Mean of the attended tokens, then L2: row 0 averages all three, row 1 skips the pad.
    assert np.allclose(vectors[0], np.array([1.0, 2.0]) / np.sqrt(5.0), atol=1e-6)
    assert np.allclose(vectors[1], np.array([1.0, 1.0]) / np.sqrt(2.0), atol=1e-6)


# --------------------------------------------------------------------------- #
# Topology: shared by default, a separate slot when configured
# --------------------------------------------------------------------------- #


def test_unset_or_equal_model_name_is_the_recall_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    resident = object()
    monkeypatch.setattr(embeddings, "_MODEL", resident)

    assert embeddings.activation_model_name() == embeddings.MODEL_NAME
    assert embeddings.activation_encoder_is_shared() is True
    assert embeddings.get_activation_model() is resident

    monkeypatch.setenv(embeddings.ACTIVATION_MODEL_ENV, embeddings.MODEL_NAME)
    assert embeddings.activation_encoder_is_shared() is True
    assert embeddings.get_activation_model() is resident

    monkeypatch.setenv(embeddings.ACTIVATION_MODEL_ENV, E5)
    assert embeddings.activation_model_name() == E5
    assert embeddings.activation_encoder_is_shared() is False


def test_a_separate_model_loads_into_its_own_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(embeddings.ACTIVATION_MODEL_ENV, E5)
    loaded: list[str] = []

    class Model:
        device = "cpu"

    monkeypatch.setattr(embedding_backend, "load_encoder", lambda name: loaded.append(name) or Model())
    monkeypatch.setattr(embeddings, "get_model", lambda: pytest.fail("the recall model must not load"))

    first = embeddings.get_activation_model()

    assert loaded == [E5]
    assert embeddings.get_activation_model() is first
    assert embeddings._MODEL is None


def _separate_resident(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    monkeypatch.setenv(embeddings.ACTIVATION_MODEL_ENV, E5)
    profile = embedding_backend.EncoderProfile(
        model=E5, pooling="mean", query_prefix="query: ", passage_prefix="passage: ", max_seq=512, pad_token="<pad>"
    )

    class Model:
        device = "cpu"

        def __init__(self) -> None:
            self.profile = profile

        def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
            calls.append((list(texts), kwargs))
            return np.ones((len(texts), 4), dtype=np.float64)

    monkeypatch.setattr(embeddings, "_ACTIVATION_MODEL", Model())


def test_query_and_passages_use_the_activation_profile_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _separate_resident(monkeypatch, calls)

    query = embeddings.embed_activation_query_if_loaded("wie war das")
    passages = embeddings.embed_activation_passages(["Sourdough cold proof", "Knee rehab"])

    assert query is not None and query.dtype == np.float32 and query.shape == (4,)
    assert passages.shape == (2, 4) and passages.dtype == np.float32
    assert calls[0][0] == ["query: wie war das"]
    assert calls[1][0] == ["passage: Sourdough cold proof", "passage: Knee rehab"]


def test_a_query_encode_is_never_refused_while_the_recall_gate_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recall write's bulk encode holds the process-wide gate for seconds; a
    separate activation encoder must not read `busy` behind it."""
    gate = runtime_resources.ModelAdmissionGate(4)
    monkeypatch.setattr(runtime_resources, "_gate", gate)
    monkeypatch.setattr(runtime_resources, "_gate_capacity", 4)
    calls: list = []
    _separate_resident(monkeypatch, calls)
    entered, release = threading.Event(), threading.Event()

    def hold_recall_gate() -> None:
        with runtime_resources.model_execution():
            entered.set()
            release.wait(timeout=5)

    worker = threading.Thread(target=hold_recall_gate)
    worker.start()
    try:
        assert entered.wait(timeout=1)
        assert embeddings.embed_activation_query_if_loaded("продолжим") is not None
        assert gate.admitted_count() == 1, "the activation encode must not enter the recall gate"
    finally:
        release.set()
        worker.join(timeout=1)


def test_the_activation_slot_refuses_without_waiting_when_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _separate_resident(monkeypatch, calls)
    entered, release = threading.Event(), threading.Event()

    def hold_activation_slot() -> None:
        with embeddings.activation_execution():
            entered.set()
            release.wait(timeout=5)

    worker = threading.Thread(target=hold_activation_slot)
    worker.start()
    try:
        assert entered.wait(timeout=1)
        with pytest.raises(runtime_resources.ModelBusyError):
            embeddings.embed_activation_query_if_loaded("continue")
        assert calls == []
    finally:
        release.set()
        worker.join(timeout=1)


def test_shared_topology_keeps_the_recall_query_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        embeddings,
        "embed_query_if_loaded",
        lambda text, **kw: seen.append((text, kw)) or np.zeros(3, dtype=np.float32),
    )

    assert embeddings.embed_activation_query_if_loaded("continue") is not None
    assert seen == [("continue", {"max_tokens": embeddings.ACTIVATION_TURN_MAX_TOKENS})]


def test_if_loaded_never_loads_either_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedding_backend, "load_encoder", lambda *_a, **_k: pytest.fail("no model load"))
    monkeypatch.setattr(embeddings, "get_model", lambda: pytest.fail("no recall model load"))

    assert embeddings.embed_activation_query_if_loaded("continue") is None
    monkeypatch.setenv(embeddings.ACTIVATION_MODEL_ENV, E5)
    assert embeddings.embed_activation_query_if_loaded("continue") is None


# --------------------------------------------------------------------------- #
# Lifecycle: reaper, live mode switch, warm-up preload
# --------------------------------------------------------------------------- #


def test_the_separate_model_is_a_reapable_model_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    slots = {slot.name: slot for slot in model_reaper.default_slots()}

    assert slots["activation"].is_model is True
    assert slots["activation"].is_loaded() is False
    monkeypatch.setattr(embeddings, "_ACTIVATION_MODEL", object())
    assert slots["activation"].is_loaded() is True
    assert embeddings.unload_activation_model() is True
    assert embeddings._ACTIVATION_MODEL is None


def test_a_live_mode_switch_unloads_the_separate_model(monkeypatch: pytest.MonkeyPatch) -> None:
    unloaded: list[str] = []
    monkeypatch.setattr(embeddings, "unload_activation_model", lambda: unloaded.append("activation") or True)
    monkeypatch.setattr(model_reaper, "start", lambda: None)
    monkeypatch.setattr(model_reaper, "stop", lambda: None)
    monkeypatch.setenv("EXOMEM_MODE", "normal")

    mode.apply_live()

    assert unloaded == ["activation"]


@pytest.mark.parametrize("configured, expected", [(None, []), (E5, ["activation"])])
def test_warm_up_preloads_a_separate_activation_model_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str | None, expected: list[str]
) -> None:
    if configured:
        monkeypatch.setenv(embeddings.ACTIVATION_MODEL_ENV, configured)
    monkeypatch.setenv("EXOMEM_PRELOAD_MODELS", "1")
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    readiness.reset()
    loads: list[str] = []
    monkeypatch.setattr(warmup, "warm_retrieval_catalog", lambda _root: True, raising=False)
    monkeypatch.setattr(warmup, "warm_caches", lambda _root, **_kw: {})
    monkeypatch.setattr("exomem.semantic_contract.build_corpus_context", lambda _root: None)
    monkeypatch.setattr(embeddings, "get_model", lambda: types.SimpleNamespace(encode=lambda _t: None))
    monkeypatch.setattr(embeddings, "get_reranker", lambda: types.SimpleNamespace(predict=lambda _p: None))
    monkeypatch.setattr(embeddings, "clip_enabled", lambda: False)
    monkeypatch.setattr(
        embeddings,
        "get_activation_model",
        lambda: loads.append("activation") or types.SimpleNamespace(encode=lambda _t: None),
    )

    try:
        warmup.warm_all(tmp_path)
    finally:
        readiness.reset()

    assert loads == expected


# --------------------------------------------------------------------------- #
# The served model: bge-m3 on ONNX Runtime int8 with external data (T6b)
# --------------------------------------------------------------------------- #

_M3_REPO = {
    "1_Pooling/config.json": {"pooling_mode_cls_token": True, "pooling_mode_mean_tokens": False},
    # The repository declares its full 8192-token window; the server reads 512.
    "sentence_bert_config.json": {"max_seq_length": 8192, "do_lower_case": False},
    "tokenizer_config.json": {"pad_token": "<pad>"},
}


def test_the_served_model_is_bge_m3_int8_with_external_data_at_512_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _repo(tmp_path, _M3_REPO, monkeypatch)

    profile = embedding_backend.read_profile(M3)
    served = embedding_backend.served_artifact(M3)

    assert served is not None and re.fullmatch(r"[0-9a-f]{40}", served.revision)
    assert (served.quantization, served.file_format) == (
        embedding_backend.ORT_DYNAMIC_INT8,
        embedding_backend.ONNX_EXTERNAL_DATA,
    )
    assert (profile.pooling, profile.max_seq, profile.pad_token) == ("cls", 512, "<pad>")
    assert (profile.query_prefix, profile.passage_prefix) == ("", "")
    assert (profile.revision, profile.quantization, profile.file_format) == (
        served.revision,
        served.quantization,
        served.file_format,
    )
    # Every file is read at the pinned commit, never at a moving branch.
    assert asked and {revision for _file, revision in asked} == {served.revision}
    assert embedding_backend.served_artifact(E5) is None


def test_every_served_model_is_capped_at_512_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    long_model = "some-org/long-model"
    _repo(
        tmp_path,
        {"sentence_bert_config.json": {"max_seq_length": 8192}, "tokenizer.json": "{}"},
        monkeypatch,
    )

    class Model:
        tokenizer = types.SimpleNamespace(pad_token="<pad>", num_special_tokens_to_add=lambda pair=False: 2)

        def __init__(self) -> None:
            self.max_seq_length = 8192
            self.read_at: list[int] = []

        def __iter__(self):
            return iter([])

        def encode(self, texts: list[str], **_kwargs: object) -> np.ndarray:
            self.read_at.append(self.max_seq_length)
            return np.ones((len(texts), 2), dtype=np.float32)

    loaded = Model()
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=lambda *_a, **_k: loaded),
    )
    monkeypatch.setattr(runtime_resources, "configure_torch", lambda: None)

    assert embedding_backend.read_profile(long_model).max_seq == 512
    assert embedding_backend.profile_from_sentence_transformer(long_model, Model()).max_seq == 512

    encoder = embedding_backend._TorchEncoder(long_model, "cpu", False)
    encoder.encode(["a long passage"], batch_size=8)
    encoder.encode(["a turn"], batch_size=8, max_tokens=40)
    encoder.encode(["a long passage"], batch_size=8)

    assert encoder.profile.max_seq == 512
    # The torch lane reads what its profile says, and a capped encode reads 40
    # of the text's own tokens, special tokens on top, for its own call only.
    assert loaded.read_at == [512, 42, 512]
    assert encoder.concurrent_encodes is False


def test_the_fingerprint_names_the_served_bytes() -> None:
    base = embedding_backend.EncoderProfile(
        model=M3,
        pooling="cls",
        query_prefix="",
        passage_prefix="",
        max_seq=512,
        pad_token="<pad>",
        revision="a" * 40,
        quantization=embedding_backend.ORT_DYNAMIC_INT8,
        file_format=embedding_backend.ONNX_EXTERNAL_DATA,
        artifact_digest="0123456789abcdef",
    )
    for change in (
        {"revision": "b" * 40},
        {"quantization": "fp32"},
        {"file_format": "onnx"},
        {"artifact_digest": "fedcba9876543210"},
    ):
        assert dataclasses.replace(base, **change).fingerprint() != base.fingerprint(), change
    assert dataclasses.replace(base, pad_token="[PAD]").fingerprint() == base.fingerprint()

    # A profile with no served artefact keeps the fingerprint it always had, so
    # nothing already filed under it reads as another vector space.
    plain = embedding_backend.EncoderProfile(
        model=E5, pooling="mean", query_prefix="query: ", passage_prefix="passage: ", max_seq=512, pad_token="<pad>"
    )
    legacy = json.dumps(
        {
            "model": E5,
            "pooling": "mean",
            "query_prefix": "query: ",
            "passage_prefix": "passage: ",
            "max_seq": 512,
            "normalize": "l2",
        },
        sort_keys=True,
    )

    assert plain.fingerprint() == f"{E5}|mean|l2|{hashlib.sha256(legacy.encode()).hexdigest()[:16]}"


def test_a_model_without_its_tokenizer_json_never_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline, transformers rebuilds a tokenizer from whatever else the snapshot
    holds and can turn CJK into `<unk>` or into nothing, with no error. A model
    whose `tokenizer.json` is neither resident nor fetchable does not load, on
    any lane: torch, ONNX, or the reranker."""
    _repo(tmp_path, {"sentence_bert_config.json": {"max_seq_length": 512}}, monkeypatch)
    constructed: list[str] = []
    st = types.ModuleType("sentence_transformers")
    st.SentenceTransformer = lambda *_a, **_k: constructed.append("bi-encoder")
    st.CrossEncoder = lambda *_a, **_k: constructed.append("cross-encoder")
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    monkeypatch.setattr(runtime_resources, "configure_torch", lambda: None)
    monkeypatch.setattr(accel, "select_device", lambda **_: "cpu")
    monkeypatch.setattr(embeddings, "_RERANKER", None)

    for backend in (embedding_backend.TORCH, embedding_backend.ONNX):
        with pytest.raises(embedding_backend.ModelFilesUnavailable, match="tokenizer.json"):
            embedding_backend.load_encoder(E5, backend=backend)
    with pytest.raises(embedding_backend.ModelFilesUnavailable, match="tokenizer.json"):
        embeddings.get_reranker()

    assert constructed == []
    assert embeddings._RERANKER is None


# --------------------------------------------------------------------------- #
# The served artefact is built once per host, from the pinned export
# --------------------------------------------------------------------------- #

TINY = "some-org/tiny-served"
TINY_REVISION = "0123456789abcdef0123456789abcdef01234567"
TINY_WIDTH = 32


def _tiny_served_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real, tiny ONNX export with external data, declared as a served model.

    Gather, Tanh, MatMul over a 64-word vocabulary: the quantiser rewrites the
    embedding table and the matrix, and the float activation between them is
    quantised per tensor at run time, as in the real model. Big enough that the
    quantised weights leave the graph for the external data file.
    """
    onnx = pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    from onnx import TensorProto, helper, numpy_helper
    from tokenizers import Tokenizer, models, pre_tokenizers, processors

    rng = np.random.default_rng(7)
    vocab = 64
    words = ["<s>", "<pad>", "</s>", "<unk>"] + [f"w{i}" for i in range(vocab - 4)]
    graph = helper.make_graph(
        [
            helper.make_node("Gather", ["table", "input_ids"], ["embedded"]),
            helper.make_node("Tanh", ["embedded"], ["activated"]),
            helper.make_node("MatMul", ["activated", "weight"], ["token_embeddings"]),
        ],
        "tiny",
        [helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "sequence"])],
        [helper.make_tensor_value_info("token_embeddings", TensorProto.FLOAT, ["batch", "sequence", TINY_WIDTH])],
        [
            numpy_helper.from_array(rng.standard_normal((vocab, TINY_WIDTH)).astype(np.float32), "table"),
            numpy_helper.from_array(rng.standard_normal((TINY_WIDTH, TINY_WIDTH)).astype(np.float32), "weight"),
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    export = tmp_path / "export"
    export.mkdir()
    onnx.save_model(
        model,
        str(export / "model.onnx"),
        save_as_external_data=True,
        all_tensors_to_one_file=True,
        location="model.onnx_data",
        size_threshold=0,
    )
    tokenizer = Tokenizer(models.WordLevel({word: i for i, word in enumerate(words)}, unk_token="<unk>"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="<s> $A </s>", special_tokens=[("<s>", 0), ("</s>", 2)]
    )
    asked = _repo(
        tmp_path,
        {
            "onnx/model.onnx": (export / "model.onnx").read_bytes(),
            "onnx/model.onnx_data": (export / "model.onnx_data").read_bytes(),
            "tokenizer.json": tokenizer.to_str(),
            "1_Pooling/config.json": {"pooling_mode_mean_tokens": True},
            "sentence_bert_config.json": {"max_seq_length": 512},
            "tokenizer_config.json": {"pad_token": "<pad>"},
        },
        monkeypatch,
    )
    served = embedding_backend.ServedArtifact(
        revision=TINY_REVISION,
        source=("onnx/model.onnx", "onnx/model.onnx_data"),
        quantization=embedding_backend.ORT_DYNAMIC_INT8,
        file_format=embedding_backend.ONNX_EXTERNAL_DATA,
    )
    monkeypatch.setitem(embedding_backend._SERVED, TINY, served)
    monkeypatch.setitem(embedding_backend._DECLARED, TINY, ("", "", "mean", "<pad>"))
    # Asked for an accelerator, the served artefact still runs where its parity was measured.
    monkeypatch.setattr(accel, "select_device", lambda **_: "cuda")
    return asked, served


def test_the_served_artifact_is_built_once_at_the_pinned_revision_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked, served = _tiny_served_repo(tmp_path, monkeypatch)
    quantized: list[tuple] = []
    real_quantize = embedding_backend._quantize
    monkeypatch.setattr(
        embedding_backend, "_quantize", lambda *args: quantized.append(args) or real_quantize(*args)
    )

    first = embedding_backend.load_encoder(TINY, backend=embedding_backend.TORCH)
    target = embedding_backend.artifact_dir(TINY, served)
    manifest = json.loads((target / "artifact.json").read_text(encoding="utf-8"))

    assert (first.backend, first.device, first.concurrent_encodes) == (embedding_backend.ONNX, "cpu", True)
    assert first._session.get_providers() == ["CPUExecutionProvider"]
    assert len(quantized) == 1
    assert {revision for _file, revision in asked} == {TINY_REVISION}
    assert sorted(path.name for path in target.iterdir()) == ["artifact.json", "model.onnx", "model.onnx.data"]
    assert sorted(path.name for path in target.parent.iterdir()) == sorted(
        [target.name, f".{target.name}.lock"]
    ), "a build leaves no stage behind, only its lock"
    assert re.fullmatch(r"[0-9a-f]{16}", first.profile.artifact_digest or "")
    assert manifest["digest"] == first.profile.artifact_digest
    assert (manifest["model"], manifest["revision"], manifest["quantization"], manifest["file_format"]) == (
        TINY,
        TINY_REVISION,
        embedding_backend.ORT_DYNAMIC_INT8,
        embedding_backend.ONNX_EXTERNAL_DATA,
    )

    second = embedding_backend.load_encoder(TINY)
    texts = ["w1 w2 w3", "w4"]

    assert len(quantized) == 1, "a built artefact is reused, not rebuilt"
    assert second.profile == first.profile
    vectors = second.encode(texts, batch_size=8)
    assert vectors.shape == (2, TINY_WIDTH)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)
    assert np.allclose(vectors, first.encode(texts, batch_size=8))


def test_a_served_int8_vector_is_a_function_of_its_text_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dynamic int8 quantises each activation tensor with one scale, so in a
    shared batch a text's vector moves with its neighbours (up to 0.02 cosine on
    bge-m3). The served model runs one text per session call, so a vector is the
    same whichever batch it was asked for in: what reuse by text assumes."""
    _tiny_served_repo(tmp_path, monkeypatch)
    encoder = embedding_backend.load_encoder(TINY)
    texts = ["w1 w2 w3 w4 w5 w6 w7", "w8", "w9 w10"]

    together = encoder.encode(texts, batch_size=8)
    alone = np.vstack([encoder.encode([text], batch_size=8) for text in texts])

    assert np.array_equal(together, alone)
    assert np.array_equal(encoder.encode(texts, batch_size=8, max_tokens=2), np.vstack(
        [encoder.encode([text], batch_size=8, max_tokens=2) for text in texts]
    ))


def test_the_quantiser_runs_outside_the_server_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The quantiser holds the fp32 graph and its int8 copy at once: 8.7 GB peak
    for bge-m3. In a child process that memory goes back to the host when the
    child exits, and running out of it ends the build, not the server."""
    _tiny_served_repo(tmp_path, monkeypatch)
    for name in [name for name in sys.modules if name.startswith("onnxruntime.quantization")]:
        monkeypatch.delitem(sys.modules, name)

    embedding_backend.load_encoder(TINY)

    assert not [name for name in sys.modules if name.startswith("onnxruntime.quantization")]


def test_a_failed_build_refuses_the_load_and_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _asked, served = _tiny_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(embedding_backend, "_QUANTIZE_CHILD", "raise SystemExit('the quantiser is unavailable')")

    with pytest.raises(embedding_backend.ModelFilesUnavailable, match="the quantiser is unavailable"):
        embedding_backend.load_encoder(TINY)

    target = embedding_backend.artifact_dir(TINY, served)
    assert not [path for path in target.parent.rglob("*") if path.is_file() and path.name != f".{target.name}.lock"]


def test_a_damaged_artifact_is_rebuilt_to_the_same_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _asked, served = _tiny_served_repo(tmp_path, monkeypatch)
    first = embedding_backend.load_encoder(TINY)
    data = embedding_backend.artifact_dir(TINY, served) / "model.onnx.data"
    data.write_bytes(data.read_bytes()[:-7])

    again = embedding_backend.load_encoder(TINY)

    assert again.profile.artifact_digest == first.profile.artifact_digest
    assert again.profile.fingerprint() == first.profile.fingerprint()


def test_a_capped_encode_reads_the_turn_s_head_and_leaves_the_shared_tokenizer_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tiny_served_repo(tmp_path, monkeypatch)
    encoder = embedding_backend.load_encoder(TINY)
    long_turn = " ".join(f"w{i}" for i in range(4, 40))
    head = "w4 w5 w6"  # the turn's first three tokens; <s> and </s> come on top

    capped = encoder.encode([long_turn], batch_size=8, max_tokens=3)

    assert np.allclose(capped, encoder.encode([head], batch_size=8), atol=1e-6)
    assert np.allclose(encoder.encode([head], batch_size=8, max_tokens=3), encoder.encode([head], batch_size=8))
    assert not np.allclose(encoder.encode([long_turn], batch_size=8), capped, atol=1e-3)
    # A capped row next to a shorter one pads like any batch.
    both = encoder.encode([long_turn, "w9"], batch_size=8, max_tokens=3)
    assert np.allclose(both[0], capped[0], atol=1e-6)
    assert np.allclose(both[1], encoder.encode(["w9"], batch_size=8)[0], atol=1e-6)


# --------------------------------------------------------------------------- #
# One model, one resident instance
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# T6c: a host downloads the published artefact before it ever builds one
# --------------------------------------------------------------------------- #


def _publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served):
    """What a maintainer does with the release script: build the artefact once,
    pin its digest, and publish it as a release asset in a directory the host's
    download URL points at. The host then starts with an empty artefact dir."""
    monkeypatch.setenv(embedding_backend.ARTIFACT_DIR_ENV, str(tmp_path / "maintainer"))
    embedding_backend.load_encoder(TINY)
    digest = embedding_backend.artifact_sha256(embedding_backend.artifact_dir(TINY, served))
    assets = tmp_path / "assets"
    asset = embedding_backend.write_artifact_asset(TINY, dataclasses.replace(served, digest=digest), assets)
    pinned = dataclasses.replace(
        served, digest=digest, asset_digest=hashlib.sha256(asset.read_bytes()).hexdigest()
    )
    monkeypatch.setitem(embedding_backend._SERVED, TINY, pinned)
    monkeypatch.setenv(embedding_backend.ARTIFACT_DIR_ENV, str(tmp_path / "host"))
    monkeypatch.setenv(embedding_backend.ARTIFACT_URL_ENV, assets.as_uri())
    return pinned, asset


def _rewrite_asset(asset: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(asset, "w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_a_host_downloads_the_published_artifact_instead_of_building_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build peaks at 8.7 GB for bge-m3 and needs about 2.8 GB of disk; a
    download verified against the pinned digest needs neither."""
    _asked, served = _tiny_served_repo(tmp_path, monkeypatch)
    pinned, asset = _publish(tmp_path, monkeypatch, served)
    monkeypatch.setattr(embedding_backend, "_quantize", lambda *_a: pytest.fail("a verified download is not rebuilt"))

    encoder = embedding_backend.load_encoder(TINY)

    target = embedding_backend.artifact_dir(TINY, pinned)
    assert asset.name == f"tiny-served-int8-{pinned.digest[:8]}.onnx.tar"
    assert encoder.profile.artifact_digest == pinned.digest[:16]
    assert embedding_backend.artifact_sha256(target) == pinned.digest
    assert sorted(path.name for path in target.iterdir()) == ["artifact.json", "model.onnx", "model.onnx.data"]
    assert sorted(path.name for path in target.parent.iterdir()) == sorted(
        [target.name, f".{target.name}.lock"]
    ), "a download leaves no stage behind, only its lock"
    assert encoder.encode(["w1 w2"], batch_size=8).shape == (1, TINY_WIDTH)


@pytest.mark.parametrize("damage", ["other bytes", "an extra member", "a path outside the directory"])
def test_a_download_that_is_not_the_pinned_artifact_is_refused_and_built_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    _asked, served = _tiny_served_repo(tmp_path, monkeypatch)
    pinned, asset = _publish(tmp_path, monkeypatch, served)
    with tarfile.open(asset) as tar:
        members = {member.name: tar.extractfile(member).read() for member in tar}
    if damage == "other bytes":
        data = bytearray(members["model.onnx.data"])
        data[len(data) // 2] ^= 0xFF
        members["model.onnx.data"] = bytes(data)
    elif damage == "an extra member":
        members["run-me.sh"] = b"echo hello"
    else:
        members["../outside.txt"] = b"escaped"
    _rewrite_asset(asset, members)
    built: list[tuple] = []
    real_quantize = embedding_backend._quantize
    monkeypatch.setattr(embedding_backend, "_quantize", lambda *args: built.append(args) or real_quantize(*args))

    encoder = embedding_backend.load_encoder(TINY)

    assert len(built) == 1, "a refused download falls back to the local build"
    assert encoder.profile.artifact_digest == pinned.digest[:16], "the local build is deterministic"
    assert not list(tmp_path.rglob("outside.txt")) and not list(tmp_path.rglob("run-me.sh"))


def _hostile_asset(kind: str, path: Path) -> None:
    """The reviewer's bombs, small on the wire and large unpacked: a GNU sparse
    member that claims 4 GiB, and an xz-compressed regular member. A compressed
    stream is refused whatever it holds, so the xz one holds 64 MiB (the
    reviewer's held 6 GiB, which takes half a minute to write)."""
    if kind == "sparse":
        tar_cli = shutil.which("tar")
        if tar_cli is None:
            pytest.skip("GNU tar writes the sparse member")
        work = path.parent / "work"
        work.mkdir()
        (work / "model.onnx").write_bytes(b"x" * 10)
        with open(work / "model.onnx.data", "wb") as fh:
            fh.seek((4 << 30) - 1)  # a hole: one byte is written
            fh.write(b"\0")
        subprocess.run(
            [tar_cli, "--format=gnu", "-S", "-cf", str(path), "-C", str(work), "model.onnx", "model.onnx.data"],
            check=True,
        )
        shutil.rmtree(work)
        return

    class Zeros(io.RawIOBase):
        def __init__(self, left: int) -> None:
            self.left = left

        def readable(self) -> bool:
            return True

        def readinto(self, buffer) -> int:
            size = min(len(buffer), self.left)
            buffer[:size] = bytes(size)
            self.left -= size
            return size

    with tarfile.open(path, "w:xz", preset=0) as tar:
        graph = tarfile.TarInfo("model.onnx")
        graph.size = 10
        tar.addfile(graph, io.BytesIO(b"x" * 10))
        data = tarfile.TarInfo("model.onnx.data")
        data.size = 64 << 20
        tar.addfile(data, io.BufferedReader(Zeros(data.size), 8 << 20))


@pytest.mark.parametrize("kind", ["sparse", "xz"])
@pytest.mark.parametrize("pinned_to_it", [False, True], ids=["other-asset", "pin-matches"])
def test_a_hostile_asset_is_refused_before_a_byte_is_unpacked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, pinned_to_it: bool
) -> None:
    """The download is checked against the packed asset's own pinned sha256
    before `tarfile` ever opens it. Should a pin ever name a hostile asset, the
    member check still refuses a sparse member, a compressed stream, or a size
    beyond the cap, before anything is written."""
    served = dataclasses.replace(
        embedding_backend.served_artifact(M3), revision="f" * 40, asset_digest="0" * 64
    )
    assets = tmp_path / "assets"
    assets.mkdir()
    asset = assets / embedding_backend.artifact_asset_name(M3, served)
    _hostile_asset(kind, asset)
    if pinned_to_it:
        served = dataclasses.replace(served, asset_digest=hashlib.sha256(asset.read_bytes()).hexdigest())
    monkeypatch.setenv(embedding_backend.ARTIFACT_DIR_ENV, str(tmp_path / "host"))
    monkeypatch.setenv(embedding_backend.ARTIFACT_URL_ENV, assets.as_uri())
    opened: list[str] = []
    real_open = tarfile.open
    monkeypatch.setattr(
        embedding_backend.tarfile, "open", lambda *a, **k: opened.append(str(a[1:] or k)) or real_open(*a, **k)
    )
    written: list[int] = []
    monkeypatch.setattr(
        embedding_backend.shutil, "copyfileobj", lambda *_a, **_k: written.append(1) or pytest.fail("unpacked")
    )
    target = embedding_backend.artifact_dir(M3, served)

    assert embedding_backend._fetch_artifact(M3, served, target) is False

    assert written == []
    assert opened == (["('r:',)"] if pinned_to_it else []), opened
    assert not target.exists()
    assert [path.name for path in target.parent.iterdir()] == [], "no stage is left behind"


def test_the_member_sizes_are_capped_before_anything_is_unpacked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = dataclasses.replace(embedding_backend.served_artifact(M3), revision="e" * 40)
    assets = tmp_path / "assets"
    assets.mkdir()
    asset = assets / embedding_backend.artifact_asset_name(M3, served)
    _rewrite_asset(asset, {"model.onnx": b"g" * 600, "model.onnx.data": b"d" * 600})
    served = dataclasses.replace(served, asset_digest=hashlib.sha256(asset.read_bytes()).hexdigest())
    monkeypatch.setenv(embedding_backend.ARTIFACT_DIR_ENV, str(tmp_path / "host"))
    monkeypatch.setenv(embedding_backend.ARTIFACT_URL_ENV, assets.as_uri())
    monkeypatch.setattr(embedding_backend, "_MAX_UNPACKED_BYTES", 1000)
    monkeypatch.setattr(
        embedding_backend.shutil, "copyfileobj", lambda *_a, **_k: pytest.fail("unpacked past the cap")
    )

    assert embedding_backend._fetch_artifact(M3, served, embedding_backend.artifact_dir(M3, served)) is False


def test_the_bge_m3_release_asset_is_pinned_by_its_own_sha256() -> None:
    """Reproduced by the build script and by an independent reviewer."""
    served = embedding_backend.served_artifact(M3)
    assert served.asset_digest == "71e5f90fa019c2de0471b158e408da6ca7f43c93188308095242dfe46525ef76"


def test_an_unavailable_download_falls_back_to_the_local_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _asked, served = _tiny_served_repo(tmp_path, monkeypatch)
    monkeypatch.setitem(embedding_backend._SERVED, TINY, dataclasses.replace(served, digest="0" * 64))
    built: list[tuple] = []
    real_quantize = embedding_backend._quantize
    monkeypatch.setattr(embedding_backend, "_quantize", lambda *args: built.append(args) or real_quantize(*args))

    encoder = embedding_backend.load_encoder(TINY)

    assert len(built) == 1
    assert re.fullmatch(r"[0-9a-f]{16}", encoder.profile.artifact_digest or "")


def test_the_published_bge_m3_artifact_is_a_github_release_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    served = embedding_backend.served_artifact(M3)
    monkeypatch.delenv(embedding_backend.ARTIFACT_URL_ENV, raising=False)

    assert served is not None and re.fullmatch(r"[0-9a-f]{64}", served.digest or "")
    assert served.digest.startswith("7b9a0b3b0b292643"), "the digest T6b's fingerprint names"
    assert embedding_backend.artifact_url(M3, served) == (
        "https://github.com/Artexis10/exomem/releases/download/served-models/bge-m3-int8-7b9a0b3b.onnx.tar"
    )
    for off in ("", "off", "0", "none"):
        monkeypatch.setenv(embedding_backend.ARTIFACT_URL_ENV, off)
        assert embedding_backend.artifact_url(M3, served) is None, off
    monkeypatch.delenv(embedding_backend.ARTIFACT_URL_ENV)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    assert embedding_backend.artifact_url(M3, served) is None, "an offline host never downloads"
    assert embedding_backend.artifact_url(M3, dataclasses.replace(served, digest=None)) is None
    assert embedding_backend.artifact_url(M3, dataclasses.replace(served, asset_digest=None)) is None


def test_a_failed_acquisition_is_not_retried_on_every_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A host that cannot download and cannot build would otherwise start the
    8.7 GB build again on every write that asks for the model."""
    _tiny_served_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(embedding_backend, "_QUANTIZE_CHILD", "raise SystemExit('no memory for the quantiser')")
    attempts: list[int] = []
    real_build = embedding_backend._build_artifact
    monkeypatch.setattr(embedding_backend, "_build_artifact", lambda *a: attempts.append(1) or real_build(*a))
    clock = [1000.0]
    monkeypatch.setattr(embedding_backend.time, "monotonic", lambda: clock[0])

    with pytest.raises(embedding_backend.ModelFilesUnavailable, match="no memory for the quantiser"):
        embedding_backend.load_encoder(TINY)
    with pytest.raises(embedding_backend.ModelFilesUnavailable, match="until the process restarts"):
        embedding_backend.load_encoder(TINY)
    assert attempts == [1]

    clock[0] += embedding_backend.ACQUIRE_RETRY_SECONDS + 1
    with pytest.raises(embedding_backend.ModelFilesUnavailable, match="no memory for the quantiser"):
        embedding_backend.load_encoder(TINY)
    assert attempts == [1, 1]


# --------------------------------------------------------------------------- #
# R8: acquisition is safe to run twice and fails well on a small host
# --------------------------------------------------------------------------- #


def _fake_m3_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """bge-m3's served entry with its export stood in for by small files, and
    nothing published (the reviewer's probes p2 and p13). Returns the export
    files asked for, so a test can see whether the 2.2 GB download started."""
    src = tmp_path / "src"
    src.mkdir()
    for name in embedding_backend.served_artifact(M3).source:
        (src / Path(name).name).write_bytes(b"source-" + name.encode() * 1000)
    resolved: list[str] = []
    monkeypatch.setattr(
        embedding_backend,
        "_resolve",
        lambda _model, name, revision=None: resolved.append(name) or str(src / Path(name).name),
    )
    monkeypatch.setenv(embedding_backend.ARTIFACT_URL_ENV, "off")
    return resolved


def _fake_quantize(sleep: float = 0.0, live: dict[str, int] | None = None):
    counting = threading.Lock()

    def quantize(_served, _source: str, out: str) -> None:
        if live is not None:
            with counting:
                live["now"] += 1
                live["calls"] += 1
                live["max"] = max(live["max"], live["now"])
        time.sleep(sleep)
        Path(out).write_bytes(b"graph")
        Path(out + ".data").write_bytes(b"weights")
        if live is not None:
            with counting:
                live["now"] -= 1

    return quantize


def test_two_loads_at_once_acquire_the_artifact_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe p2: two loads race on a host with nothing published. The second
    waits on the lock beside the artefact, then finds the first one's, instead
    of running a second 8.7 GB build beside it."""
    _fake_m3_sources(tmp_path, monkeypatch)
    live = {"now": 0, "max": 0, "calls": 0}
    monkeypatch.setattr(embedding_backend, "_quantize", _fake_quantize(sleep=1.0, live=live))
    served = embedding_backend.served_artifact(M3)
    results: list[tuple[str, str]] = []
    threads = [
        threading.Thread(target=lambda: results.append(embedding_backend.ensure_artifact(M3, served)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert (live["calls"], live["max"]) == (1, 1)
    assert len(results) == 2 and results[0] == results[1]


_KILLED_BUILD_CHILD = """
import sys, time
from pathlib import Path
from exomem import embedding_backend as eb

src = Path(sys.argv[1])
eb._resolve = lambda _model, name, revision=None: str(src / Path(name).name)
eb._available_memory = lambda: 64 << 30

def quantize(_served, _source, _out):
    print("building", flush=True)
    time.sleep(300)

eb._quantize = quantize
eb.ensure_artifact("BAAI/bge-m3", eb.served_artifact("BAAI/bge-m3"))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX")
def test_a_killed_acquisition_holds_no_lock_and_its_stage_is_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe p2, killed: a build killed mid-way, as the OOM killer kills it,
    leaves its stage with the copied export in it. The next acquisition finds
    the lock free, since the kernel dropped it with its dead holder, and sweeps
    the stage before it builds."""
    _fake_m3_sources(tmp_path, monkeypatch)
    served = embedding_backend.served_artifact(M3)
    target = embedding_backend.artifact_dir(M3, served)
    child = subprocess.Popen(
        [sys.executable, "-c", _KILLED_BUILD_CHILD, str(tmp_path / "src")], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "building"
    finally:
        child.kill()
        child.wait(timeout=60)
    assert list(target.parent.glob(f".{target.name}-build-*")), "the killed build left its stage"

    monkeypatch.setattr(embedding_backend, "_quantize", _fake_quantize())
    path, _digest = embedding_backend.ensure_artifact(M3, served)

    assert Path(path).is_file()
    assert sorted(p.name for p in target.parent.iterdir()) == sorted([target.name, f".{target.name}.lock"])


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX")
def test_a_build_the_kernel_kills_reads_as_out_of_memory_and_waits_a_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe p13: the quantiser child is killed as the OOM killer kills it. The
    failure names the likely cause, not a missing package, and the next attempt
    in this process waits a day, never 15 minutes."""
    _fake_m3_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(embedding_backend, "_QUANTIZE_CHILD", "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    spawns: list[int] = []
    real_run = subprocess.run
    monkeypatch.setattr(embedding_backend.subprocess, "run", lambda *a, **k: spawns.append(1) or real_run(*a, **k))
    clock = [1000.0]
    monkeypatch.setattr(embedding_backend.time, "monotonic", lambda: clock[0])
    served = embedding_backend.served_artifact(M3)

    with pytest.raises(embedding_backend.ModelFilesUnavailable, match=r"killed by signal 9 \(likely out of memory"):
        embedding_backend.ensure_artifact(M3, served)
    for advance in (60, 900, 23 * 3600):
        clock[0] += advance
        with pytest.raises(embedding_backend.ModelFilesUnavailable, match="until the process restarts or 24 h pass"):
            embedding_backend.ensure_artifact(M3, served)
    assert spawns == [1]

    clock[0] += 3600
    with pytest.raises(embedding_backend.ModelFilesUnavailable, match="likely out of memory"):
        embedding_backend.ensure_artifact(M3, served)
    assert spawns == [1, 1]


def test_a_host_short_of_memory_starts_no_build_and_is_told_where_the_artifact_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below 10 GiB available nothing is downloaded, copied or spawned: the
    refusal names the published artefact and the variable that fetches it."""
    resolved = _fake_m3_sources(tmp_path, monkeypatch)
    built: list[int] = []
    monkeypatch.setattr(embedding_backend, "_quantize", lambda *_a: built.append(1))
    monkeypatch.setattr(embedding_backend, "_available_memory", lambda: 6 << 30)
    served = embedding_backend.served_artifact(M3)

    with pytest.raises(embedding_backend.ModelFilesUnavailable) as refused:
        embedding_backend.ensure_artifact(M3, served)

    message = str(refused.value)
    assert embedding_backend.BUILD_MIN_AVAILABLE_BYTES == 10 << 30
    assert (built, resolved) == ([], []), "no build started and no export fetched"
    assert "6.0 GiB" in message and "10 GiB" in message
    assert "bge-m3-int8-7b9a0b3b.onnx.tar" in message and "EXOMEM_MODEL_ARTIFACT_URL" in message
    with pytest.raises(embedding_backend.ModelFilesUnavailable, match="until the process restarts"):
        embedding_backend.ensure_artifact(M3, served)


@pytest.mark.parametrize("available", [None, 12 << 30], ids=["unmeasured", "enough"])
def test_a_host_with_memory_or_no_reading_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available: int | None
) -> None:
    _fake_m3_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(embedding_backend, "_quantize", _fake_quantize())
    monkeypatch.setattr(embedding_backend, "_available_memory", lambda: available)

    path, _digest = embedding_backend.ensure_artifact(M3, embedding_backend.served_artifact(M3))

    assert Path(path).is_file()


@pytest.mark.parametrize("error_name", ["LocalEntryNotFoundError", "RevisionNotFoundError", "OSError"])
def test_a_hub_failure_is_remembered_like_a_failed_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_name: str
) -> None:
    """Offline with the export uncached, or a revision the hub no longer has:
    the load is refused as unavailable and not retried on the next write."""
    hub_errors = pytest.importorskip("huggingface_hub.errors")
    _fake_m3_sources(tmp_path, monkeypatch)
    asked: list[str] = []

    def missing(_model: str, name: str, revision: str | None = None) -> str:
        asked.append(name)
        if error_name == "OSError":
            raise OSError("connection refused")
        if error_name == "RevisionNotFoundError":
            import httpx

            not_found = httpx.Response(404, request=httpx.Request("GET", "https://hub.invalid/revision"))
            raise hub_errors.RevisionNotFoundError("no such revision", response=not_found)
        raise hub_errors.LocalEntryNotFoundError("offline and not cached")

    monkeypatch.setattr(embedding_backend, "_resolve", missing)
    served = embedding_backend.served_artifact(M3)

    with pytest.raises(embedding_backend.ModelFilesUnavailable, match="neither in the model cache nor fetchable"):
        embedding_backend.ensure_artifact(M3, served)
    with pytest.raises(embedding_backend.ModelFilesUnavailable, match="until the process restarts"):
        embedding_backend.ensure_artifact(M3, served)
    assert len(asked) == 1


def test_available_memory_reads_memavailable_or_else_the_free_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _REAL_AVAILABLE_MEMORY is not None
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16384000 kB\nMemFree:         1024000 kB\nMemAvailable:    7340032 kB\n", encoding="ascii"
    )
    monkeypatch.setattr(embedding_backend, "_MEMINFO", meminfo)
    assert _REAL_AVAILABLE_MEMORY() == 7 << 30

    monkeypatch.setattr(embedding_backend, "_MEMINFO", tmp_path / "absent")
    pages = {"SC_AVPHYS_PAGES": 1000, "SC_PAGE_SIZE": 4096}
    monkeypatch.setattr(embedding_backend.os, "sysconf", lambda name: pages[name], raising=False)
    assert _REAL_AVAILABLE_MEMORY() == 4096 * 1000

    def unknown(name: str) -> int:
        raise ValueError(name)

    monkeypatch.setattr(embedding_backend.os, "sysconf", unknown, raising=False)
    assert _REAL_AVAILABLE_MEMORY() is None


@pytest.mark.parametrize("lane", ["recall", "activation"])
def test_a_served_model_is_acquired_outside_the_model_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    """A first load that downloads or builds holds no model slot meanwhile: every
    other encode on that slot runs, and the slot is taken only for the load."""
    _tiny_served_repo(tmp_path, monkeypatch)
    if lane == "recall":
        monkeypatch.setattr(embeddings, "MODEL_NAME", TINY)
        load, slot = embeddings.get_model, runtime_resources.model_execution
    else:
        monkeypatch.setenv(embeddings.ACTIVATION_MODEL_ENV, TINY)
        load, slot = embeddings.get_activation_model, embeddings.activation_execution
    free_while_building: list[bool] = []
    real_build = embedding_backend._build_artifact

    def build_and_probe(*args) -> None:
        def probe() -> None:
            try:
                with slot(wait=False):
                    free_while_building.append(True)
            except runtime_resources.ModelBusyError:
                free_while_building.append(False)

        other = threading.Thread(target=probe)
        other.start()
        other.join(timeout=30)
        real_build(*args)

    monkeypatch.setattr(embedding_backend, "_build_artifact", build_and_probe)

    model = load()

    assert free_while_building == [True]
    assert model.backend == embedding_backend.ONNX


def test_a_served_model_without_its_tokenizer_acquires_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _asked, served = _tiny_served_repo(tmp_path, monkeypatch)
    real_resolve = embedding_backend._resolve

    def no_tokenizer(model: str, name: str, revision: str | None = None) -> str:
        if name == "tokenizer.json":
            raise FileNotFoundError(name)
        return real_resolve(model, name, revision)

    monkeypatch.setattr(embedding_backend, "_resolve", no_tokenizer)
    monkeypatch.setattr(embedding_backend, "ensure_artifact", lambda *_a: pytest.fail("nothing is acquired"))

    with pytest.raises(embedding_backend.ModelFilesUnavailable, match="tokenizer.json"):
        embedding_backend.ensure_served_artifact(TINY)


def test_the_release_asset_is_byte_for_byte_reproducible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _asked, served = _tiny_served_repo(tmp_path, monkeypatch)
    embedding_backend.load_encoder(TINY)

    first = embedding_backend.write_artifact_asset(TINY, served, tmp_path / "first")
    second = embedding_backend.write_artifact_asset(TINY, served, tmp_path / "second")

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as tar:
        members = [(m.name, m.isreg(), m.mtime, m.uid, m.gid, m.mode) for m in tar]
    assert members == [
        ("model.onnx", True, 0, 0, 0, 0o644),
        ("model.onnx.data", True, 0, 0, 0, 0o644),
    ]
    digest = embedding_backend.artifact_sha256(embedding_backend.artifact_dir(TINY, served))
    assert first.name == f"tiny-served-int8-{digest[:8]}.onnx.tar"


def _release_script():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "build_served_model_asset.py"
    spec = importlib.util.spec_from_file_location("build_served_model_asset", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_release_script_publishes_only_the_pinned_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _asked, served = _tiny_served_repo(tmp_path, monkeypatch)
    script = _release_script()

    assert script.main(["--model", TINY, "--out", str(tmp_path / "unpinned")]) == 0
    printed = capsys.readouterr().out
    digest = re.search(r"artefact sha256: ([0-9a-f]{64})", printed).group(1)
    monkeypatch.setitem(embedding_backend._SERVED, TINY, dataclasses.replace(served, digest=digest))
    assert script.main(["--model", TINY, "--out", str(tmp_path / "pinned")]) == 0
    assert sorted(path.name for path in (tmp_path / "pinned").iterdir()) == [f"tiny-served-int8-{digest[:8]}.onnx.tar"]
    assert "served-models/tiny-served-int8-" in capsys.readouterr().out

    monkeypatch.setitem(embedding_backend._SERVED, TINY, dataclasses.replace(served, digest="f" * 64))
    assert script.main(["--model", TINY, "--out", str(tmp_path / "wrong")]) == 1
    assert not [path for path in (tmp_path / "wrong").iterdir() if path.suffix == ".tar"]


def test_the_release_script_never_repackages_an_artifact_already_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reviewer packaged a fetched artefact in 3.2 s without a build: the
    script reused whatever `ensure_artifact` found. It builds its own, always."""
    _asked, served = _tiny_served_repo(tmp_path, monkeypatch)
    host = embedding_backend.load_encoder(TINY)  # an artefact already on this host
    before = embedding_backend.artifact_sha256(embedding_backend.artifact_dir(TINY, served))
    built: list[tuple] = []
    real_quantize = embedding_backend._quantize
    monkeypatch.setattr(embedding_backend, "_quantize", lambda *args: built.append(args) or real_quantize(*args))

    assert _release_script().main(["--model", TINY, "--out", str(tmp_path / "release")]) == 0

    assert len(built) == 1, "the script built the artefact it publishes"
    assert all(str(tmp_path / "release") in out for _served, _source, out in built)
    assert f"artefact sha256: {before}" in capsys.readouterr().out, "the build is deterministic"
    assert [path.suffix for path in (tmp_path / "release").iterdir()] == [".tar"], "the build dir is removed"
    assert host.profile.artifact_digest == before[:16]


def test_the_served_encoder_runs_two_threads_unless_the_budget_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tiny_served_repo(tmp_path, monkeypatch)
    seen: list[dict] = []
    real = runtime_resources.configure_onnx_session_options
    monkeypatch.setattr(
        runtime_resources,
        "configure_onnx_session_options",
        lambda options, **kw: seen.append(kw) or real(options, **kw),
    )

    embedding_backend.load_encoder(TINY)

    assert embedding_backend.SERVED_DEFAULT_THREADS == 2
    assert seen == [{"default_threads": embedding_backend.SERVED_DEFAULT_THREADS}]


@pytest.mark.parametrize(
    "mode_name, recall_model, loads, ensured",
    [
        ("normal", M3, ["recall"], []),
        ("quiet", M3, [], [M3]),
        ("normal", "BAAI/bge-base-en-v1.5", [], []),
    ],
)
def test_warm_up_readies_a_served_recall_model_off_the_request_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode_name: str,
    recall_model: str,
    loads: list[str],
    ensured: list[str],
) -> None:
    """A served model's first load may download or build it; that belongs to the
    warm-up, never to whichever request comes first. Normal mode preloads the
    one shared instance; quiet mode, which loads no model at boot, still fetches
    the artefact; a hub-file model keeps its lazy load."""
    monkeypatch.setattr(embeddings, "MODEL_NAME", recall_model)
    monkeypatch.setenv("EXOMEM_MODE", mode_name)
    monkeypatch.delenv("EXOMEM_PRELOAD_MODELS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    readiness.reset()
    calls: list[str] = []
    ensured_calls: list[str] = []
    monkeypatch.setattr(warmup, "warm_retrieval_catalog", lambda _root: True, raising=False)
    monkeypatch.setattr(warmup, "warm_caches", lambda _root, **_kw: {})
    monkeypatch.setattr("exomem.semantic_contract.build_corpus_context", lambda _root: None)
    monkeypatch.setattr(
        embeddings, "get_model", lambda: calls.append("recall") or types.SimpleNamespace(encode=lambda _t: None)
    )
    monkeypatch.setattr(embeddings, "get_reranker", lambda: pytest.fail("the reranker stays lazy"))
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: pytest.fail("CLIP stays lazy"))
    monkeypatch.setattr(embedding_backend, "ensure_served_artifact", lambda name: ensured_calls.append(name))

    try:
        warmup.warm_all(tmp_path)
        ready = readiness.is_ready("embeddings")
    finally:
        readiness.reset()

    assert (calls, ensured_calls, ready) == (loads, ensured, True)


def test_equal_served_profiles_share_one_resident_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activation and recall on one model hold ONE instance: one load, one memory cost."""
    resident = object()
    monkeypatch.setattr(embeddings, "MODEL_NAME", M3)
    monkeypatch.setenv(embeddings.ACTIVATION_MODEL_ENV, M3)
    monkeypatch.setattr(embeddings, "_MODEL", resident)
    monkeypatch.setattr(embedding_backend, "load_encoder", lambda *_a, **_k: pytest.fail("one instance only"))

    assert embeddings.activation_encoder_is_shared() is True
    assert embeddings.get_activation_model() is resident
    assert embeddings._ACTIVATION_MODEL is None
