"""The compat matrix: what a row declared against what the run observed.

The declared side is the scenario's own fairness packet — the closed
``driver_surface_id × provider × variant`` matrix and its dispositions, plus the
projector's field-declaration statuses and the registered variant vocabulary of
the programme design. The observed side is the run: the variant the runner bound
and wrote into the manifest, the provider that variant belongs to, the readiness
the manifest recorded, and the manifest's own terminal status.

Every declared≠observed cell is a first-class :class:`Divergence`. Two kinds are
*validity* failures rather than findings — presenting a result under a variant
identity, or under a provider, that differs from the manifest's registered one —
and so is a run the protocol did not mark VALID: an environment fault invalidates
the affected rows instead of scoring against the product. A declared-equivalent
surface whose readiness never verified is a finding a reviewer must read, but it
does not by itself re-label the run.

Building from a non-VALID manifest stays allowed, because a diagnostic read of a
failed run is exactly what a reviewer needs; the rendered matrix carries the
status and its reason so it can never be mistaken for a publishable result.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from epistemic.schema import FairnessPacket
from epistemic.snapshot import FieldDeclaration
from protocol.models import ManifestStatus, RunManifest
from protocol.offline import offline_guard
from pydantic import Field

from .fairness import ReportModel, require_relative_path

#: The registered provider variants of the programme design, grouped by the
#: provider each belongs to. Hosted and local variants of one product are
#: separate identities and never collapse into one row; the names are the
#: design's own and no new one is minted here.
VARIANTS_BY_PROVIDER: dict[str, frozenset[str]] = {
    "exomem": frozenset({"exomem-source-only", "exomem-controlled", "exomem-native"}),
    "basic-memory": frozenset(
        {"basic-memory-controlled", "basic-memory-native-git", "basic-memory-native-nogit"}
    ),
    "supermemory": frozenset(
        {
            "supermemory-hosted-memorybench",
            "supermemory-hosted-native",
            "supermemory-local-controlled",
            "supermemory-local-native",
            "supermemory-local-documents-v3",
        }
    ),
    "hybrid-rag-control": frozenset({"hybrid-rag-control"}),
    "grep-markdown": frozenset({"grep-markdown"}),
    "no-memory": frozenset({"no-memory"}),
}

#: Every registered variant identity, flattened.
VARIANT_VOCABULARY: frozenset[str] = frozenset(
    name for names in VARIANTS_BY_PROVIDER.values() for name in names
)

#: ``(provider, variant, driver_surface_id)`` — one declared audit cell.
CompatKey = tuple[str, str, str]

DivergenceKind = Literal["variant", "provider", "capability"]

#: Divergences that make a result invalid rather than merely notable.
INVALIDATING_KINDS: frozenset[str] = frozenset({"variant", "provider"})

#: Rendered when the observed variant is not a registered identity, so the
#: provider behind it cannot be derived and the comparison cannot be made.
PROVIDER_COMPARISON_IMPOSSIBLE = "provider comparison not possible"


class CompatMatrixError(ValueError):
    """The run cannot be compared against its declarations as it stands."""


def provider_of_variant(variant: str) -> str | None:
    """The provider a registered variant belongs to, or ``None`` if unregistered."""

    for provider, names in VARIANTS_BY_PROVIDER.items():
        if variant in names:
            return provider
    return None


class Divergence(ReportModel):
    """One declared≠observed cell, with the artifact a reviewer opens."""

    kind: DivergenceKind
    key: CompatKey
    declared: str = Field(min_length=1)
    observed: str = Field(min_length=1)
    evidence_path: str = Field(min_length=1)


class CompatCell(ReportModel):
    """One declared surface, carrying what the run actually showed."""

    provider: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    driver_surface_id: str = Field(min_length=1)
    declared_disposition: Literal["equivalent", "capability_gap"]
    #: Whether the declared identity is one of :data:`VARIANT_VOCABULARY`.
    declared_variant_registered: bool
    observed_variant: str = Field(min_length=1)
    #: Whether the *observed* identity is registered — spec :61-66 asks for the
    #: registered identity in every artifact, which includes this side.
    observed_variant_registered: bool
    #: The provider the observed variant belongs to; ``None`` when the observed
    #: identity is unregistered and the provider therefore cannot be derived.
    observed_provider: str | None = None
    observed_readiness_verified: bool
    evidence_path: str = Field(min_length=1)

    @property
    def key(self) -> CompatKey:
        return (self.provider, self.variant, self.driver_surface_id)


class CompatMatrix(ReportModel):
    run_id: str = Field(min_length=1)
    observed_variant: str = Field(min_length=1)
    #: The manifest's terminal status. A run the protocol did not mark VALID
    #: never yields a publishable comparative row (spec :26-30, :37-40).
    run_status: ManifestStatus
    invalid_reason: str | None = None
    #: The projector's declaration statuses, sorted and de-duplicated.
    declared_field_statuses: tuple[str, ...]
    cells: tuple[CompatCell, ...] = Field(min_length=1)
    divergences: tuple[Divergence, ...]

    def invalidates(self) -> bool:
        """True when this matrix may not be read as a comparable result."""

        if self.run_status != "VALID":
            return True
        return any(item.kind in INVALIDATING_KINDS for item in self.divergences)


def build_compat_matrix(
    *,
    packet: FairnessPacket,
    manifest: RunManifest,
    declarations: Iterable[FieldDeclaration],
    manifest_ref: str = "manifest.json",
) -> CompatMatrix:
    """Compare one scenario's declarations against one run's manifest."""

    with offline_guard():
        observed = manifest.provider_variant
        if not observed:
            raise CompatMatrixError(
                "compat requires the observed variant the runner bound into the manifest; "
                f"run {manifest.run_id} recorded none"
            )
        if not packet.privileged_endpoint_matrix:
            raise CompatMatrixError(
                "compat requires a non-empty privileged endpoint matrix; the fairness "
                f"packet for run {manifest.run_id} declares no audited surface"
            )
        evidence_path = require_relative_path(manifest_ref, label="the compat evidence path")

        observed_registered = observed in VARIANT_VOCABULARY
        observed_provider = provider_of_variant(observed)

        lanes = [lane for lane in manifest.readiness if lane.lane == observed]
        unverified = [lane for lane in lanes if not lane.verified]
        readiness_verified = bool(lanes) and not unverified
        readiness_observed = (
            "readiness verified"
            if readiness_verified
            else "readiness unverified: "
            + (
                "; ".join(lane.evidence for lane in unverified)
                if unverified
                else f"no readiness lane recorded for {observed}"
            )
        )

        cells: list[CompatCell] = []
        divergences: list[Divergence] = []
        for entry in packet.privileged_endpoint_matrix:
            cell = CompatCell(
                provider=entry.provider,
                variant=entry.variant,
                driver_surface_id=entry.driver_surface_id,
                declared_disposition=entry.disposition,
                declared_variant_registered=entry.variant in VARIANT_VOCABULARY,
                observed_variant=observed,
                observed_variant_registered=observed_registered,
                observed_provider=observed_provider,
                observed_readiness_verified=readiness_verified,
                evidence_path=evidence_path,
            )
            cells.append(cell)
            if entry.variant != observed:
                divergences.append(
                    Divergence(
                        kind="variant",
                        key=cell.key,
                        declared=entry.variant,
                        observed=observed,
                        evidence_path=evidence_path,
                    )
                )
            if observed_provider is not None and entry.provider != observed_provider:
                divergences.append(
                    Divergence(
                        kind="provider",
                        key=cell.key,
                        declared=entry.provider,
                        observed=observed_provider,
                        evidence_path=evidence_path,
                    )
                )
            if entry.disposition == "equivalent" and not readiness_verified:
                divergences.append(
                    Divergence(
                        kind="capability",
                        key=cell.key,
                        declared="equivalent",
                        observed=readiness_observed,
                        evidence_path=evidence_path,
                    )
                )

        return CompatMatrix(
            run_id=manifest.run_id,
            observed_variant=observed,
            run_status=manifest.status,
            invalid_reason=manifest.invalid_reason,
            declared_field_statuses=tuple(
                sorted({declaration.status for declaration in declarations})
            ),
            cells=tuple(sorted(cells, key=lambda cell: cell.key)),
            divergences=tuple(sorted(divergences, key=lambda item: (item.kind, item.key))),
        )


def render_compat_matrix(matrix: CompatMatrix) -> str:
    """Render the matrix as markdown, led by the status that governs its use."""

    with offline_guard():
        verdict = "INVALID — not a publishable comparative result" if matrix.invalidates() else "comparable"
        lines = [
            f"# Compat matrix — run {matrix.run_id}",
            "",
            f"- run status: **{matrix.run_status}**",
            f"- disposition: **{verdict}**",
        ]
        if matrix.invalid_reason:
            lines.append(f"- invalid reason: {matrix.invalid_reason}")
        lines.append(f"- observed variant: `{matrix.observed_variant}`")
        if matrix.cells and matrix.cells[0].observed_provider is None:
            lines.append(
                f"- observed provider: unregistered variant identity — "
                f"{PROVIDER_COMPARISON_IMPOSSIBLE}"
            )
        else:
            lines.append(f"- observed provider: `{matrix.cells[0].observed_provider}`")
        lines.append(f"- declared field statuses: {', '.join(matrix.declared_field_statuses)}")

        lines += [
            "",
            "| provider | variant | surface | disposition | declared registered | "
            "observed registered | readiness |",
            "|---|---|---|---|---|---|---|",
        ]
        for cell in matrix.cells:
            lines.append(
                f"| {cell.provider} | {cell.variant} | {cell.driver_surface_id} | "
                f"{cell.declared_disposition} | {cell.declared_variant_registered} | "
                f"{cell.observed_variant_registered} | "
                f"{'verified' if cell.observed_readiness_verified else 'unverified'} |"
            )

        lines += ["", "## Divergences", ""]
        if matrix.divergences:
            lines += [
                f"- [{item.kind}] {' / '.join(item.key)}: declared {item.declared!r}, "
                f"observed {item.observed!r} (`{item.evidence_path}`)"
                for item in matrix.divergences
            ]
        else:
            lines.append("- none")
        return "\n".join(lines) + "\n"


__all__ = [
    "INVALIDATING_KINDS",
    "PROVIDER_COMPARISON_IMPOSSIBLE",
    "VARIANTS_BY_PROVIDER",
    "VARIANT_VOCABULARY",
    "CompatCell",
    "CompatKey",
    "CompatMatrix",
    "CompatMatrixError",
    "Divergence",
    "DivergenceKind",
    "build_compat_matrix",
    "provider_of_variant",
    "render_compat_matrix",
]
