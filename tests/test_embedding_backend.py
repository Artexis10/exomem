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

from exomem import embedding_backend, privacy_log

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
    # BERT deletes these control characters and joins the words either side.
    "field\x1crecord\x1dgroup\x1eunit\x1fnext\x85line",
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


@pytest.mark.parametrize("specials", [False, True])
def test_onnx_fit_guard_rejects_truncated_text_without_changing_inference(specials: bool) -> None:
    """Oversized advisory text must not reuse a vector of its truncated prefix."""
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from tokenizers.processors import TemplateProcessing

    tokenizer = Tokenizer(WordLevel(
        vocab={"[UNK]": 0, "a": 1, "[PAD]": 2, "[CLS]": 3, "[SEP]": 4},
        unk_token="[UNK]",
    ))
    tokenizer.pre_tokenizer = Whitespace()
    if specials:
        tokenizer.post_processor = TemplateProcessing(
            single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 3), ("[SEP]", 4)],
        )
    tokenizer.enable_truncation(max_length=8)
    tokenizer.enable_padding(pad_id=2, pad_token="[PAD]")
    encoder = object.__new__(embedding_backend._OnnxEncoder)
    encoder._tokenizer = tokenizer
    encoder.profile = embedding_backend.EncoderProfile(
        model="model", pooling="mean", query_prefix="", passage_prefix="",
        max_seq=8, pad_token="[PAD]", collapse_whitespace=True,
    )
    boundary = " ".join(["a"] * (6 if specials else 8))
    oversized = boundary + " a"
    assert encoder.texts_fit([])
    assert encoder.texts_fit(["", "a\x1ca", boundary])
    assert not encoder.texts_fit(["a", oversized])
    # The guard must leave concurrent inference's padding and cap in place.
    rows = tokenizer.encode_batch(["a", oversized])
    assert [len(row.ids) for row in rows] == [8, 8]
    assert sum(rows[0].attention_mask) == (3 if specials else 1)


