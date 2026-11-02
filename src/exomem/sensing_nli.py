"""The NLI instrument on the admitted pin, for `pair.relation` and `recap.covered_by`.

The same repository pin, resident-snapshot digest and local-only loader that
admit the stance verifier (`claims.VERIFIER_PINS`), recorded as an instrument:

* The templates `nli-pair-v1` and `nli-coverage-v1` feed two texts as a
  classification pair in both orders. There is no prompt and no generation, so
  vault text never reaches instruction position. Both templates share one
  loaded model; each is admitted on its own fixture set, so a coverage fixture
  miss refuses coverage only.
* The output is one probability vector per direction over the head's declared
  `(entailment, neutral, contradiction)` columns. The verdict is derived from
  the rounded vectors by the question's versioned label map (`sensing`).
* An input the model cannot see whole abstains (`input_too_long`) instead of
  being truncated.
* It runs on CPU with one thread (owner ruling R5) and only inside the sensor
  worker child. `identity()` is the one function the service process calls,
  and it imports nothing heavier than package metadata.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path
from typing import Any

from . import claims, sensing

log = logging.getLogger(__name__)

RUNTIME = "sentence-transformers-cpu"
_RUNTIME_PACKAGES = ("sentence-transformers", "transformers", "torch")
_COLUMNS = ("entailment", "neutral", "contradiction")


class Refused(Exception):
    """The instrument cannot run. `reason` is a closed code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


REFUSAL_REASONS = frozenset(
    {
        "no-pin",
        "label-map-unknown",
        "weights-missing",
        "digest-mismatch",
        "dependency-missing",
        "fixtures-failed",
    }
)


def runtime_version() -> str | None:
    """The installed runtime stack, from package metadata alone. None when absent."""
    try:
        return ";".join(f"{name}={metadata.version(name)}" for name in _RUNTIME_PACKAGES)
    except metadata.PackageNotFoundError:
        return None


def identity(question_type: str = sensing.PAIR_RELATION) -> sensing.InstrumentIdentity | None:
    """The pinned instrument's identity for a question, or None when the runtime is not installed.

    Loads nothing: the pin is repository data and the runtime version is
    package metadata. The two questions share the model and differ in template.
    """
    pin = claims._active_pin()
    version = runtime_version()
    if pin is None or version is None:
        return None
    entry = sensing.question(question_type)
    return sensing.InstrumentIdentity(
        model=pin.model_name,
        revision=pin.model_revision,
        weights_sha256=pin.weights_sha256,
        runtime=RUNTIME,
        runtime_version=version,
        template_version=entry.template_version,
        label_map_version=entry.label_map_version,
        fixture_set=entry.fixture_set,
        placement="local-cpu",
    )


def _softmax_rows(logits: Any) -> list[list[float]] | None:
    import numpy as np

    arr = np.asarray(logits, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] != len(_COLUMNS) or not np.isfinite(arr).all():
        return None
    shifted = arr - arr.max(axis=1, keepdims=True)
    exp = np.exp(shifted)
    return (exp / exp.sum(axis=1, keepdims=True)).tolist()


class NliInstrument:
    """An admitted pair.relation instrument around a local CrossEncoder."""

    def __init__(self, identity_: sensing.InstrumentIdentity, model: Any) -> None:
        self.identity = identity_
        self._model = model
        tokenizer = getattr(model, "tokenizer", None)
        self._tokenizer = tokenizer
        self._max_length = int(
            getattr(model, "max_length", None)
            or getattr(tokenizer, "model_max_length", 512)
            or 512
        )

    def _fits(self, first: str, second: str) -> bool:
        if self._tokenizer is None:
            return True
        encoded = self._tokenizer(first, second, truncation=False)
        return len(encoded["input_ids"]) <= self._max_length

    def judge(
        self, pairs: Sequence[tuple[str, str]]
    ) -> list[tuple[list[float] | None, list[float] | None, str | None]]:
        """`(ab, ba, abstain_reason)` per pair. Raises on non-finite output."""
        out: list[tuple[list[float] | None, list[float] | None, str | None]] = []
        runnable: list[int] = []
        batch: list[tuple[str, str]] = []
        for index, (first, second) in enumerate(pairs):
            if not (self._fits(first, second) and self._fits(second, first)):
                out.append((None, None, "input_too_long"))
                continue
            out.append((None, None, None))
            runnable.append(index)
            batch.extend(((first, second), (second, first)))
        if batch:
            logits = self._model.predict(
                batch, apply_softmax=False, convert_to_numpy=True, show_progress_bar=False
            )
            rows = _softmax_rows(logits)
            if rows is None:
                raise ValueError("non-finite or misshapen logits; no reading is recorded")
            for offset, index in enumerate(runnable):
                out[index] = (rows[2 * offset], rows[2 * offset + 1], None)
        return out


