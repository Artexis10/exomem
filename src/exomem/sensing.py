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

#: Placements an instrument may run in. Only `local-cpu` exists in this build.
PLACEMENTS = frozenset({"local-cpu", "cloud-plane", "api"})

#: Why a reading abstained. A closed code, never prose.
ABSTAIN_REASONS = frozenset({"directional_asymmetry", "input_too_long"})

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


def setting() -> str:
    from . import mode

    return resolve_setting(os.environ.get(ENV), mode.read_config())


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


#: Every label map this build can load. The thresholds equal the stance
#: verifier's reviewed `v2` ones, so its fixture evidence carries over.
LABEL_MAPS: dict[str, RelationLabelMap] = {
    "relation-v1": RelationLabelMap(
        version="relation-v1",
        columns=("entailment", "neutral", "contradiction"),
        contradicts_min=0.93,
        restates_min=0.95,
        refines_min=0.95,
    ),
}


def label_map(version: str) -> RelationLabelMap:
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

    @property
    def unpinned_weights(self) -> bool:
        return self.weights_sha256 is None

    @property
    def instrument_id(self) -> str:
        return _digest(
            "instrument",
            self.model,
            self.revision,
            self.weights_sha256 or "unpinned",
            self.runtime,
            self.runtime_version,
            self.template_version,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
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
class Reading:
    reading_id: str
    question_type: str
    instrument: InstrumentIdentity
    inputs: tuple[InputUnit, ...]
    #: `{"ab": [...], "ba": [...]}` in input order, or None for an abstention
    #: the instrument decided before any vector existed.
    vectors: Mapping[str, Sequence[float]] | None
    verdict: Verdict
    sensed_at: str
    placement: str
    columns: tuple[str, ...] = field(default=("entailment", "neutral", "contradiction"))

    @property
    def hashes(self) -> tuple[str, ...]:
        return tuple(item.text_sha256 for item in self.inputs)

    @property
    def input_key(self) -> str:
        return input_key(self.question_type, self.hashes)

    def output(self) -> dict[str, Any]:
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


def rederive(reading_vectors: Mapping[str, Sequence[float]] | None, stored: Verdict, version: str) -> Verdict | None:
    """The verdict under the ACTIVE label map, from the stored vectors.

    A label-map change is a re-derivation, never a re-sense. An abstention the
    instrument decided without vectors stays that abstention.
    """
    if reading_vectors is None:
        return stored if stored.label == "abstain" else None
    return label_map(version).apply(reading_vectors.get("ab"), reading_vectors.get("ba"))


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
