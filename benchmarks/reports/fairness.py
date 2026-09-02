"""The fairness matrix: one disclosed row per lane × provider × variant.

The eight published fields are exactly the table of
``docs/benchmark-fairness-contract.md`` — :data:`FAIRNESS_MATRIX_FIELDS` pairs
each published label with the attribute that carries it, and the row model
declares them in that order and no other. Nothing here is optional: the spec
sentence is that every lane's entry *SHALL* record what this repository
authored, who authored each configuration value, the asymmetries with their
direction, the pins, and the blocked measurements. A field with a default would
let a row be published while saying nothing, which is the failure this matrix
exists to prevent.

Emptiness is the same failure wearing a different hat. A row with no pins and no
asymmetries reads as "nothing to declare" whether that was surveyed or merely
skipped, so each such field carries a companion in
:data:`EMPTY_DISCLOSURE_REASONS`: leave the field empty and you must say why, in
words, and you may not do both. "None declared" is a claim somebody makes.

The glue accounting is not restated either: it is :meth:`Projector.meta`'s own
published numbers, and the projector's module path must appear among the
disclosed files, so the LOC number is provably about a file the row discloses
rather than about something the reader cannot see.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from epistemic.snapshot import FieldDeclaration, ProjectorMeta, parse_evidence_citation
from protocol.models import LaneReadiness
from protocol.offline import offline_guard
from pydantic import BaseModel, ConfigDict, Field, model_validator

#: ``(published label, attribute)`` for the fairness-matrix row of
#: ``docs/benchmark-fairness-contract.md``. The labels are copied from the
#: document, not paraphrased, and :class:`FairnessRow` declares the attributes in
#: this order after its ``(lane, provider, variant)`` key.
FAIRNESS_MATRIX_FIELDS: tuple[tuple[str, str], ...] = (
    ("config source", "config_source"),
    ("config authored by", "config_authored_by"),
    ("exomem-authored glue", "exomem_authored_glue"),
    ("asymmetries", "asymmetries"),
    ("capability declarations", "capability_declarations"),
    ("readiness", "readiness"),
    ("pins", "pins"),
    ("blocked measurements", "blocked_measurements"),
)

#: ``(published field, companion reason)``. A published field that may legitimately
#: be empty must say so explicitly rather than defaulting into silence.
EMPTY_DISCLOSURE_REASONS: tuple[tuple[str, str], ...] = (
    ("config_source", "no_config_source_reason"),
    ("asymmetries", "no_asymmetries_reason"),
    ("pins", "no_pins_reason"),
    ("blocked_measurements", "no_blocked_measurements_reason"),
)

#: The three authorship values the contract publishes for a configuration value.
ConfigAuthor = Literal["competitor", "exomem", "shared-harness"]

#: ``(lane, provider, variant)`` — the key every comparative row is rendered at.
FairnessKey = tuple[str, str, str]


class FairnessMatrixError(ValueError):
    """The matrix cannot be published as it stands."""


class ReportModel(BaseModel):
    """Strict, frozen, ``extra='forbid'`` — a published row never mutates."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def require_relative_path(value: str, *, label: str) -> str:
    """Return ``value`` when it is a run-root-relative path, else raise.

    An absolute path in a published artifact leaks the machine that produced it,
    and a traversal escapes the run root a reviewer was handed.
    """

    text = value.strip()
    if not text:
        raise ValueError(f"{label} must be a relative path under the run root")
    if PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute():
        raise ValueError(f"{label} must be relative to the run root, not {text!r}")
    if ".." in PurePosixPath(text).parts:
        raise ValueError(f"{label} must be relative to the run root without traversal")
    return text


def _require_citation_or_url(value: str, *, label: str) -> str:
    """A provenance value is a ``path:line`` citation or a documentation URL."""

    text = value.strip()
    if text.startswith(("http://", "https://")):
        return text
    if parse_evidence_citation(text) is None:
        raise ValueError(f"{label} {value!r} is neither a path:line citation nor a URL")
    return text