def _fake_onnx_encoder(
    monkeypatch: pytest.MonkeyPatch, *, served: bool, device: str = "cpu"
) -> tuple[embedding_backend._OnnxEncoder, object, dict[str, str]]:
    class Options:
        def __init__(self) -> None:
            self.entries: dict[str, str] = {}

        def add_session_config_entry(self, key: str, value: str) -> None:
            self.entries[key] = value

    options = Options()
    entries_at_session_creation: dict[str, str] = {}
    session = types.SimpleNamespace(get_inputs=lambda: [])

    def inference_session(*_args: object, **kwargs: object) -> object:
        session_options = kwargs["sess_options"]
        entries_at_session_creation.update(session_options.entries)
        return session

    ort = types.SimpleNamespace(
        SessionOptions=lambda: options,
        GraphOptimizationLevel=types.SimpleNamespace(ORT_ENABLE_ALL="all"),
        InferenceSession=inference_session,
        get_available_providers=lambda: ["CPUExecutionProvider"],
    )
    tokenizer = types.SimpleNamespace(
        enable_truncation=lambda **_kwargs: None,
        enable_padding=lambda **_kwargs: None,
        token_to_id=lambda _token: 0,
    )
    profile = embedding_backend.EncoderProfile(
        model="model",
        pooling="mean",
        query_prefix="",
        passage_prefix="",
        max_seq=512,
        pad_token="<pad>",
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(
        sys.modules,
        "tokenizers",
        types.SimpleNamespace(Tokenizer=types.SimpleNamespace(from_file=lambda _path: tokenizer)),
    )
    monkeypatch.setattr(embedding_backend, "require_tokenizer", lambda _name: "tokenizer.json")
    monkeypatch.setattr(embedding_backend, "read_profile", lambda _name: profile)
    monkeypatch.setattr(
        embedding_backend,
        "served_artifact",
        lambda _name: types.SimpleNamespace() if served else None,
    )
    monkeypatch.setattr(embedding_backend, "ensure_artifact", lambda *_args: ("served.onnx", "abc"))
    monkeypatch.setattr(embedding_backend, "_model_file", lambda *_args: "hub.onnx")

    encoder = embedding_backend._OnnxEncoder("model", device)
    return encoder, options, entries_at_session_creation


def test_cloud_weight_sharing_is_applied_to_the_served_onnx_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.delenv("EXOMEM_HOSTED_CELL", raising=False)
    monkeypatch.delenv("EXOMEM_ONNX_SHARE_WEIGHTS", raising=False)
    privacy_log.install_hosted_log_redaction()

    with caplog.at_level("INFO", logger=embedding_backend.__name__):
        encoder, options, entries_at_session_creation = _fake_onnx_encoder(
            monkeypatch, served=True
        )

    assert encoder.share_weights is True
    assert options.entries == {"session.disable_prepacking": "1"}
    assert entries_at_session_creation == {"session.disable_prepacking": "1"}
    runtime_records = [
        record for record in caplog.records
        if getattr(record, "event", None) == "onnx_runtime_shape"
    ]
    assert len(runtime_records) == 1
    assert runtime_records[0].fields == {
        "device": "cpu",
        "intra_op_threads": options.intra_op_num_threads,
        "inter_op_threads": options.inter_op_num_threads,
        "share_weights": True,
    }
    assert runtime_records[0].content == {}
    assert "served.onnx" not in caplog.text
    assert "tokenizer.json" not in caplog.text


@pytest.mark.parametrize(
    ("device", "expected"),
    [("CPU", "cpu"), ("cuda:1", "cuda"), ("mps", "mps"),
     ("private-device-path-sentinel", "other"),
     ("cuda:private-device-path-sentinel", "cuda")],
)
def test_runtime_shape_fields_exclude_caller_device_content(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    device: str, expected: str,
) -> None:
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "1")
    privacy_log.install_hosted_log_redaction()
    with caplog.at_level("INFO", logger=embedding_backend.__name__):
        encoder, options, _ = _fake_onnx_encoder(monkeypatch, served=False, device=device)
    record = next(r for r in caplog.records if getattr(r, "event", None) == "onnx_runtime_shape")
    assert encoder.share_weights is False
    assert record.fields == {
        "device": expected,
        "intra_op_threads": options.intra_op_num_threads,
        "inter_op_threads": options.inter_op_num_threads,
        "share_weights": False,
    }
    assert record.content == {}
    assert "private-device-path-sentinel" not in str(record.__dict__)
    assert "hub.onnx" not in str(record.__dict__)


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"EXOMEM_HOSTED_CELL": "1"},
        {"EXOMEM_CLOUD_CELL": "1", "EXOMEM_ONNX_SHARE_WEIGHTS": "0"},
    ],
    ids=("personal", "hosted", "cloud-explicitly-disabled"),
)
def test_served_onnx_keeps_prepacking_when_weight_sharing_is_off(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]
) -> None:
    for name in (
        "EXOMEM_CLOUD_CELL",
        "EXOMEM_HOSTED_CELL",
        "EXOMEM_ONNX_SHARE_WEIGHTS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    encoder, options, entries_at_session_creation = _fake_onnx_encoder(
        monkeypatch, served=True
    )

    assert encoder.share_weights is False
    assert options.entries == {}
    assert entries_at_session_creation == {}


def test_cloud_weight_sharing_is_not_applied_to_a_hub_onnx_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.delenv("EXOMEM_ONNX_SHARE_WEIGHTS", raising=False)

    encoder, options, entries_at_session_creation = _fake_onnx_encoder(
        monkeypatch, served=False
    )

    assert encoder.share_weights is False
    assert options.entries == {}
    assert entries_at_session_creation == {}


@pytest.mark.skipif(not RUN_EQUIVALENCE, reason="set RUN_EMBED_EQUIVALENCE_TEST=1")
@pytest.mark.parametrize(
    "model_name",
    # Recall's CLS-pooled model, and the mean-pooled XLM-R activation encoder
    # whose padding token is `<pad>` (id 1), not BERT's `[PAD]` (id 0).
    ["BAAI/bge-base-en-v1.5", "intfloat/multilingual-e5-small"],
)
def test_onnx_and_torch_produce_interchangeable_vectors(model_name: str) -> None:
    """Same model, two runtimes: vectors must be substitutable without re-indexing.

    Asserts the property that actually matters — that an existing vault's vectors
    stay comparable to newly encoded ones — rather than bitwise equality, which
    no two runtimes give.
    """
    pytest.importorskip("sentence_transformers")
    pytest.importorskip("onnxruntime")

    torch_encoder = embedding_backend.load_encoder(model_name, backend=embedding_backend.TORCH)
    onnx_encoder = embedding_backend.load_encoder(model_name, backend=embedding_backend.ONNX)

    left = torch_encoder.encode(EQUIVALENCE_TEXTS, batch_size=8)
    right = onnx_encoder.encode(EQUIVALENCE_TEXTS, batch_size=8)

    assert left.shape == right.shape
    assert torch_encoder.profile.fingerprint() == onnx_encoder.profile.fingerprint()
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
