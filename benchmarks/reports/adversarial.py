"""The adversarial packet handed to a reviewer with no stake in the outcome.

Nothing in the packet is a claim the generator makes on its own behalf. The
pre-registration binding is derived from the ratification and amendment
receipts, so it is the same identity every run manifest carries. The assumptions
and confounds are the projector's own field declarations split by whether the
field is observable at all, plus the two arguments the scenario's fairness packet
already had to make — why the invariant is product-neutral, and what the public
suites already cover. The suspicious-win flags are read off a validated cohort
that the cohort validator has already normalized: a product row keeps its
five-valued outcome and loses product-signal eligibility whenever a control
reproduced the same assertion instance, and it is exactly those masked wins that
a reviewer is asked to attack.

A packet with no recorded review disposition renders as ``internal-diagnostic``.
That is the whole point of the artifact: a comparative claim is not publishable
until somebody who did not produce it has tried to break it.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from epistemic.cohort import CONTROL_PROVIDERS, ValidatedEpistemicCohort
from epistemic.schema import FairnessPacket
from epistemic.snapshot import FieldDeclaration
from protocol.contracts import derive_preregistration_identity
from protocol.offline import offline_guard
from pydantic import Field, field_validator

from .fairness import ReportModel, require_relative_path

#: The one accepted shape of a reviewer handle. This is an *allowlist of form*,
#: not a name detector — nothing in this repository can tell whether a string is
#: somebody's name. The repository privacy gate
#: (``src/exomem/public_artifact_privacy.py``) finds absolute paths and Windows
#: SIDs and nothing else, and the handle renders verbatim into the packet
#: markdown, so a filter that merely rejected spaces would still pass
#: ``hugo.kivi`` and ``hugoander@gmail.com``. Requiring the literal
#: ``independent-reviewer:`` prefix and a lowercase ``[a-z0-9-]`` id after it
#: leaves no room for a dotted name, an underscore, or an email address.
_ALLOWED_REVIEWER_ID = re.compile(r"^independent-reviewer:[a-z0-9][a-z0-9-]*$")

#: The label an unreviewed comparative artifact renders under. A document
#: finalized without a recorded adversarial review disposition is a diagnostic,
#: not a publication.
INTERNAL_DIAGNOSTIC = "internal-diagnostic"


class AdversarialPacketError(ValueError):
    """The packet cannot be built as asked."""


class ReviewerChallenge(ReportModel):
    """One reviewer-checklist question, verbatim from the fairness contract."""

    challenge_id: str = Field(min_length=1)
    question: str = Field(min_length=1)


#: The reviewer checklist of ``docs/benchmark-fairness-contract.md``, in document
#: order and copied verbatim. The ids are stable handles for binding each
#: question to the artifact a reviewer opens to answer it.
REVIEWER_CHALLENGES: tuple[ReviewerChallenge, ...] = (
    ReviewerChallenge(
        challenge_id="config-provenance",
        question="Does any competitor knob lack provenance? (automatic disqualification)",
    ),
    ReviewerChallenge(
        challenge_id="equivalence-gate-diffs",
        question=(
            "Do the equivalence-gate diffs contain unexplained mismatches or expired "
            "exceptions?"
        ),
    ),
    ReviewerChallenge(
        challenge_id="glue-size-asymmetry",
        question="Are Exomem's projector/driver LOC materially larger than competitors'?",
    ),
    ReviewerChallenge(
        challenge_id="environment-fault-scoring",
        question="Did any row score a product where the manifest shows an environment fault?",
    ),
    ReviewerChallenge(
        challenge_id="not-applicable-family",
        question="Does any comparative claim rest on a family containing an N/A?",
    ),
    ReviewerChallenge(
        challenge_id="privileged-call-receipt",
        question=(
            "Does every privileged call have an authenticated sandbox receipt and an "
            "exact matrix entry, with capability gaps excluded rather than scored?"
        ),
    ),
    ReviewerChallenge(
        challenge_id="cohort-replay",
        question=(
            "Does every product/control cell replay from provider-bound evidence, and do "
            "control passes disappear from every product-signal gate count?"
        ),
    ),
    ReviewerChallenge(
        challenge_id="judge-blinding",
        question=(
            "Is any judged number published without the structural-blinding fix and a "
            "judge–human agreement measurement?"
        ),
    ),
    ReviewerChallenge(
        challenge_id="cost-envelope",
        question="Does any cost claim include unmetered server-side extraction?",
    ),
    ReviewerChallenge(
        challenge_id="latency-host",
        question="Is any latency comparison rendered from the known-unvalidated host?",
    ),
)


class AmendmentBinding(ReportModel):
    """One ordered amendment receipt, as the folded chain records it."""

    sequence: int = Field(ge=1)
    receipt_path: str = Field(min_length=1)
    receipt_sha256: str = Field(min_length=1)
    parent_contract_sha256: str = Field(min_length=1)
    contract_sha256: str = Field(min_length=1)
    acknowledgment_status: str = Field(min_length=1)


class PreregistrationBinding(ReportModel):
    """The pre-registration hash and amendment chain the run executed against."""

    contract_revision: str = Field(min_length=1)
    path: str = Field(min_length=1)
    #: The ratified document, before any amendment.
    base_sha256: str = Field(min_length=1)
    #: The effective document the ordered amendment chain culminates in.
    sha256: str = Field(min_length=1)
    amendments: tuple[AmendmentBinding, ...]


class Assumption(ReportModel):
    statement: str = Field(min_length=1)
    status: str = Field(min_length=1)
    mechanism: str | None = None
    evidence: str = Field(min_length=1)


class Confound(ReportModel):
    statement: str = Field(min_length=1)
    status: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class SuspiciousWinFlag(ReportModel):
    """A product win on an assertion instance a control also scored."""

    provider: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    assertion: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    signal_disposition: str = Field(min_length=1)
    controls_scoring: tuple[str, ...] = Field(min_length=1)


class ChallengePath(ReportModel):
    """One reviewer question bound to the artifact that answers it."""

    challenge_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    artifact_path: str = Field(min_length=1)


class Objection(ReportModel):
    """One material objection, and what became of it.

    The spec allows exactly two outcomes: an objection is either fixed or
    documented with the publication. There is no third disposition, and no way
    to record a review that simply ignored one.
    """

    text: str = Field(min_length=1)
    status: Literal["fixed", "documented"]


class ReviewDisposition(ReportModel):
    """Evidence that an independent reviewer examined *these* packet bytes.

    ``packet_sha256`` binds the disposition to the content it reviewed, computed
    over the packet with this disposition removed. A packet whose content moved
    after the review therefore renders as unreviewed: a stale review is no
    review, and that is the whole reason the hash is here rather than a bare
    "reviewed" flag that any later edit would silently inherit.
    """

    #: An ``independent-reviewer:<lane-or-run-id>`` handle, never a person.
    reviewer_id: str = Field(min_length=1)
    reviewed_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    packet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    objections: tuple[Objection, ...]

    @field_validator("reviewer_id")
    @classmethod
    def _reviewer_id_has_the_allowed_form(cls, value: str) -> str:
        if _ALLOWED_REVIEWER_ID.fullmatch(value) is None:
            raise ValueError(
                "reviewer_id must match the allowed handle form "
                "'independent-reviewer:<lane-or-run-id>', where the id is "
                "lowercase [a-z0-9-]; this is an allowlist of form, because "
                "nothing downstream can recognise a personal name"
            )
        return value

    @field_validator("reviewed_at")
    @classmethod
    def _reviewed_at_is_a_real_date(cls, value: str) -> str:
        try:
            datetime.date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"reviewed_at must be a real calendar date: {value!r}") from error
        return value


class AdversarialPacket(ReportModel):
    run_id: str = Field(min_length=1)
    preregistration: PreregistrationBinding
    assumptions: tuple[Assumption, ...]
    confounds: tuple[Confound, ...]
    suspicious_win_flags: tuple[SuspiciousWinFlag, ...]
    challenge_paths: tuple[ChallengePath, ...] = Field(min_length=1)
    review_disposition: ReviewDisposition | None = None


def packet_content_sha256(packet: AdversarialPacket) -> str:
    """The packet's content hash, excluding its own review disposition.

    Excluding the disposition is what makes the binding usable at all: a
    reviewer hashes what they read, then attaches the disposition carrying that
    hash, and attaching it must not change the answer.
    """

    payload = packet.model_dump(mode="json")
    payload.pop("review_disposition", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _preregistration_binding(repo_root: Path | str) -> PreregistrationBinding:
    identity = derive_preregistration_identity(repo_root)
    return PreregistrationBinding(
        contract_revision=identity.contract_revision,
        path=identity.original.path,
        base_sha256=identity.original.sha256,
        sha256=identity.effective.sha256,
        amendments=tuple(
            AmendmentBinding(
                sequence=amendment.sequence,
                receipt_path=amendment.receipt.receipt_path,
                receipt_sha256=amendment.receipt.receipt_sha256,
                parent_contract_sha256=amendment.parent_contract_sha256,
                contract_sha256=amendment.contract.sha256,
                acknowledgment_status=amendment.acknowledgment_status,
            )
            for amendment in identity.amendments
        ),
    )


def _assumptions(
    declarations: Iterable[FieldDeclaration], packet: FairnessPacket
) -> tuple[Assumption, ...]:
    """Every observable field is an assumption the comparison rests on."""

    found = [
        Assumption(
            statement=f"{declaration.field!r} is observable for this row",
            status=declaration.status,
            mechanism=declaration.mechanism,
            evidence=declaration.evidence,
        )
        for declaration in declarations
        if declaration.observable
    ]
    found.append(
        Assumption(
            statement=packet.why_neutral.strip(),
            status="fairness-packet",
            evidence="fairness packet: why_neutral",
        )
    )
    return tuple(found)


def _confounds(
    declarations: Iterable[FieldDeclaration], packet: FairnessPacket
) -> tuple[Confound, ...]:
    """Every field that cannot be evaluated confounds the comparison."""

    found = [
        Confound(
            statement=f"{declaration.field!r} cannot be evaluated ({declaration.status})",
            status=declaration.status,
            evidence=declaration.evidence,
        )
        for declaration in declarations
        if not declaration.observable
    ]
    found.append(
        Confound(
            statement=packet.public_coverage_subtraction.strip(),
            status="fairness-packet",
            evidence="fairness packet: public_coverage_subtraction",
        )
    )
    return tuple(found)


def _identity_key(cell) -> tuple:
    return tuple(cell.identity.model_dump(mode="json").items())


def _suspicious_win_flags(cohort: ValidatedEpistemicCohort) -> tuple[SuspiciousWinFlag, ...]:
    """Product passes the controls reproduced, in cohort order."""

    control_passes: dict[tuple, list[str]] = {}
    for row in cohort.rows:
        if row.provider not in CONTROL_PROVIDERS:
            continue
        for cell in row.assertions:
            if cell.result.outcome == "pass":
                control_passes.setdefault(_identity_key(cell), []).append(row.provider)

    flags: list[SuspiciousWinFlag] = []
    for row in cohort.rows:
        if row.provider in CONTROL_PROVIDERS:
            continue
        for cell in row.assertions:
            scoring = control_passes.get(_identity_key(cell))
            if cell.result.outcome != "pass" or not scoring:
                continue
            flags.append(
                SuspiciousWinFlag(
                    provider=row.provider,
                    variant=row.variant,
                    scenario_id=cell.identity.scenario_id,
                    assertion=cell.identity.assertion,
                    outcome=cell.result.outcome,
                    signal_disposition=cell.signal_disposition,
                    controls_scoring=tuple(
                        name for name in CONTROL_PROVIDERS if name in scoring
                    ),
                )
            )
    return tuple(flags)


def _challenge_paths(artifacts: Mapping[str, str]) -> tuple[ChallengePath, ...]:
    missing = [
        challenge.challenge_id
        for challenge in REVIEWER_CHALLENGES
        if not artifacts.get(challenge.challenge_id)
    ]
    if missing:
        raise AdversarialPacketError(
            "every reviewer challenge needs the artifact that answers it; "
            f"unbound: {', '.join(missing)}"
        )
    bound: list[ChallengePath] = []
    for challenge in REVIEWER_CHALLENGES:
        raw = artifacts[challenge.challenge_id]
        try:
            path = require_relative_path(
                raw, label=f"the {challenge.challenge_id} challenge artifact"
            )
        except ValueError as error:
            raise AdversarialPacketError(str(error)) from error
        bound.append(
            ChallengePath(
                challenge_id=challenge.challenge_id,
                question=challenge.question,
                artifact_path=path,
            )
        )
    return tuple(bound)


def build_adversarial_packet(
    *,
    run_id: str,
    repo_root: Path | str,
    fairness: FairnessPacket,
    declarations: Iterable[FieldDeclaration],
    cohort: ValidatedEpistemicCohort,
    challenge_artifacts: Mapping[str, str],
    review_disposition: ReviewDisposition | None = None,
) -> AdversarialPacket:
    """Assemble the packet an independent reviewer is given, from artifacts alone."""

    with offline_guard():
        declared = tuple(declarations)
        return AdversarialPacket(
            run_id=run_id,
            preregistration=_preregistration_binding(repo_root),
            assumptions=_assumptions(declared, fairness),
            confounds=_confounds(declared, fairness),
            suspicious_win_flags=_suspicious_win_flags(cohort),
            challenge_paths=_challenge_paths(challenge_artifacts),
            review_disposition=review_disposition,
        )


def render_adversarial_packet(packet: AdversarialPacket) -> str:
    """Render the packet as markdown, labelled by its review disposition."""

    with offline_guard():
        lines: list[str] = [f"# Adversarial packet — run {packet.run_id}", ""]
        disposition = packet.review_disposition
        if disposition is None:
            lines.append(
                f"**{INTERNAL_DIAGNOSTIC}** — no adversarial review disposition is "
                "recorded, so nothing here is publishable as a comparative claim."
            )
        elif disposition.packet_sha256 != packet_content_sha256(packet):
            lines.append(
                f"**{INTERNAL_DIAGNOSTIC}** — the recorded review is bound to "
                f"`{disposition.packet_sha256}`, but this packet hashes to "
                f"`{packet_content_sha256(packet)}`. A stale review is no review."
            )
        else:
            lines += [
                f"**Reviewed** by `{disposition.reviewer_id}` on "
                f"{disposition.reviewed_at}, bound to `{disposition.packet_sha256}`.",
                "",
                "### Objections",
                "",
            ]
            if disposition.objections:
                lines += [
                    f"- [{objection.status}] {objection.text}"
                    for objection in disposition.objections
                ]
            else:
                lines.append("- none raised")
        lines += [
            "",
            "## Pre-registration",
            "",
            f"- contract revision: `{packet.preregistration.contract_revision}`",
            f"- document: `{packet.preregistration.path}`",
            f"- ratified sha256: `{packet.preregistration.base_sha256}`",
            f"- effective sha256: `{packet.preregistration.sha256}`",
        ]
        for amendment in packet.preregistration.amendments:
            lines.append(
                f"- amendment {amendment.sequence} ({amendment.acknowledgment_status}): "
                f"`{amendment.receipt_path}` → `{amendment.contract_sha256}`"
            )

        lines += ["", "## Assumptions", ""]
        lines += [
            f"- [{item.status}] {item.statement} ({item.evidence})"
            for item in packet.assumptions
        ]
        lines += ["", "## Confounds", ""]
        lines += [
            f"- [{item.status}] {item.statement} ({item.evidence})"
            for item in packet.confounds
        ]

        lines += ["", "## Suspicious wins", ""]
        if packet.suspicious_win_flags:
            lines += [
                f"- {flag.provider}/{flag.variant} {flag.assertion} in "
                f"{flag.scenario_id}: {flag.outcome} with "
                f"{flag.signal_disposition}; controls scoring: "
                f"{', '.join(flag.controls_scoring)}"
                for flag in packet.suspicious_win_flags
            ]
        else:
            lines.append("- none: no product win was reproduced by a control")

        lines += ["", "## Challenge artifacts", ""]
        lines += [
            f"- {bound.question} → `{bound.artifact_path}`"
            for bound in packet.challenge_paths
        ]
        return "\n".join(lines) + "\n"


__all__ = [
    "INTERNAL_DIAGNOSTIC",
    "REVIEWER_CHALLENGES",
    "AdversarialPacket",
    "AdversarialPacketError",
    "AmendmentBinding",
    "Assumption",
    "ChallengePath",
    "Confound",
    "Objection",
    "PreregistrationBinding",
    "ReviewDisposition",
    "ReviewerChallenge",
    "SuspiciousWinFlag",
    "build_adversarial_packet",
    "packet_content_sha256",
    "render_adversarial_packet",
]