def judge_fixtures(instrument: Any, fixture_set: str) -> tuple[bool, str, list[dict[str, Any]]]:
    """Run a fixture set through an instrument and the label map.

    Returns `(green, detail, results)`. One miss is red: partial evidence is none.
    """
    fixtures = sensing.fixtures(fixture_set)
    if not fixtures:
        return False, f"unknown fixture set {fixture_set!r}", []
    label_map = sensing.label_map(instrument.identity.label_map_version)
    # A slotted fixture is read in slot order. A relation pair is unordered, so it
    # is fed in text-hash order, as the sensor worker feeds it.
    ordered = []
    for item in fixtures:
        if isinstance(item, sensing.CoverageFixture):
            ordered.append((item, False, (item.premise, item.hypothesis)))
            continue
        swap = sensing.text_sha256(item.text_b) < sensing.text_sha256(item.text_a)
        ordered.append((item, swap, (item.text_b, item.text_a) if swap else (item.text_a, item.text_b)))
    judged = instrument.judge([pair for _item, _swap, pair in ordered])
    results: list[dict[str, Any]] = []
    misses: list[str] = []
    for (item, swap, _pair), (ab, ba, reason) in zip(ordered, judged, strict=True):
        vectors = None if reason else (sensing.round_vector(ab), sensing.round_vector(ba))
        verdict = label_map.apply(
            None if vectors is None else vectors[0],
            None if vectors is None else vectors[1],
            abstain_reason=reason,
        )
        produced = "no label" if verdict is None else verdict.label
        refining = None
        if verdict is not None and verdict.label == "refines":
            first_refines = verdict.direction == "ab"
            refining = ("b" if first_refines else "a") if swap else ("a" if first_refines else "b")
        results.append(
            {
                "note": item.note,
                "expected": item.expected,
                "produced": produced,
                "refining": refining,
                "vectors": vectors,
            }
        )
        wanted_refining = getattr(item, "refining", None)
        if produced != item.expected or (item.expected == "refines" and refining != wanted_refining):
            misses.append(f"{item.note!r}: expected {item.expected}/{wanted_refining}, got {produced}/{refining}")
    if misses:
        return False, "; ".join(misses), results
    return True, f"{len(fixtures)} fixtures green for {fixture_set!r}", results


_ADMITTED: dict[tuple[str, str], tuple[bool, str]] = {}
_LOADED: dict[str, NliInstrument] = {}
#: One resident model per pinned weights digest, shared by both templates: the
#: weights are the only large allocation and a template adds none.
_MODELS: dict[str, Any] = {}
_LOCK = threading.Lock()


