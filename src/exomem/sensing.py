"""Instruments, the question registry and the reading record.

A model is an INSTRUMENT: it answers one closed question from this module's
registry, with probabilities over a closed label set plus `abstain`, into the
append-only readings ledger (`sensing_ledger`). It never writes, rewrites or
decides anything. The dreamer models deterministically over those readings
(`sensed_model`), and the agent alone authors canon.

This module is pure: no model import, no I/O beyond the per-machine setting.
Every registry entry, label map, unit scope and fixture set is a repository
artifact. No environment value, runtime configuration or vault content may
add, select or alter one, so "a question nobody reviewed never senses" is a
property of the code rather than of deployment configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The operator switch: `EXOMEM_SENSING`, else the per-machine config key
#: `sensing`, else `off`. Effective only while the dreamer runs.
ENV = "EXOMEM_SENSING"
CONFIG_KEY = "sensing"
SETTINGS: tuple[str, ...] = ("off", "on")
DEFAULT_SETTING = "off"

PAIR_RELATION = "pair.relation"
RELATION_TYPE = "relation.type"

#: Placements an instrument may run in. Only `local-cpu` exists in this build.
PLACEMENTS = frozenset({"local-cpu", "cloud-plane", "api"})

#: Why a reading abstained. A closed code, never prose.
ABSTAIN_REASONS = frozenset({"directional_asymmetry", "input_too_long", "low_confidence"})

#: Probabilities are stored at this precision, and every verdict is derived
#: from the stored (rounded) vector, so a replay reproduces it exactly.
VECTOR_DECIMALS = 6


def resolve_setting(env_value: str | None, config: Mapping[str, Any] | None) -> str:
    """`EXOMEM_SENSING`, else the config key `sensing`, else `off`.

    An unknown value at either tier falls through rather than enabling
    anything: the safe reading of a typo is the default.
    """
    for raw in (env_value, (config or {}).get(CONFIG_KEY)):
        value = str(raw or "").strip().lower()
        if value in SETTINGS:
            return value
    return DEFAULT_SETTING


#: How long a config-file reading of the setting is trusted. The status check
#: sits on every page read, so sensing off must add no per-read I/O; a changed
#: config file takes effect within this long (the env var takes effect at once).
#: A reading belongs to the config file it came from (`EXOMEM_CONFIG_PATH`).
CONFIG_MEMO_SECONDS = 5.0
#: `mode.config_path`'s override; read here without importing `mode` per call.
CONFIG_PATH_ENV = "EXOMEM_CONFIG_PATH"
_CONFIG_MEMO: list[tuple[float, str | None, str]] = []
_CONFIG_LOCK = threading.Lock()


def clear_memo() -> None:
    with _CONFIG_LOCK:
        _CONFIG_MEMO.clear()


def setting() -> str:
    """`EXOMEM_SENSING`, else the memoised config key `sensing`, else `off`."""
    env_value = str(os.environ.get(ENV) or "").strip().lower()
    if env_value in SETTINGS:
        return env_value
    now = time.monotonic()
    config_env = os.environ.get(CONFIG_PATH_ENV)
    with _CONFIG_LOCK:
        if _CONFIG_MEMO:
            stamped, memo_env, memo_value = _CONFIG_MEMO[0]
            if memo_env == config_env and now - stamped < CONFIG_MEMO_SECONDS:
                return memo_value
    from . import mode

    value = resolve_setting(None, mode.read_config())
    with _CONFIG_LOCK:
        _CONFIG_MEMO[:] = [(now, config_env, value)]
    return value


def enabled() -> bool:
    return setting() == "on"


# ----------------------------------------------------------------------
# label maps
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """One closed-set verdict. `direction` names the entailing order for
    `refines`: `ab` means the first input entails (refines) the second."""

    label: str
    p: float | None
    direction: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "p": self.p, "direction": self.direction, "reason": self.reason}


@dataclass(frozen=True)
class RelationLabelMap:
    """`pair.relation`'s versioned map from two directional probability vectors.

    Rules, in order: symmetric contradiction; mutual entailment (restatement);
    a one-way contradiction is its own state (`abstain: directional_asymmetry`)
    and never collapses into another label; one-way entailment (refinement,
    with the entailing order); otherwise neutral. `neutral` means only that the
    head established neither relation at the reviewed thresholds, never that
    the texts are unrelated.
    """

    version: str
    columns: tuple[str, ...]
    contradicts_min: float
    restates_min: float
    refines_min: float
    labels: tuple[str, ...] = ("contradicts", "refines", "restates", "neutral", "abstain")

    def apply(
        self,
        ab: Sequence[float] | None,
        ba: Sequence[float] | None,
        *,
        abstain_reason: str | None = None,
    ) -> Verdict | None:
        """The verdict for two stored vectors, or None when they cannot be read.

        None is a refusal (wrong width, non-finite), never a label.
        """
        if abstain_reason is not None:
            return Verdict("abstain", None, None, abstain_reason)
        if ab is None or ba is None:
            return None
        try:
            vectors = [[float(value) for value in ab], [float(value) for value in ba]]
        except (TypeError, ValueError):
            return None
        width = len(self.columns)
        if any(len(vector) != width for vector in vectors):
            return None
        if any(not _finite(value) for vector in vectors for value in vector):
            return None
        entail = self.columns.index("entailment")
        neutral = self.columns.index("neutral")
        contra = self.columns.index("contradiction")
        c_ab, c_ba = vectors[0][contra], vectors[1][contra]
        e_ab, e_ba = vectors[0][entail], vectors[1][entail]
        if min(c_ab, c_ba) >= self.contradicts_min:
            return Verdict("contradicts", _p(min(c_ab, c_ba)))
        if min(e_ab, e_ba) >= self.restates_min:
            return Verdict("restates", _p(min(e_ab, e_ba)))
        if max(c_ab, c_ba) >= self.contradicts_min:
            return Verdict("abstain", _p(max(c_ab, c_ba)), None, "directional_asymmetry")
        if max(e_ab, e_ba) >= self.refines_min:
            direction = "ab" if e_ab >= e_ba else "ba"
            return Verdict("refines", _p(max(e_ab, e_ba)), direction)
        return Verdict("neutral", _p((vectors[0][neutral] + vectors[1][neutral]) / 2.0))


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _p(value: float) -> float:
    return round(float(value), VECTOR_DECIMALS)


@dataclass(frozen=True)
class RelationTypeLabelMap:
    """`relation.type`'s versioned map over one menu-shaped `answer` vector.

    Its columns are per-reading (the request's menu keys in order, then
    `none`), so unlike `RelationLabelMap` it declares no fixed `columns`; the
    stored reading's own columns validate and derive the verdict.

    Rules, in order: an instrument abstention is already a verdict; the top
    key is a relation verdict when the top column is a menu key at or above
    `propose_min`; `none` when the top column is the none column; otherwise
    `abstain: low_confidence`.
    """

    version: str
    propose_min: float
    #: A variable-column map declares no fixed columns; `None` tells readers
    #: to take the stored reading's columns.
    columns: None = None

    def apply_vectors(
        self,
        vectors: Mapping[str, Sequence[float]] | None,
        *,
        columns: Sequence[str] | None,
    ) -> Verdict | None:
        if vectors is None or set(vectors) != {"answer"} or not columns:
            return None
        if str(columns[-1]) != "none":
            return None
        try:
            vector = [float(value) for value in vectors["answer"]]
        except (TypeError, ValueError):
            return None
        if len(vector) != len(columns) or any(not _finite(value) for value in vector):
            return None
        if abs(sum(vector) - 1.0) > 0.02:
            return None
        top = max(range(len(vector)), key=lambda index: vector[index])
        key, p = str(columns[top]), vector[top]
        if key != "none" and p >= self.propose_min:
            return Verdict(key, _p(p))
        if key == "none":
            return Verdict("none", _p(p))
        return Verdict("abstain", _p(p), None, "low_confidence")


#: Every label map this build can load. The thresholds equal the stance
#: verifier's reviewed `v2` ones, so its fixture evidence carries over.
LABEL_MAPS: dict[str, RelationLabelMap | RelationTypeLabelMap] = {
    "relation-v1": RelationLabelMap(
        version="relation-v1",
        columns=("entailment", "neutral", "contradiction"),
        contradicts_min=0.93,
        restates_min=0.95,
        refines_min=0.95,
    ),
    # Provisional until a pin's measured evidence fixes it (D6); admission
    # refuses without evidence for this exact map.
    "relation-type-v1": RelationTypeLabelMap(version="relation-type-v1", propose_min=0.5),
}


def label_map(version: str) -> RelationLabelMap | RelationTypeLabelMap:
    if not version:
        raise ValueError("label map version is required")
    try:
        return LABEL_MAPS[version]
    except KeyError:
        raise ValueError(f"unknown label map version: {version!r}") from None


# ----------------------------------------------------------------------
# the question registry
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class UnitScope:
    """Which units a question reads: registry keys, never words in the text."""

    version: str
    compact_categories: frozenset[str]
    rich_kinds: frozenset[str]
    max_chars: int = 2000

    def admits(self, *, kind: str, category: str | None) -> bool:
        kind = str(kind or "").strip().casefold()
        if kind == "observation":
            return str(category or "").strip().casefold() in self.compact_categories
        return kind in self.rich_kinds


@dataclass(frozen=True)
class Question:
    question_type: str
    template_version: str
    label_map_version: str
    unit_scope: UnitScope
    fixture_set: str
    # Closed request and vector shapes belong to the reviewed question contract.
    slot_names: tuple[str, ...] = ("a", "b")
    extractors: tuple[str, ...] = ("unit-text-v1", "page-claim-v1")
    vector_names: tuple[str, ...] = ("ab", "ba")


# Debt (C10): the scope of the frozen `units-v1` instrument; a registry read would let vault data
# change which units it compares, and so invalidate its fixtures.
UNITS_V1 = UnitScope(
    version="units-v1",
    compact_categories=frozenset(
        {
            "claim",
            "decision",
            "fact",
            "finding",
            "insight",
            "constraint",
            "assumption",
            "risk",
            "preference",
        }
    ),
    rich_kinds=frozenset(
        {
            "claim",
            "finding",
            "evidence",
            "decision",
            "assumption",
            "inference",
            "constraint",
            "hypothesis",
            "prediction",
            "result",
            "pattern",
        }
    ),
)

#: `relation.type` reads whole pages, not units: its scope admits nothing at
#: unit level, so the unit proposer never feeds it.
PAGES_V1 = UnitScope(
    version="pages-v1",
    compact_categories=frozenset(),
    rich_kinds=frozenset(),
)

#: `relation.type`'s extractor per slot, in slot order. Other questions only
#: constrain membership; this one's extractors are positional.
SLOT_EXTRACTORS: dict[str, dict[str, str]] = {
    RELATION_TYPE: {
        "source": "page-excerpt-v1",
        "target": "page-excerpt-v1",
        "link": "link-sentence-v1",
        "menu": "relation-menu-v1",
    }
}

#: The registry. Later entries (`mention.same_referent`, `recap.covered_by`,
#: ...) are specified in `add-sensed-epistemic-model` and land with their
#: instruments; an entry exists here only once something can answer it.
QUESTIONS: dict[str, Question] = {
    PAIR_RELATION: Question(
        question_type=PAIR_RELATION,
        template_version="nli-pair-v1",
        label_map_version="relation-v1",
        unit_scope=UNITS_V1,
        fixture_set="relation-v1-multilingual",
    ),
    RELATION_TYPE: Question(
        question_type=RELATION_TYPE,
        template_version="relation-type-v1",
        label_map_version="relation-type-v1",
        unit_scope=PAGES_V1,
        fixture_set="relation-type-v1-judged",
        slot_names=("source", "target", "link", "menu"),
        extractors=("page-excerpt-v1", "link-sentence-v1", "relation-menu-v1"),
        vector_names=("answer",),
    ),
}


def question(question_type: str) -> Question:
    try:
        return QUESTIONS[question_type]
    except KeyError:
        raise ValueError(f"unknown question type: {question_type!r}") from None


# ----------------------------------------------------------------------
# inputs: the exact text fed in, and its hash
# ----------------------------------------------------------------------

#: How a unit's text is taken from the graph before it is fed to an instrument.
EXTRACTOR = "unit-text-v1"
_SPACE = re.compile(r"\s+")


def extract_text(raw: str | None) -> str:
    """`unit-text-v1`: NFC, whitespace runs collapsed to one space, stripped."""
    return _SPACE.sub(" ", unicodedata.normalize("NFC", str(raw or ""))).strip()


def text_sha256(text: str) -> str:
    """The input hash: sha256 of the exact text fed in (as `claims._checksum`)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ordered_hashes(hashes: Sequence[str]) -> tuple[str, ...]:
    """Inputs are ordered by text hash, which makes a pair unordered."""
    return tuple(sorted(str(value) for value in hashes))


