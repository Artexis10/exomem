"""The activation encoder: its profile, fingerprint and own execution slot (step 4, T6).

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
import json
import sys
import threading
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


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv(embeddings.ACTIVATION_MODEL_ENV, raising=False)
    monkeypatch.setattr(embeddings, "_MODEL", None)
    monkeypatch.setattr(embeddings, "_ACTIVATION_MODEL", None)
    yield


def _repo(tmp_path: Path, files: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve `files` as a model repository through `embedding_backend._resolve`."""
    root = tmp_path / "repo"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content) if not isinstance(content, str) else content, encoding="utf-8")
    asked: list[str] = []

    def resolve(_model: str, filename: str) -> str:
        asked.append(filename)
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
    seen: list[str] = []
    monkeypatch.setattr(embeddings, "embed_query_if_loaded", lambda text: seen.append(text) or np.zeros(3, dtype=np.float32))

    assert embeddings.embed_activation_query_if_loaded("continue") is not None
    assert seen == ["continue"]


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
