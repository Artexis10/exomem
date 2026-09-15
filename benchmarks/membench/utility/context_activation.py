"""Deterministic, model-free scorer for the context-activation benchmark.

OpenSpec change ``add-context-activation-benchmark``, task 2. Consumes an
activation packet -- from a hand-written oracle-packet file today, or later
from ``activate_context``'s own output over MCP/CLI/REST -- and scores it
against the pre-registered fixture (``epistemic.corpora.context_activation``).
No model judge, no I/O beyond loading a packet file: every check here is a
pure function of a packet and a fixture, matching this package's sibling
``scoring.py`` ("Pure paired scorer over observed action outcomes. No model
judge, no I/O.").

Two documented simplifications relative to the full activation-packet
contract (``openspec/changes/add-context-activation/specs/context-activation/
spec.md`` in the sibling ``add-context-activation`` change), both because this
scorer only has to be *correct*, not *complete*, before the compiler exists:

1. **Poison is "never surfaced", not "never surfaced as current".** The real
   contract allows a superseded ancestor to be surfaced either omitted or
   marked ``lifecycle: superseded``. This scorer only credits the omission
   path: a poison ref appearing anywhere counts against precision, whether
   or not it carries a superseded marking. Stricter than the product
   contract, never looser.
2. **Twin false activation is "resolved outside the twin's own gold", not
   "any resolved anchor".** See ``context_activation`` fixtures module's
   docstring for why the maximally literal reading of the benchmark spec's
   own scenario text is unsatisfiable for T3/T7/T8's explicit design intent.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from epistemic.corpora.context_activation import FIXTURES, FixtureCase, anchor_kind_for


class PacketError(ValueError):
    """Raised for a malformed packet file or dict."""


class ManifestVoidError(ValueError):
    """Raised when a run manifest lacks a required digest field."""


# --------------------------------------------------------------------------
# The packet shape (mirrors add-context-activation's activate_context output).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    ref: str
    title: str
    kind: str
    status: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unit:
    ref: str
    role: str
    text: str
    lifecycle: str = "active"
    updated: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CurrentStateEntry:
    anchor: str
    source: str
    as_of: str | None = None


@dataclass(frozen=True)
class ActivationPacket:
    """The working-memory packet a scorer consumes, oracle-file or product."""

    anchors: tuple[Anchor, ...] = ()
    roles: tuple[str, ...] = ()
    units: tuple[Unit, ...] = ()
    pointers: tuple[str, ...] = ()
    current_state: tuple[CurrentStateEntry, ...] = ()
    missing: tuple[str, ...] = ()
    ambiguity: tuple[str, ...] = ()
    budget_limit_chars: int | None = None
    budget_used_chars: int = 0
    abstained: bool = False
    abstention_reason: str | None = None
    #: Wall-clock latency for producing this packet, attached by the caller
    #: (the harness measures it; the packet contract itself carries no
    #: latency field). ``None`` for a hand-written oracle packet.
    latency_ms: float | None = None


#: The documented kill-switch shape (``EXOMEM_DISABLE_WORKING_SET=1``): see
#: the sibling ``add-context-activation`` spec's "Kill switch abstains
#: without building" scenario. Used both as the literal disabled-mechanism
#: packet and as the default for any case a run supplies no packet for.
DISABLED_PACKET = ActivationPacket(abstained=True, abstention_reason="disabled")


def _tuple_or_empty(data: dict[str, Any], key: str) -> tuple[Any, ...]:
    value = data.get(key)
    return tuple(value) if value else ()


def packet_from_dict(data: dict[str, Any]) -> ActivationPacket:
    try:
        anchors = tuple(
            Anchor(
                ref=item["ref"],
                title=str(item.get("title", "")),
                kind=str(item.get("kind", "unknown")),
                status=item["status"],
                evidence=tuple(item.get("evidence", ()) or ()),
            )
            for item in data.get("anchors", ()) or ()
        )
        units = tuple(
            Unit(
                ref=item["ref"],
                role=str(item.get("role", "")),
                text=str(item.get("text", "")),
                lifecycle=str(item.get("lifecycle", "active")),
                updated=item.get("updated"),
                provenance=dict(item.get("provenance", {}) or {}),
            )
            for item in data.get("units", ()) or ()
        )
        current_state = tuple(
            CurrentStateEntry(
                anchor=item["anchor"], source=str(item.get("source", "")), as_of=item.get("as_of")
            )
            for item in data.get("current_state", ()) or ()
        )
    except KeyError as exc:
        raise PacketError(f"packet entry missing required field {exc}") from exc

    budget = data.get("budget", {}) or {}
    abstention = data.get("abstention", {}) or {}
    return ActivationPacket(
        anchors=anchors,
        roles=_tuple_or_empty(data, "roles"),
        units=units,
        pointers=_tuple_or_empty(data, "pointers"),
        current_state=current_state,
        missing=_tuple_or_empty(data, "missing"),
        ambiguity=_tuple_or_empty(data, "ambiguity"),
        budget_limit_chars=budget.get("limit_chars"),
        budget_used_chars=int(budget.get("used_chars", 0) or 0),
        abstained=bool(data.get("abstained", False)),
        abstention_reason=abstention.get("reason"),
        latency_ms=data.get("latency_ms"),
    )


def load_packet(path: Path) -> ActivationPacket:
    """Load a packet from a JSON file (an oracle packet, or captured product output)."""

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PacketError(f"cannot read packet {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PacketError(f"packet {path} is not a JSON object")
    return packet_from_dict(data)


def turn_status(packet: ActivationPacket) -> str:
    """The packet's turn-level status, derived from the anchor/ambiguity/abstention shape.

    The activation contract states per-anchor resolution (``resolved``,
    ``partial``) and turn-level ``ambiguous``/abstained ``unresolved``
    separately rather than as one packet field; this folds them into the
    single vocabulary the benchmark fixtures pin per case.
    """

    if packet.abstained:
        return "unresolved"
    if packet.ambiguity:
        return "ambiguous"
    statuses = {a.status for a in packet.anchors}
    if "resolved" in statuses:
        return "resolved"
    if "partial" in statuses:
        return "partial"
    return "unresolved"


def packet_token_count(packet: ActivationPacket, *, encoding_name: str = "o200k_base") -> int:
    """Tokens over exactly the text a packet would inject into context.

    Mirrors ``benchmarks/lme/metered.py``'s frozen-encoding convention
    (``o200k_base``, ``tiktoken`` imported lazily) rather than introducing a
    second tokenizer choice.
    """

    text = "\n".join((*(unit.text for unit in packet.units), *(entry.source for entry in packet.current_state)))
    if not text:
        return 0
    import tiktoken

    return len(tiktoken.get_encoding(encoding_name).encode_ordinary(text))


# --------------------------------------------------------------------------
# Per-case scoring.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AnchorKindTally:
    """Gold/poison hit counts for one anchor kind. Always a numerator/denominator pair."""

    kind: str
    gold_total: int
    gold_hit: int
    poison_total: int
    poison_hit: int


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    is_twin: bool
    expected_status: str
    observed_status: str
    status_match: bool
    gold_hit: int
    gold_total: int
    poison_hit: int
    poison_total: int
    twin_false_activation: bool
    must_include_missing: tuple[str, ...]
    must_exclude_present: tuple[str, ...]
    packet_chars: int
    packet_tokens: int
    latency_ms: float | None
    by_anchor_kind: tuple[AnchorKindTally, ...]
    passed: bool
    failure_reasons: tuple[str, ...]


#: Per-case gold-recall floor (spec: "activation recall on gold at least 0.90").
GOLD_RECALL_FLOOR = 0.90


def _resolved_refs(packet: ActivationPacket) -> set[str]:
    return {anchor.ref for anchor in packet.anchors if anchor.status == "resolved"}


def _mentioned_refs(packet: ActivationPacket) -> set[str]:
    """Every ref the packet surfaced anywhere: as any anchor, in ambiguity, or in current_state."""

    refs = {anchor.ref for anchor in packet.anchors}
    refs.update(packet.ambiguity)
    refs.update(entry.anchor for entry in packet.current_state)
    return refs


def score_case(
    packet: ActivationPacket,
    fixture: FixtureCase,
    *,
    key_to_ref: dict[str, str] | None = None,
) -> CaseScore:
    """Score one packet against its pre-registered fixture.

    ``key_to_ref`` maps a fixture's logical gold/poison keys to the packet's
    own anchor ``ref`` values: the synthetic corpus's ``key_to_path`` for CI,
    or a locally-authored (never-committed) real-vault mapping for the
    private instrument. Defaults to the identity mapping, for a hand-written
    oracle packet that already speaks in the fixture's own logical keys.
    """

    def ref_for(key: str) -> str:
        return key_to_ref.get(key, key) if key_to_ref else key

    resolved = _resolved_refs(packet)
    mentioned = _mentioned_refs(packet)

    gold_refs = tuple(ref_for(key) for key in fixture.gold)
    poison_refs = tuple(ref_for(key) for key in fixture.poison)
    gold_hit = sum(1 for ref in gold_refs if ref in mentioned)
    poison_hit = sum(1 for ref in poison_refs if ref in mentioned)

    tallies: dict[str, list[int]] = {}
    for key, ref in zip(fixture.gold, gold_refs, strict=True):
        tally = tallies.setdefault(anchor_kind_for(key), [0, 0, 0, 0])
        tally[0] += 1
        tally[1] += int(ref in mentioned)
    for key, ref in zip(fixture.poison, poison_refs, strict=True):
        tally = tallies.setdefault(anchor_kind_for(key), [0, 0, 0, 0])
        tally[2] += 1
        tally[3] += int(ref in mentioned)
    by_anchor_kind = tuple(
        AnchorKindTally(kind=kind, gold_total=g_t, gold_hit=g_h, poison_total=p_t, poison_hit=p_h)
        for kind, (g_t, g_h, p_t, p_h) in sorted(tallies.items())
    )

    observed_status = turn_status(packet)
    status_match = observed_status == fixture.expected_status

    # Twin false activation: an anchor resolved outside the twin's own gold.
    # See this module's docstring (simplification 2) for why this departs
    # from a maximally literal "any resolved anchor" reading.
    own_gold = set(gold_refs)
    twin_false_activation = bool(fixture.case_id.startswith("T") and any(ref not in own_gold for ref in resolved))

    text_blob = " ".join((*(unit.text for unit in packet.units), *(entry.source for entry in packet.current_state)))
    must_include_missing = tuple(s for s in fixture.must_include if s not in text_blob)
    must_exclude_present = tuple(s for s in fixture.must_exclude if s in text_blob)

    failure_reasons: list[str] = []
    if not status_match:
        failure_reasons.append(f"expected status {fixture.expected_status!r}, observed {observed_status!r}")
    if poison_hit:
        failure_reasons.append(f"{poison_hit} poison anchor(s) surfaced")
    if twin_false_activation:
        failure_reasons.append("twin resolved an anchor outside its own gold")
    if fixture.gold:
        recall = gold_hit / len(fixture.gold)
        if recall < GOLD_RECALL_FLOOR:
            failure_reasons.append(f"gold recall {recall:.2f} below the {GOLD_RECALL_FLOOR} floor")
    if must_include_missing:
        failure_reasons.append(f"missing required fact(s): {list(must_include_missing)}")
    if must_exclude_present:
        failure_reasons.append(f"forbidden fact(s) present: {list(must_exclude_present)}")
    if fixture.case_id == "C6" and (resolved or packet.budget_used_chars):
        failure_reasons.append("no-memory case injected an anchor or characters")

    return CaseScore(
        case_id=fixture.case_id,
        is_twin=fixture.case_id.startswith("T"),
        expected_status=fixture.expected_status,
        observed_status=observed_status,
        status_match=status_match,
        gold_hit=gold_hit,
        gold_total=len(fixture.gold),
        poison_hit=poison_hit,
        poison_total=len(fixture.poison),
        twin_false_activation=twin_false_activation,
        must_include_missing=must_include_missing,
        must_exclude_present=must_exclude_present,
        packet_chars=packet.budget_used_chars,
        packet_tokens=packet_token_count(packet),
        latency_ms=packet.latency_ms,
        by_anchor_kind=by_anchor_kind,
        passed=not failure_reasons,
        failure_reasons=tuple(failure_reasons),
    )


# --------------------------------------------------------------------------
# Run manifest and report (no aggregate field anywhere; every metric is a
# per-case or per-case-per-anchor-kind numerator/denominator dual).
# --------------------------------------------------------------------------

REQUIRED_DIGEST_FIELDS: tuple[str, ...] = ("fixture_set_digest", "corpus_digest", "threshold_digest")


@dataclass(frozen=True)
class RunManifest:
    fixture_set_digest: str
    corpus_digest: str
    threshold_digest: str
    mechanism: str = "unknown"


def validate_manifest(data: dict[str, Any]) -> RunManifest:
    """Refuse (:class:`ManifestVoidError`) a run manifest missing any required digest."""

    missing = [field_name for field_name in REQUIRED_DIGEST_FIELDS if not data.get(field_name)]
    if missing:
        raise ManifestVoidError(f"run manifest void: missing digest field(s) {missing}")
    return RunManifest(
        fixture_set_digest=data["fixture_set_digest"],
        corpus_digest=data["corpus_digest"],
        threshold_digest=data["threshold_digest"],
        mechanism=str(data.get("mechanism", "unknown")),
    )


@dataclass(frozen=True)
class AuditReport:
    manifest: RunManifest
    per_case: tuple[CaseScore, ...]
    fixtures_run: int
    fixtures_total: int
    verdict: str  # "no_verdict" | "reviewed" -- never a pass/fail aggregate score


def build_report(
    manifest: RunManifest, scores: tuple[CaseScore, ...], *, fixtures_total: int = 18
) -> AuditReport:
    verdict = "reviewed" if len(scores) >= fixtures_total else "no_verdict"
    return AuditReport(
        manifest=manifest, per_case=scores, fixtures_run=len(scores), fixtures_total=fixtures_total, verdict=verdict
    )


def run_audit(
    packets: dict[str, ActivationPacket],
    *,
    manifest: RunManifest,
    key_to_ref: dict[str, str] | None = None,
    fixtures: tuple[FixtureCase, ...] = FIXTURES,
) -> AuditReport:
    """Score every fixture against its supplied packet, or :data:`DISABLED_PACKET`.

    A ``case_id`` with no entry in ``packets`` is scored against the
    documented kill-switch shape rather than skipped -- an absent packet is
    exactly what the mechanism-removal scenario means by "the compiler is
    disabled".
    """

    scores = tuple(
        score_case(packets.get(fixture.case_id, DISABLED_PACKET), fixture, key_to_ref=key_to_ref)
        for fixture in fixtures
    )
    # `fixtures_total` is always the canonical full registered set (18), not
    # `len(fixtures)`: a caller scoring a deliberately partial subset (e.g. a
    # dropped twin) must still see "no_verdict", never "reviewed" just
    # because it covered everything it decided to attempt.
    return build_report(manifest, scores, fixtures_total=len(FIXTURES))


def audit_passed(report: AuditReport) -> bool:
    """Whether the audit is green: full coverage and every case passed."""

    return report.verdict == "reviewed" and all(score.passed for score in report.per_case)


def token_size_distribution(scores: Iterable[CaseScore]) -> dict[str, Any]:
    """The packet-size distribution across a run's cases.

    A percentile is the benchmark's own pre-registered way of reporting
    packet-size threshold compliance (spec: "packet size p50 at most 900
    tokens and p95 at most 1,500 ... hard refusal above 2,000"), not a
    weighted aggregate score: it is always reported beside the full per-case
    list it is derived from, never in place of it.
    """

    sizes = sorted(score.packet_tokens for score in scores)
    if not sizes:
        return {"p50": None, "p95": None, "over_hard_cap": []}

    def _percentile(data: list[int], pct: float) -> int:
        index = min(len(data) - 1, max(0, int(round(pct * (len(data) - 1)))))
        return data[index]

    over_cap = [score.case_id for score in scores if score.packet_tokens > 2000]
    return {"p50": _percentile(sizes, 0.50), "p95": _percentile(sizes, 0.95), "over_hard_cap": over_cap}


def report_to_dict(report: AuditReport) -> dict[str, Any]:
    return {
        "manifest": dataclasses.asdict(report.manifest),
        "verdict": report.verdict,
        "fixtures_run": report.fixtures_run,
        "fixtures_total": report.fixtures_total,
        "packet_size_tokens": token_size_distribution(report.per_case),
        "per_case": [
            {
                "case_id": score.case_id,
                "is_twin": score.is_twin,
                "expected_status": score.expected_status,
                "observed_status": score.observed_status,
                "status_match": score.status_match,
                "gold": {"hit": score.gold_hit, "total": score.gold_total},
                "poison": {"hit": score.poison_hit, "total": score.poison_total},
                "twin_false_activation": score.twin_false_activation,
                "must_include_missing": list(score.must_include_missing),
                "must_exclude_present": list(score.must_exclude_present),
                "packet_chars": score.packet_chars,
                "packet_tokens": score.packet_tokens,
                "latency_ms": score.latency_ms,
                "by_anchor_kind": [
                    {
                        "kind": tally.kind,
                        "gold": {"hit": tally.gold_hit, "total": tally.gold_total},
                        "poison": {"hit": tally.poison_hit, "total": tally.poison_total},
                    }
                    for tally in score.by_anchor_kind
                ],
                "passed": score.passed,
                "failure_reasons": list(score.failure_reasons),
            }
            for score in report.per_case
        ],
    }


def write_report(report: AuditReport, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report_to_dict(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


__all__ = [
    "DISABLED_PACKET",
    "GOLD_RECALL_FLOOR",
    "REQUIRED_DIGEST_FIELDS",
    "ActivationPacket",
    "Anchor",
    "AnchorKindTally",
    "AuditReport",
    "CaseScore",
    "CurrentStateEntry",
    "ManifestVoidError",
    "PacketError",
    "RunManifest",
    "Unit",
    "audit_passed",
    "build_report",
    "load_packet",
    "packet_from_dict",
    "packet_token_count",
    "report_to_dict",
    "run_audit",
    "score_case",
    "token_size_distribution",
    "turn_status",
    "validate_manifest",
    "write_report",
]