def input_key(question_type: str, hashes: Sequence[str]) -> str:
    """Lookup key for the same inputs under any instrument."""
    return _digest("input", question_type, list(ordered_hashes(hashes)))


def reading_id(question_type: str, instrument_id: str, hashes: Sequence[str]) -> str:
    return _digest("reading", question_type, instrument_id, list(ordered_hashes(hashes)))


def edge_fingerprint(question_type: str, hashes: Sequence[str], verdict: Verdict) -> str:
    """Binds to the inputs and the verdict, never to the reading id, so a replay
    or a re-sense that agrees yields the same fingerprint."""
    return _digest(
        "edge", question_type, list(ordered_hashes(hashes)), verdict.label, verdict.direction
    )[:24]


# ----------------------------------------------------------------------
# instruments
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentIdentity:
    """Everything that names an instrument.

    `instrument_id` covers what determines the stored vectors. The label map
    and fixture set are recorded but deliberately outside it: neither changes a
    vector, so a label-map change re-derives verdicts instead of re-sensing.
    """

    model: str
    revision: str
    weights_sha256: str | None
    runtime: str
    runtime_version: str
    template_version: str
    label_map_version: str
    fixture_set: str
    placement: str = "local-cpu"
    label_token_map: str | None = None

    @property
    def unpinned_weights(self) -> bool:
        return self.weights_sha256 is None

    @property
    def instrument_id(self) -> str:
        parts = (
            "instrument",
            self.model,
            self.revision,
            self.weights_sha256 or "unpinned",
            self.runtime,
            self.runtime_version,
            self.template_version,
        )
        return (
            _digest(*parts)
            if self.label_token_map is None
            else _digest(*parts, self.label_token_map)
        )

    def as_dict(self) -> dict[str, Any]:
        result = {
            "model": self.model,
            "revision": self.revision,
            "weights_sha256": self.weights_sha256,
            "unpinned_weights": self.unpinned_weights,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "template_version": self.template_version,
            "label_map_version": self.label_map_version,
            "fixture_set": self.fixture_set,
            "placement": self.placement,
        }
        if self.label_token_map is not None:
            result["label_token_map"] = self.label_token_map
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> InstrumentIdentity:
        return cls(
            model=str(data["model"]),
            revision=str(data["revision"]),
            weights_sha256=(
                None if data.get("weights_sha256") is None else str(data["weights_sha256"])
            ),
            runtime=str(data["runtime"]),
            runtime_version=str(data["runtime_version"]),
            template_version=str(data["template_version"]),
            label_map_version=str(data["label_map_version"]),
            fixture_set=str(data["fixture_set"]),
            placement=str(data.get("placement") or "local-cpu"),
            label_token_map=data.get("label_token_map"),
        )


