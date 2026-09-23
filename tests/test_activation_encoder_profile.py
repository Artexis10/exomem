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
import json
import re
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
M3 = "BAAI/bge-m3"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv(embeddings.ACTIVATION_MODEL_ENV, raising=False)
    monkeypatch.setattr(embeddings, "_MODEL", None)
    monkeypatch.setattr(embeddings, "_ACTIVATION_MODEL", None)
    monkeypatch.setenv(embedding_backend.ARTIFACT_DIR_ENV, str(tmp_path / "artifacts"))
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
    assert [path.name for path in target.parent.iterdir()] == [target.name], "a build leaves no stage behind"
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
    assert not [path for path in target.parent.rglob("*") if path.is_file()]


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