def _fixture_digest(ident: sensing.InstrumentIdentity) -> str:
    """What a green verdict was earned against: the fixtures and the label map."""
    import dataclasses
    import hashlib
    import json

    payload = json.dumps(
        [
            [dataclasses.asdict(item) for item in sensing.fixtures(ident.fixture_set)],
            dataclasses.asdict(sensing.label_map(ident.label_map_version)),
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recorded_green(evidence_path: Path | None, key: str) -> bool:
    if evidence_path is None:
        return False
    import json

    try:
        return json.loads(evidence_path.read_text(encoding="utf-8")).get(key) is True
    except (OSError, ValueError, AttributeError):
        return False


def _record_green(evidence_path: Path | None, key: str) -> None:
    """Add `key` to the memo, keeping every other identity's verdict.

    The memo holds one entry per admitted question, so a second question's
    write must not erase the first's. An unreadable memo restarts empty: the
    cost is one repeated fixture run, never a wrong admission.
    """
    if evidence_path is None:
        return
    import json
    import os

    try:
        try:
            memo = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            memo = {}
        if not isinstance(memo, dict):
            memo = {}
        memo[key] = True
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = evidence_path.with_name(evidence_path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(memo, sort_keys=True), encoding="utf-8")
        os.replace(tmp, evidence_path)
    except OSError:
        log.debug("sensing: admission evidence not recorded", exc_info=True)


def _build_model(snapshot: str) -> Any:
    import torch
    from sentence_transformers import CrossEncoder

    torch.set_num_threads(1)
    return CrossEncoder(str(Path(snapshot)), device="cpu", local_files_only=True)


def _load_model(pin: Any, snapshot: str) -> Any:
    """The resident CrossEncoder for a pin, loaded once per weights digest."""
    with _LOCK:
        cached = _MODELS.get(pin.weights_sha256)
    if cached is not None:
        return cached
    model = _build_model(snapshot)
    with _LOCK:
        return _MODELS.setdefault(pin.weights_sha256, model)


def admit(
    evidence_path: Path | None = None, question_type: str = sensing.PAIR_RELATION
) -> NliInstrument:
    """Load and admit the pinned instrument for a question on CPU, or raise `Refused`.

    Only the sensor worker child calls this. The fixture run is the costly half
    of admission (about 20 CPU-seconds on one thread), so a green verdict is
    recorded at `evidence_path`, keyed by the instrument id (model, revision,
    verified weights digest, runtime and template) and a digest of the fixture
    set and label map. A later child with that exact identity reuses it. The
    weights digest itself is re-verified on every admission.
    """
    pin = claims._active_pin()
    if pin is None:
        raise Refused("no-pin", "the repository pin registry is empty")
    ident = identity(question_type)
    if ident is None:
        raise Refused("dependency-missing", "the `nli` extra is not installed")
    try:
        sensing.label_map(ident.label_map_version)
    except ValueError as error:
        raise Refused("label-map-unknown", str(error)) from None
    digest, detail = claims._resolve_weights(pin)
    if digest is None:
        raise Refused("weights-missing", detail)
    if digest != pin.weights_sha256:
        raise Refused("digest-mismatch", f"resolved {digest}, pinned {pin.weights_sha256}")
    with _LOCK:
        loaded = _LOADED.get(ident.instrument_id)
        verdict = _ADMITTED.get((ident.instrument_id, ident.fixture_set))
    if loaded is not None and verdict is not None and verdict[0]:
        return loaded
    try:
        model = _load_model(pin, detail)
    except Exception as error:  # noqa: BLE001 - an absent extra or bad load is a refusal
        raise Refused("dependency-missing", f"{type(error).__name__}: {error}") from None
    labels = getattr(getattr(model, "config", None), "id2label", None) or getattr(
        getattr(getattr(model, "model", None), "config", None), "id2label", None
    )
    if isinstance(labels, dict):
        declared = tuple(str(labels[key]).strip().casefold() for key in sorted(labels))
        if declared != _COLUMNS:
            raise Refused("label-map-unknown", f"head declares {declared}, map needs {_COLUMNS}")
    instrument = NliInstrument(ident, model)
    key = (ident.instrument_id, ident.fixture_set)
    evidence_key = f"{ident.instrument_id}:{_fixture_digest(ident)}"
    with _LOCK:
        verdict = _ADMITTED.get(key)
        if verdict is None and _recorded_green(evidence_path, evidence_key):
            verdict = (True, "fixtures green (recorded for this exact identity)")
            _ADMITTED[key] = verdict
        if verdict is None:
            green, fixture_detail, _results = judge_fixtures(instrument, ident.fixture_set)
            verdict = (green, fixture_detail)
            _ADMITTED[key] = verdict
            if green:
                _record_green(evidence_path, evidence_key)
    if not verdict[0]:
        raise Refused("fixtures-failed", verdict[1])
    with _LOCK:
        _LOADED[ident.instrument_id] = instrument
    return instrument