# ----------------------------------------------------------------------
# the reading record
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class InputUnit:
    unit_ref: str
    path: str
    text_sha256: str
    extractor: str = EXTRACTOR

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit_ref": self.unit_ref,
            "path": self.path,
            "text_sha256": self.text_sha256,
            "extractor": self.extractor,
        }


@dataclass(frozen=True)
class InputSlot:
    unit_ref: str
    path: str
    slot: str
    extractor: str
    text_sha256: str
    anchor: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = {
            "unit_ref": self.unit_ref,
            "path": self.path,
            "slot": self.slot,
            "extractor": self.extractor,
            "text_sha256": self.text_sha256,
        }
        if self.anchor is not None:
            value["anchor"] = self.anchor
        return value


def request_input_key(
    question_type: str, input_version: int, inputs: Sequence[InputUnit | InputSlot]
) -> str:
    if input_version == 1:
        return input_key(question_type, [item.text_sha256 for item in inputs])
    return _digest(
        "input",
        2,
        question_type,
        [[item.slot, item.extractor, item.text_sha256, item.anchor] for item in inputs],
    )


def request_reading_id(
    question_type: str,
    instrument_id: str,
    input_version: int,
    inputs: Sequence[InputUnit | InputSlot],
) -> str:
    if input_version == 1:
        return reading_id(question_type, instrument_id, [item.text_sha256 for item in inputs])
    return _digest(
        "reading",
        2,
        question_type,
        instrument_id,
        [[item.slot, item.extractor, item.text_sha256, item.anchor] for item in inputs],
    )