class Asymmetry(ReportModel):
    """One known asymmetry, with the direction it favours.

    Reported, an asymmetry is a result; unreported, it is a defect — so the
    direction is required rather than inferred.
    """

    description: str = Field(min_length=1)
    favours: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class GlueAccounting(ReportModel):
    """What this repository authored for the row: files, LOC, endpoints.

    The numbers come from :meth:`Projector.meta`, and ``projector_module`` names
    the module they were measured over — which must be one of ``files``, so the
    published LOC is about disclosed code.
    """

    files: tuple[str, ...] = Field(min_length=1)
    projector: ProjectorMeta
    projector_module: str = Field(min_length=1)
    reviewer_disposition: str = Field(min_length=1)

    @model_validator(mode="after")
    def _paths_are_relative_and_the_module_is_disclosed(self) -> GlueAccounting:
        for path in self.files:
            require_relative_path(path, label="an exomem-authored glue file")
        require_relative_path(self.projector_module, label="the projector module")
        if self.projector_module not in self.files:
            raise ValueError(
                "the projector module whose LOC is published must appear in the "
                f"disclosed files: {self.projector_module!r} is not among {list(self.files)}"
            )
        return self

    @classmethod
    def for_projector(
        cls,
        projector,
        *,
        repo_root: Path | str,
        reviewer_disposition: str,
        files: Iterable[str] = (),
    ) -> GlueAccounting:
        """Derive the accounting from a real projector, so the two cannot disagree."""

        source = inspect.getsourcefile(type(projector))
        if source is None:  # pragma: no cover - a projector always has a module
            raise ValueError("the projector has no source module to account for")
        module = Path(source).resolve().relative_to(Path(repo_root).resolve()).as_posix()
        return cls(
            files=tuple(dict.fromkeys((module, *files))),
            projector=projector.meta(),
            projector_module=module,
            reviewer_disposition=reviewer_disposition,
        )


class CapabilityDeclarationCounts(ReportModel):
    """N/A, ``absent_by_design`` and ``unavailable`` counts for one row.

    ``not_applicable`` is deliberately not a synonym for ``absent_by_design``:
    a designed absence the product itself markets scores ``fail`` rather than
    N/A (``benchmarks/epistemic/assertions.py`` lines 427-441), so a row whose
    two counts differ is disclosing exactly that claim-conditioned gap.
    """

    not_applicable: int = Field(ge=0)
    absent_by_design: int = Field(ge=0)
    unavailable: int = Field(ge=0)

    @classmethod
    def from_declarations(
        cls, declarations: Iterable[FieldDeclaration]
    ) -> CapabilityDeclarationCounts:
        declared = tuple(declarations)
        by_design = [item for item in declared if item.status == "absent_by_design"]
        unavailable = [item for item in declared if item.status == "unavailable"]
        return cls(
            not_applicable=sum(1 for item in by_design if item.marketing_claim is None),
            absent_by_design=len(by_design),
            unavailable=len(unavailable),
        )


class BlockedMeasurement(ReportModel):
    """A measurement this row could not take, and the one command that unblocks it."""

    measurement: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    unblock_command: str = Field(min_length=1)


class FairnessRow(ReportModel):
    """One fairness-matrix entry, keyed by ``(lane, provider, variant)``."""

    lane: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    variant: str = Field(min_length=1)

    #: file:line / URL for every competitor-side setting.
    config_source: dict[str, str]
    #: competitor · exomem (disclosed) · shared harness.
    config_authored_by: dict[str, ConfigAuthor]
    #: file list + LOC + endpoints + reviewer disposition.
    exomem_authored_glue: GlueAccounting
    #: enumerated, each with direction (favours whom).
    asymmetries: tuple[Asymmetry, ...]
    #: N/A, absent_by_design, unavailable counts.
    capability_declarations: CapabilityDeclarationCounts
    #: verification method + evidence path (or readiness-unverifiable).
    readiness: tuple[LaneReadiness, ...] = Field(min_length=1)
    #: product version/commit/binary, dataset sha, model ids.
    pins: dict[str, str]
    #: with reasons and one-command unblock paths.
    blocked_measurements: tuple[BlockedMeasurement, ...]

    #: Companions for the fields above that may legitimately be empty. Exactly
    #: one of (entries, reason) is present for each.
    no_config_source_reason: str | None = None
    no_asymmetries_reason: str | None = None
    no_pins_reason: str | None = None
    no_blocked_measurements_reason: str | None = None

    @property
    def key(self) -> FairnessKey:
        return (self.lane, self.provider, self.variant)

    def disclosure_for(self, attribute: str) -> object:
        """The entries, or the stated reason there are none."""

        content = getattr(self, attribute)
        if content:
            return content
        for field, reason_field in EMPTY_DISCLOSURE_REASONS:
            if field == attribute:
                return f"none declared: {getattr(self, reason_field)}"
        return content

    @model_validator(mode="after")
    def _provenance_is_complete(self) -> FairnessRow:
        for setting, source in self.config_source.items():
            _require_citation_or_url(source, label=f"config source for {setting!r}")
        if set(self.config_source) != set(self.config_authored_by):
            raise ValueError(
                "config authorship must name exactly the sourced settings: "
                f"sourced {sorted(self.config_source)}, "
                f"authored {sorted(self.config_authored_by)}"
            )
        return self

    @model_validator(mode="after")
    def _emptiness_is_stated_not_defaulted(self) -> FairnessRow:
        for field, reason_field in EMPTY_DISCLOSURE_REASONS:
            content = getattr(self, field)
            reason = getattr(self, reason_field)
            if not content and not (reason and reason.strip()):
                raise ValueError(
                    f"{field} is empty, so {reason_field} must state why — "
                    "'none declared' is a claim, not a silence"
                )
            if content and reason is not None:
                raise ValueError(
                    f"{field} cannot both carry entries and state {reason_field}"
                )
        return self


