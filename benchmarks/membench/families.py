"""Scenario-family registry with oracle-ability classification.

Every scenario family is classified as ``deterministic-oracle`` (expected
records fully computable at generation time), ``rubric-track`` (writable
knowledge whose quality is only human/blind-judge assessable, routed to
predeclared rubrics), or ``out-of-scope`` (not digitally writable — the
declared boundary of the benchmark). A template may only register under an
**active** family; ``planned`` families refuse templates until flipped
active, and out-of-scope families are permanently ``excluded``.

The registry is data, not behaviour: pure stdlib, no membench imports, no
side effects. Generation-time enforcement lives in
:func:`family_registration_error` (called from ``membench.generate``), and
the published coverage table in ``docs/memory-proof-benchmark.md`` is the
verbatim output of :func:`coverage_table_markdown` (no-drift gate in
``tests/test_membench_families.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

Classification = Literal["deterministic-oracle", "rubric-track", "out-of-scope"]
Status = Literal["active", "planned", "excluded"]


@dataclass(frozen=True)
class Family:
    """One registry entry: id, oracle-ability classification, lifecycle status,
    and the stated rationale that makes the coverage claim auditable."""

    family_id: str
    classification: Classification
    status: Status
    rationale: str


# v0.1 families (all deterministic-oracle, active): the ids are exactly the
# strings the 17 shipped templates pass as ``Template.family``.
_V01_FAMILIES: tuple[Family, ...] = (
    Family(
        family_id="temporal",
        classification="deterministic-oracle",
        status="active",
        rationale=(
            "Bitemporal ground truth (event vs ingestion time, supersession, "
            "expiry, as-of views) is fully computable from the seeded claim "
            "timeline."
        ),
    ),
    Family(
        family_id="epistemics",
        classification="deterministic-oracle",
        status="active",
        rationale=(
            "Authority, dispute, tentative lifecycle, retraction, provenance, "
            "and absence-vs-unsupported states are derived by the oracle from "
            "recorded assertions."
        ),
    ),
    Family(
        family_id="query_behavior",
        classification="deterministic-oracle",
        status="active",
        rationale=(
            "Recall, abstention, and clarification behaviour is checked "
            "against oracle-derived expected records for every query kind."
        ),
    ),
    Family(
        family_id="maintenance",
        classification="deterministic-oracle",
        status="active",
        rationale=(
            "Duplicate, contradiction, stale, and orphan pressure is injected "
            "by the generation schedule, so corpus-health ground truth is "
            "computable."
        ),
    ),
    Family(
        family_id="identity",
        classification="deterministic-oracle",
        status="active",
        rationale=(
            "Alias and entity-graph resolution targets are declared at "
            "generation time; the oracle knows every coreference."
        ),
    ),
    Family(
        family_id="multimodal",
        classification="deterministic-oracle",
        status="active",
        rationale=(
            "Numeric and table/PDF evidence carries generated sentinel "
            "values; retrieval and answer identity are exact-checkable."
        ),
    ),
    Family(
        family_id="governance",
        classification="deterministic-oracle",
        status="active",
        rationale=(
            "Audience policy and disclosure expectations come from the "
            "generated PolicySet; leak vs no-leak is deterministic."
        ),
    ),
)

# v0.2 families (planned — declared by the expand-memory-proof-benchmark
# change; each refuses templates until its implementation flips it active).
_V02_FAMILIES: tuple[Family, ...] = (
    Family(
        family_id="procedural",
        classification="deterministic-oracle",
        status="planned",
        rationale=(
            "Ordered how-to chains where step order, preconditions, and "
            "revisions over time are the ground truth, computable from the "
            "authored chain."
        ),
    ),
    Family(
        family_id="quantitative",
        classification="deterministic-oracle",
        status="planned",
        rationale=(
            "Arithmetic over two or more stored values; the oracle computes "
            "the expected value, unit, and tolerance with both contributing "
            "sources as required citations."
        ),
    ),
    Family(
        family_id="negation_counterfactual",
        classification="deterministic-oracle",
        status="planned",
        rationale=(
            "Recorded-as-false vs not-recorded and considered-then-rejected "
            "plans score against the existing abstention and current-state "
            "gates."
        ),
    ),
    Family(
        family_id="cross_lingual",
        classification="deterministic-oracle",
        status="planned",
        rationale=(
            "Synthetic non-Latin-script sources queried in English; sentinel "
            "citation and value identity are exact-checkable, and profiles "
            "declaring no support report unsupported, never zero."
        ),
    ),
    Family(
        family_id="preference_attribution",
        classification="deterministic-oracle",
        status="planned",
        rationale=(
            "Holder and as-of time of an opinion are the ground truth; an "
            "unattributed restatement as objective fact fails the calibration "
            "gate."
        ),
    ),
    Family(
        family_id="source_reliability",
        classification="deterministic-oracle",
        status="planned",
        rationale=(
            "A recurring source's correction track record is derivable from "
            "the corpus; weighting is scored behaviourally via required "
            "citations and hedging expectations, never numeric confidence."
        ),
    ),
    Family(
        family_id="long_horizon_entropy",
        classification="deterministic-oracle",
        status="planned",
        rationale=(
            "A 52-week ingestion schedule with recurring duplication, "
            "correction, and deletion pressure; health metrics at quarterly "
            "snapshots are computable from the schedule."
        ),
    ),
    Family(
        family_id="multimodal_depth",
        classification="deterministic-oracle",
        status="planned",
        rationale=(
            "Facts existing only inside real PDF, OCR-image, or "
            "audio-transcript artifacts; sentinel retrieval under the media "
            "profile, degrading with recorded reasons without the extras."
        ),
    ),
)

# Declared boundary of the benchmark: not digitally writable, so never
# seedable, retrievable, or scorable. Permanently excluded (never active).
_OUT_OF_SCOPE_FAMILIES: tuple[Family, ...] = (
    Family(
        family_id="tacit_polanyi",
        classification="out-of-scope",
        status="excluded",
        rationale=(
            "Tacit knowledge (the Polanyi boundary): skills and know-how that "
            "cannot be written down digitally cannot be seeded into a corpus "
            "or checked by any oracle or rubric; declared here so the "
            "coverage claim states its own limit."
        ),
    ),
)

FAMILIES: tuple[Family, ...] = _V01_FAMILIES + _V02_FAMILIES + _OUT_OF_SCOPE_FAMILIES


def _validate(entries: tuple[Family, ...]) -> tuple[Family, ...]:
    seen: set[str] = set()
    for entry in entries:
        if not entry.family_id.strip():
            raise ValueError("family_id is required")
        if entry.classification not in get_args(Classification):
            raise ValueError(
                f"family {entry.family_id!r}: classification "
                f"{entry.classification!r} is not in the taxonomy"
            )
        if entry.status not in get_args(Status):
            raise ValueError(
                f"family {entry.family_id!r}: status {entry.status!r} is not in "
                "the taxonomy"
            )
        if entry.family_id in seen:
            raise ValueError(f"duplicate family id {entry.family_id!r}")
        seen.add(entry.family_id)
        if (entry.classification == "out-of-scope") != (entry.status == "excluded"):
            raise ValueError(
                f"family {entry.family_id!r}: out-of-scope classification and "
                "excluded status must coincide"
            )
        if not entry.rationale.strip():
            raise ValueError(f"family {entry.family_id!r}: rationale is required")
    return entries


_validate(FAMILIES)


def registry() -> dict[str, Family]:
    """The registry keyed by family id (copy; declaration order preserved)."""

    return {entry.family_id: entry for entry in FAMILIES}


def active_family_ids() -> frozenset[str]:
    return frozenset(e.family_id for e in FAMILIES if e.status == "active")


def family_registration_error(template_id: str, family: str) -> str | None:
    """Generation-time enforcement: why this template's family is refused.

    Returns ``None`` when the family is an active registry entry, otherwise a
    message naming the template and the family (raised as ``GenerationError``
    by the generator).
    """

    entry = registry().get(family)
    if entry is None:
        return (
            f"template {template_id!r} names unregistered family {family!r}; "
            "add it to the scenario-family registry (membench.families) with "
            "a classification and rationale before generating"
        )
    if entry.status != "active":
        return (
            f"template {template_id!r} names family {family!r}, which is "
            f"{entry.status} in the scenario-family registry; only active "
            "families accept templates"
        )
    return None


def coverage_table_markdown() -> str:
    """The published coverage table, verbatim as it appears in
    ``docs/memory-proof-benchmark.md`` (the no-drift gate compares them)."""

    lines = [
        "| Family | Classification | Status | Rationale |",
        "|---|---|---|---|",
    ]
    for entry in FAMILIES:
        lines.append(
            f"| `{entry.family_id}` | {entry.classification} | {entry.status} "
            f"| {entry.rationale} |"
        )
    return "\n".join(lines)