@dataclass(frozen=True)
class SenseRequest:
    question_type: str
    input_version: int
    inputs: tuple[InputUnit | InputSlot, ...]
    texts: tuple[str, ...]

    def __post_init__(self) -> None:
        entry = question(self.question_type)
        if type(self.input_version) is not int:
            raise ValueError("input version must be an integer")
        if any(not isinstance(text, str) for text in self.texts):
            raise ValueError("input texts must be strings")
        if any(not isinstance(item, (InputUnit, InputSlot)) for item in self.inputs):
            raise ValueError("inputs must be typed units or slots")
        if len(self.inputs) != len(self.texts) or not self.inputs:
            raise ValueError("each input needs its exact text")
        if any(
            text_sha256(text) != item.text_sha256
            for item, text in zip(self.inputs, self.texts, strict=True)
        ):
            raise ValueError("input text hash mismatch")
        if self.input_version == 1:
            if self.question_type != PAIR_RELATION or len(self.inputs) != 2:
                raise ValueError("legacy inputs require pair.relation")
            if any(
                type(item) is not InputUnit or item.extractor != EXTRACTOR for item in self.inputs
            ):
                raise ValueError("legacy inputs require unit-text-v1")
            if tuple(item.text_sha256 for item in self.inputs) != ordered_hashes(
                [item.text_sha256 for item in self.inputs]
            ):
                raise ValueError("legacy inputs must be ordered by text hash")
        elif self.input_version == 2:
            if any(not isinstance(item, InputSlot) for item in self.inputs):
                raise ValueError("version 2 requires named input slots")
            if tuple(item.slot for item in self.inputs) != entry.slot_names:
                raise ValueError("input slots differ from the question contract")
            if any(item.extractor not in entry.extractors for item in self.inputs):
                raise ValueError("input extractor differs from the question contract")
            positional = SLOT_EXTRACTORS.get(self.question_type)
            if positional is not None and any(
                item.extractor != positional[item.slot] for item in self.inputs
            ):
                raise ValueError("input extractor differs from the slot contract")
        else:
            raise ValueError("unknown input version")

    @property
    def input_key(self) -> str:
        return request_input_key(self.question_type, self.input_version, self.inputs)

    def reading_id(self, instrument_id: str) -> str:
        return request_reading_id(
            self.question_type, instrument_id, self.input_version, self.inputs
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_type": self.question_type,
            "input_version": self.input_version,
            "inputs": [item.as_dict() for item in self.inputs],
            "texts": list(self.texts),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SenseRequest:
        version = value["input_version"]
        kind = InputUnit if version == 1 else InputSlot
        return cls(
            str(value["question_type"]),
            version,
            tuple(kind(**item) for item in value["inputs"]),
            tuple(value["texts"]),
        )


@dataclass(frozen=True)
class Reading:
    reading_id: str
    question_type: str
    instrument: InstrumentIdentity
    inputs: tuple[InputUnit | InputSlot, ...]
    #: `{"ab": [...], "ba": [...]}` in input order, or None for an abstention
    #: the instrument decided before any vector existed.
    vectors: Mapping[str, Sequence[float]] | None
    verdict: Verdict
    sensed_at: str
    placement: str
    columns: tuple[str, ...] = field(default=("entailment", "neutral", "contradiction"))
    input_version: int = 1

    @property
    def hashes(self) -> tuple[str, ...]:
        return tuple(item.text_sha256 for item in self.inputs)

    @property
    def input_key(self) -> str:
        return request_input_key(self.question_type, self.input_version, self.inputs)

    def output(self) -> dict[str, Any]:
        if self.input_version == 2:
            return {
                "version": 2,
                "columns": list(self.columns),
                "vectors": None
                if self.vectors is None
                else {key: list(value) for key, value in self.vectors.items()},
                "verdict": self.verdict.as_dict(),
            }
        return {
            "columns": list(self.columns),
            "directions": (
                None
                if self.vectors is None
                else {key: list(self.vectors[key]) for key in ("ab", "ba")}
            ),
            "verdict": self.verdict.as_dict(),
        }


def round_vector(values: Sequence[float]) -> list[float]:
    return [round(float(value), VECTOR_DECIMALS) for value in values]


def make_reading(
    question_type: str,
    instrument: InstrumentIdentity,
    inputs: Sequence[InputUnit],
    *,
    ab: Sequence[float] | None,
    ba: Sequence[float] | None,
    abstain_reason: str | None,
    sensed_at: str,
) -> Reading | None:
    """One reading, with its verdict derived from the rounded vectors.

    `inputs` must already be in text-hash order and `ab`/`ba` in that order.
    None when the vectors cannot be read (a refusal, never a label).
    """
    entry = question(question_type)
    if abstain_reason is not None and abstain_reason not in ABSTAIN_REASONS:
        raise ValueError(f"unknown abstain reason: {abstain_reason!r}")
    ordered = tuple(inputs)
    if [item.text_sha256 for item in ordered] != list(ordered_hashes([i.text_sha256 for i in ordered])):
        raise ValueError("inputs must be ordered by text hash")
    vectors = None
    if abstain_reason is None:
        if ab is None or ba is None:
            return None
        vectors = {"ab": round_vector(ab), "ba": round_vector(ba)}
    verdict = label_map(entry.label_map_version).apply(
        None if vectors is None else vectors["ab"],
        None if vectors is None else vectors["ba"],
        abstain_reason=abstain_reason,
    )
    if verdict is None:
        return None
    return Reading(
        reading_id=reading_id(question_type, instrument.instrument_id, [i.text_sha256 for i in ordered]),
        question_type=question_type,
        instrument=instrument,
        inputs=ordered,
        vectors=vectors,
        verdict=verdict,
        sensed_at=sensed_at,
        placement=instrument.placement,
        columns=label_map(entry.label_map_version).columns,
    )


def rederive(
    reading_vectors: Mapping[str, Sequence[float]] | None,
    stored: Verdict,
    version: str,
    *,
    columns: Sequence[str] | None = None,
    vector_names: Sequence[str] = ("ab", "ba"),
) -> Verdict | None:
    """Validate the stored output at the consumer boundary, then apply the active map."""
    mapping = label_map(version)
    # A variable-column map declares `columns = None`: the stored reading's own
    # columns validate the width and the trailing none column. A consumer that
    # supplies none cannot validate or re-derive such a reading.
    fixed_columns = getattr(mapping, "columns", None)
    if fixed_columns is None and not columns:
        return None
    if columns is not None and fixed_columns is not None and tuple(columns) != fixed_columns:
        return None
    if columns is not None and fixed_columns is None and str(columns[-1]) != "none":
        return None
    if reading_vectors is None:
        return stored if stored.label == "abstain" else None
    if set(reading_vectors) != set(vector_names):
        return None
    width = len(columns) if fixed_columns is None else len(fixed_columns)
    try:
        if any(
            len(vector) != width or any(not _finite(float(v)) for v in vector)
            for vector in reading_vectors.values()
        ):
            return None
    except (TypeError, ValueError):
        return None
    if hasattr(mapping, "apply_vectors"):
        # Only a variable-column map takes the reading's columns; a fixed-column
        # map's apply_vectors keeps its one-argument shape.
        if fixed_columns is None:
            return mapping.apply_vectors(reading_vectors, columns=columns)
        return mapping.apply_vectors(reading_vectors)
    return mapping.apply(reading_vectors.get("ab"), reading_vectors.get("ba"))


def make_request_reading(
    request: SenseRequest,
    instrument: InstrumentIdentity,
    *,
    vectors: Mapping[str, Sequence[float]] | None,
    abstain_reason: str | None,
    sensed_at: str,
) -> Reading | None:
    entry = question(request.question_type)
    mapping = label_map(entry.label_map_version)
    columns: Sequence[str] | None = None
    if mapping.columns is None:
        # The columns live in the request's menu slot, not the instrument's
        # result: the same request bytes that were judged.
        from . import sensing_llm

        try:
            _entries, columns = sensing_llm.menu_columns(request)
        except (ValueError, TypeError, KeyError):
            return None
    rounded = (
        None
        if vectors is None
        else {name: round_vector(values) for name, values in vectors.items()}
    )
    stored = Verdict("abstain", None, reason=abstain_reason)
    if abstain_reason is not None:
        if abstain_reason not in ABSTAIN_REASONS or vectors is not None:
            raise ValueError("invalid instrument abstention")
    elif vectors is None:
        return None
    verdict = rederive(
        rounded,
        stored,
        entry.label_map_version,
        columns=columns,
        vector_names=entry.vector_names,
    )
    if verdict is None:
        return None
    stored_columns = mapping.columns if mapping.columns is not None else tuple(columns or ())
    return Reading(
        request.reading_id(instrument.instrument_id),
        request.question_type,
        instrument,
        request.inputs,
        rounded,
        verdict,
        sensed_at,
        instrument.placement,
        stored_columns,
        request.input_version,
    )


# ----------------------------------------------------------------------
# fixtures: the admission evidence, as repository data
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RelationFixture:
    """One pair an admitted `pair.relation` instrument must label exactly.

    `refining` names which text refines the other for `refines` (`a` or `b`).
    `twin_of` marks a negative twin: a minimally different pair that must NOT
    receive the label of the pair it twins.
    """

    text_a: str
    text_b: str
    expected: str
    note: str
    language_shape: str
    refining: str | None = None
    twin_of: str | None = None


RELATION_FIXTURES: dict[str, tuple[RelationFixture, ...]] = {
    "relation-v1-multilingual": (
        RelationFixture(
            "Raising the cache TTL reduces p99 latency.",
            "Raising the cache TTL increases p99 latency.",
            "contradicts",
            "genuine contradiction, shared vocabulary",
            "en/en",
        ),
        RelationFixture(
            "The rollout regressed checkout throughput.",
            "Checkout got faster after we shipped it.",
            "contradicts",
            "genuine contradiction across differing surface forms",
            "en/en",
        ),
        RelationFixture(
            "Caching improves latency for repeat reads.",
            "Caching improves latency on repeat reads.",
            "restates",
            "restatement, near-identical surface",
            "en/en",
        ),
        RelationFixture(
            "Owned files are what retrieval quality depends on.",
            "Retrieval quality depends on owning the files.",
            "restates",
            "restatement, reordered surface",
            "en/en",
        ),
        RelationFixture(
            "Batching similar work helps focus.",
            "Batching similar work helps focus, most reliably in the morning.",
            "refines",
            "same stance, added detail",
            "en/en",
            refining="b",
        ),
        RelationFixture(
            "Enabling the cache improves throughput.",
            "Disabling the cache degrades throughput.",
            "neutral",
            "compatible but non-entailing evidence remains neutral",
            "en/en",
        ),
        RelationFixture(
            "Batching does not hurt focus.",
            "Batching helps focus.",
            "refines",
            "concordant evidence: negation parity differs, one stance",
            "en/en",
            refining="b",
        ),
        RelationFixture(
            "Tesseract is required for image OCR on Windows.",
            "Pair dormancy reuses the stale-review activation calculation.",
            "neutral",
            "disjoint topics are inside NLI's neutral fallback",
            "en/en",
        ),
        RelationFixture(
            "The upload route parses multipart bodies through Starlette.",
            "Dormant notes in a close pair are the forgotten-conclusion case.",
            "neutral",
            "disjoint topics with shared house vocabulary remain neutral",
            "en/en",
        ),
        RelationFixture(
            "Der Cache reduziert die Latenz.",
            "Der Cache erhöht die Latenz.",
            "contradicts",
            "same-language German contradiction",
            "de/de",
        ),
        RelationFixture(
            "La sauvegarde démarre chaque nuit.",
            "La sauvegarde démarre chaque nuit à deux heures.",
            "refines",
            "same-language French added detail",
            "fr/fr",
            refining="b",
        ),
        RelationFixture(
            "Varukoopia käivitub igal ööl.",
            "Igal ööl käivitatakse varukoopia.",
            "restates",
            "same-language Estonian reordered restatement",
            "et/et",
        ),
        RelationFixture(
            "The cache reduces latency.",
            "Vahemälu suurendab latentsust.",
            "contradicts",
            "mixed English/Estonian contradiction",
            "en/et",
        ),
        RelationFixture(
            "The backup runs every night.",
            "Varukoopia käivitub igal ööl.",
            "restates",
            "mixed English/Estonian equivalence",
            "en/et",
        ),
        RelationFixture(
            "The backup runs every night.",
            "Varukoopia käivitub igal ööl kell kaks.",
            "refines",
            "mixed English/Estonian added detail",
            "en/et",
            refining="b",
        ),
        RelationFixture(
            "The cache reduces latency.",
            "Varukoopia käivitub igal ööl.",
            "neutral",
            "mixed English/Estonian neutral pair",
            "en/et",
        ),
        # Negative twins: one per label, minimally different, which must NOT
        # receive the twinned label.
        RelationFixture(
            "Caching improves latency for repeat reads.",
            "Caching worsens latency for repeat reads.",
            "contradicts",
            "twin of a restatement: one antonym flips it",
            "en/en",
            twin_of="restates",
        ),
        RelationFixture(
            "Der Cache reduziert die Latenz.",
            "Der Cache reduziert die Latenz bei wiederholten Lesezugriffen.",
            "refines",
            "twin of the German contradiction: added detail, same stance",
            "de/de",
            refining="b",
            twin_of="contradicts",
        ),
        RelationFixture(
            "La sauvegarde démarre chaque nuit.",
            "La sauvegarde ne démarre jamais la nuit.",
            "contradicts",
            "twin of the French refinement: negated instead of detailed",
            "fr/fr",
            twin_of="refines",
        ),
        RelationFixture(
            "Tesseract is required for image OCR on Windows.",
            "Image OCR on Windows requires Tesseract.",
            "restates",
            "twin of a neutral pair: the same fact, reordered",
            "en/en",
            twin_of="neutral",
        ),
    ),
}


def fixture_counts(fixture_set: str) -> dict[str, int]:
    """Fixture pairs per expected label: the size of each label's evidence."""
    counts: dict[str, int] = {}
    for item in RELATION_FIXTURES.get(fixture_set, ()):
        counts[item.expected] = counts.get(item.expected, 0) + 1
    return counts


def fixture_precision(fixture_set: str, label: str) -> dict[str, Any]:
    """What a served item says about its label's evidence at the admitted pin.

    Admission refuses on any miss, so an admitted instrument answered every
    pair of `label` correctly: the honest statement is the count, not a
    probability-shaped claim about unseen text.
    """
    total = fixture_counts(fixture_set).get(label, 0)
    return {"set": fixture_set, "label": label, "correct": total, "total": total}