def require_complete(
    rows: Iterable[FairnessRow], expected_keys: Iterable[FairnessKey]
) -> None:
    """Refuse publication while any expected ``(lane, provider, variant)`` has no row."""

    present = {row.key for row in rows}
    missing = sorted(set(expected_keys) - present)
    if missing:
        named = "; ".join(" / ".join(key) for key in missing)
        raise FairnessMatrixError(f"missing fairness entry blocks publication: {named}")


def _cell(value: object) -> str:
    """One matrix cell, flattened to a single pipe-safe line."""

    if isinstance(value, GlueAccounting):
        meta = value.projector
        text = (
            f"{', '.join(value.files)} · {meta.loc} LOC ({meta.loc_code} code) · "
            f"endpoints {', '.join(meta.endpoints_used) or 'none'} · "
            f"reviewer: {value.reviewer_disposition}"
        )
    elif isinstance(value, CapabilityDeclarationCounts):
        text = (
            f"N/A {value.not_applicable}, absent_by_design {value.absent_by_design}, "
            f"unavailable {value.unavailable}"
        )
    elif isinstance(value, dict):
        text = "; ".join(f"{name}={item}" for name, item in sorted(value.items())) or "none"
    elif isinstance(value, tuple):
        text = "; ".join(_item(item) for item in value) or "none"
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _item(value: object) -> str:
    if isinstance(value, Asymmetry):
        return f"{value.description} (favours {value.favours}; {value.evidence})"
    if isinstance(value, BlockedMeasurement):
        return f"{value.measurement}: {value.reason} → {value.unblock_command}"
    if isinstance(value, LaneReadiness):
        verdict = "verified" if value.verified else "unverified"
        return f"{value.lane}: {value.method} {verdict} ({value.evidence})"
    return str(value)


def render_fairness_matrix(
    rows: Iterable[FairnessRow], *, expected_keys: Iterable[FairnessKey]
) -> str:
    """Render the matrix as markdown — no aggregate, one row per key.

    Rendering is where the spec puts the refusal (:22-24): a comparative row
    without its fairness entry is not marked publishable, so completeness is
    checked here rather than only in a helper a caller may forget.
    """

    with offline_guard():
        ordered = sorted(rows, key=lambda row: row.key)
        require_complete(ordered, expected_keys)
        labels = [label for label, _ in FAIRNESS_MATRIX_FIELDS]
        header = "| " + " | ".join(["lane", "provider", "variant", *labels]) + " |"
        rule = "|" + "|".join(["---"] * (3 + len(labels))) + "|"
        lines = [header, rule]
        for row in ordered:
            cells = [
                _cell(row.disclosure_for(attribute))
                for _, attribute in FAIRNESS_MATRIX_FIELDS
            ]
            lines.append("| " + " | ".join([row.lane, row.provider, row.variant, *cells]) + " |")
        return "\n".join(lines) + "\n"


__all__ = [
    "EMPTY_DISCLOSURE_REASONS",
    "FAIRNESS_MATRIX_FIELDS",
    "Asymmetry",
    "BlockedMeasurement",
    "CapabilityDeclarationCounts",
    "ConfigAuthor",
    "FairnessKey",
    "FairnessMatrixError",
    "FairnessRow",
    "GlueAccounting",
    "ReportModel",
    "render_fairness_matrix",
    "require_complete",
    "require_relative_path",
]
