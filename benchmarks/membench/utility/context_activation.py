"""Deterministic, model-free scorer for the context-activation benchmark.

OpenSpec change ``add-context-activation-benchmark``, task 2. Consumes an
activation packet -- from a hand-written oracle-packet file today, or later
from ``activate_context``'s own output over MCP/CLI/REST -- and scores it
against the pre-registered fixture (``epistemic.corpora.context_activation``).
No model judge, no I/O beyond loading a packet file: every check here is a
pure function of a packet and a fixture, matching this package's sibling
``scoring.py`` ("Pure paired scorer over observed action outcomes. No model
judge, no I/O.").

One documented simplification relative to the full activation-packet
contract (``openspec/changes/add-context-activation/specs/context-activation/
spec.md`` in the sibling ``add-context-activation`` change), because this
scorer only has to be *correct*, not *complete*, before the compiler exists:

- **Twin false activation is "outside the twin's own gold", not "any
  resolved anchor".** Broadened (correction round, B1) beyond a
  resolved-anchor-only reading: a ``resolved``-status anchor, or *any* unit,
  pointer, current-state entry or ambiguity candidate (channels with no
  "status" concept of their own -- their mere presence already injected
  content) that names a ref outside the twin's own gold, is a false
  activation. A ``partial``-status anchor outside gold is deliberately
  excluded from this definition: that is the twin hedging allowance (see
  ``score_case``'s ``hedged`` field) and is bounded at the run level, not
  blanket-credited as safe or silently ignored. See ``context_activation``
  fixtures module's docstring for why the maximally literal spec reading is
  unsatisfiable for T3/T4/T7/T8's explicit design intent.

The real contract allows a superseded ancestor to be surfaced either omitted
or marked ``lifecycle: superseded`` with its successor named
(``provenance.superseded_by``); both paths are credited as non-poison (see
:func:`_credited_superseded_refs`), which is task 2.3.

``CurrentStateEntry.statement`` mirrors the compiler's own
``current_state[].statement`` field (``STATEMENT_MAX_CHARS = 200`` in
``exomem.working_set_state``, the sibling ``add-context-activation``
change): the authored value text (e.g. ``"status: unavailable"``), never the
``source`` label (``records``/``profile``/``note``), which names *where* the
statement came from, not what it says.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from epistemic.corpora.context_activation import (
    FIXTURES,
    MEASURED_LATENCY_MS,
    FixtureCase,
    anchor_kind_for,
)


class PacketError(ValueError):
    """Raised for a malformed packet file or dict."""


class ManifestVoidError(ValueError):
    """Raised when a run manifest lacks a required digest field."""


# --------------------------------------------------------------------------
# The packet shape (mirrors add-context-activation's activate_context output).
# --------------------------------------------------------------------------

#: Mirrors ``exomem.working_set_state.STATEMENT_MAX_CHARS`` (the sibling
#: ``add-context-activation`` change's compiler package is not a dependency
#: of this benchmark worktree, so the value is pinned here rather than
#: imported); a packet's ``current_state[].statement`` is authored text, ≤
#: this many characters, never a server-invented sentence.
STATEMENT_MAX_CHARS = 200


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
class Pointer:
    """A "look here yourself" reference the packet surfaces without inlining text."""

    ref: str
    title: str = ""
    why: str = ""


@dataclass(frozen=True)
class CurrentStateEntry:
    anchor: str
    source: str
    as_of: str | None = None
    #: The authored status text itself (task 2, M5); see module docstring.
    statement: str | None = None


@dataclass(frozen=True)
class ActivationPacket:
    """The working-memory packet a scorer consumes, oracle-file or product."""

    anchors: tuple[Anchor, ...] = ()
    roles: tuple[str, ...] = ()
    units: tuple[Unit, ...] = ()
    pointers: tuple[Pointer, ...] = ()
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
    #: Wall-clock latency for the compiler's own ``working_set.*`` stages
    #: specifically (spec: p50 ≤ 800 ms / p95 ≤ 2,500 ms on the reference
    #: corpus), distinct from ``latency_ms``'s end-to-end figure. ``None``
    #: when a caller has not measured or does not carry this breakdown
    #: (an oracle packet, or a pre-instrumentation product run).
    working_set_ms: float | None = None


#: The documented kill-switch shape (``EXOMEM_DISABLE_WORKING_SET=1``): see
#: the sibling ``add-context-activation`` spec's "Kill switch abstains
#: without building" scenario.
DISABLED_PACKET = ActivationPacket(abstained=True, abstention_reason="disabled")


def _tuple_or_empty(data: dict[str, Any], key: str) -> tuple[Any, ...]:
    value = data.get(key)
    return tuple(value) if value else ()


def _pointer_from_item(item: Any) -> Pointer:
    if isinstance(item, str):
        return Pointer(ref=item)
    return Pointer(ref=item["ref"], title=str(item.get("title", "")), why=str(item.get("why", "")))


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
        pointers = tuple(_pointer_from_item(item) for item in data.get("pointers", ()) or ())
        current_state = tuple(
            CurrentStateEntry(
                anchor=item["anchor"],
                source=str(item.get("source", "")),
                as_of=item.get("as_of"),
                statement=item.get("statement"),
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
        pointers=pointers,
        current_state=current_state,
        missing=_tuple_or_empty(data, "missing"),
        ambiguity=_tuple_or_empty(data, "ambiguity"),
        budget_limit_chars=budget.get("limit_chars"),
        budget_used_chars=int(budget.get("used_chars", 0) or 0),
        abstained=bool(data.get("abstained", False)),
        abstention_reason=abstention.get("reason"),
        latency_ms=data.get("latency_ms"),
        working_set_ms=data.get("working_set_ms"),
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


def _injected_text_parts(packet: ActivationPacket) -> tuple[str, ...]:
    """Every text-bearing field a packet would actually inject into context.

    Deliberately broader than "unit text plus current-state source" (the
    pre-correction-round scope, M4/M5): anchor titles, pointer title/why,
    ``missing`` and ``ambiguity`` labels, and current-state *statements* (the
    authored value text, never the ``source`` label) all count, because all
    of them are characters a real packet would spend budget on.
    """

    return (
        *(anchor.title for anchor in packet.anchors),
        *(unit.text for unit in packet.units),
        *(f"{p.title} {p.why}".strip() for p in packet.pointers),
        *packet.missing,
        *packet.ambiguity,
        *(entry.statement or "" for entry in packet.current_state),
    )


def _injected_text(packet: ActivationPacket) -> str:
    return "\n".join(part for part in _injected_text_parts(packet) if part)


def injected_char_count(packet: ActivationPacket) -> int:
    """The packet's actual injected character count (M3), never the packet's
    own self-reported ``budget.used_chars`` -- a packet author controls that
    field directly, so it proves nothing about what was actually injected.
    """

    return len(_injected_text(packet))


def packet_token_count(packet: ActivationPacket, *, encoding_name: str = "o200k_base") -> int:
    """Tokens over exactly the text a packet would inject into context.

    Mirrors ``benchmarks/lme/metered.py``'s frozen-encoding convention
    (``o200k_base``, ``tiktoken`` imported lazily) rather than introducing a
    second tokenizer choice.
    """

    text = _injected_text(packet)
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
    #: A twin whose expected status is ``unresolved`` observed as
    #: ``partial``/``ambiguous`` instead (B3): tolerated per-case, but
    #: bounded to at most :data:`HEDGED_TWINS_CEILING` per run.
    hedged: bool
    #: ``gold_hit`` (over resolved anchors only) / total resolved-status
    #: anchor count; ``None`` when the packet resolved nothing (M1).
    precision: float | None
    must_include_missing: tuple[str, ...]
    must_exclude_present: tuple[str, ...]
    packet_chars: int
    packet_tokens: int
    latency_ms: float | None
    working_set_ms: float | None
    by_anchor_kind: tuple[AnchorKindTally, ...]
    #: True when no packet was supplied for this case at all (M2) -- distinct
    #: from a packet that was supplied and scored ``unresolved``/abstained.
    blocked: bool
    passed: bool
    failure_reasons: tuple[str, ...]


#: Per-case gold-recall floor (spec: "activation recall on gold at least 0.90").
GOLD_RECALL_FLOOR = 0.90
#: Per-case precision floor over resolved anchors (M1): a packet padding its
#: gold hits with dozens of irrelevant resolved anchors must still fail, even
#: though none of those irrelevant anchors happens to be curated poison.
PRECISION_FLOOR = 0.80
#: Run-level ceiling on hedged twins (B3 pre-registered hedging ceiling).
HEDGED_TWINS_CEILING = 1
#: Packet-size thresholds (spec: "packet size p50 at most 900 tokens and p95
#: at most 1,500 ... hard refusal above 2,000").
TOKEN_P50_CEILING = 900
TOKEN_P95_CEILING = 1500
TOKEN_HARD_CAP = 2000
#: The compiler's own working_set.* stage latency thresholds (spec).
WORKING_SET_P50_MS_CEILING = 800
WORKING_SET_P95_MS_CEILING = 2500
#: End-to-end padded-tree precision floor for C9 (N1).
PADDING_PRECISION_FLOOR = 0.80


def _resolved_refs(packet: ActivationPacket) -> set[str]:
    return {anchor.ref for anchor in packet.anchors if anchor.status == "resolved"}


def _mentioned_refs(packet: ActivationPacket) -> set[str]:
    """Every ref the packet surfaced anywhere: any anchor, unit, pointer,
    ambiguity candidate, or current-state entry (B1). Broadened beyond
    "resolved anchors and ambiguity" to also cover units and pointers --
    otherwise a false activation injected purely through those channels
    (never touching ``anchors`` at all) would score as a clean miss.
    """

    refs = {anchor.ref for anchor in packet.anchors}
    refs.update(packet.ambiguity)
    refs.update(entry.anchor for entry in packet.current_state)
    refs.update(unit.ref for unit in packet.units)
    refs.update(pointer.ref for pointer in packet.pointers)
    return refs


def _false_activation_candidates(packet: ActivationPacket) -> set[str]:
    """Refs that are a *confident* false claim if found outside a twin's own gold.

    Resolved-status anchors, plus every unit, pointer, current-state entry
    and ambiguity candidate -- channels with no "status" concept of their
    own, where mere presence already injected content. A ``partial``-status
    anchor is excluded on purpose: see :data:`HEDGED_TWINS_CEILING` and the
    module docstring.
    """

    refs = _resolved_refs(packet)
    refs.update(unit.ref for unit in packet.units)
    refs.update(pointer.ref for pointer in packet.pointers)
    refs.update(entry.anchor for entry in packet.current_state)
    refs.update(packet.ambiguity)
    return refs


def _credited_superseded_refs(packet: ActivationPacket) -> set[str]:
    """Refs honestly presented as superseded, with a successor named (task 2.3).

    A poison ref that is a superseded ancestor is not a false activation when
    the packet marks it ``lifecycle: superseded`` and names its successor via
    ``provenance.superseded_by`` -- that is the compiler contract's marking
    path (the alternative to omission), never presenting the ancestor as
    current. A superseded unit with no successor named is not credited: an
    unnamed supersession is not distinguishable from an ordinary stale hit.
    """

    return {
        unit.ref
        for unit in packet.units
        if unit.lifecycle == "superseded" and unit.provenance.get("superseded_by")
    }


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
    false_activation_candidates = _false_activation_candidates(packet)
    credited = _credited_superseded_refs(packet)

    gold_refs = tuple(ref_for(key) for key in fixture.gold)
    poison_refs = tuple(ref_for(key) for key in fixture.poison)
    gold_hit = sum(1 for ref in gold_refs if ref in mentioned)
    poison_hit = sum(1 for ref in poison_refs if ref in mentioned and ref not in credited)

    tallies: dict[str, list[int]] = {}
    for key, ref in zip(fixture.gold, gold_refs, strict=True):
        tally = tallies.setdefault(anchor_kind_for(key), [0, 0, 0, 0])
        tally[0] += 1
        tally[1] += int(ref in mentioned)
    for key, ref in zip(fixture.poison, poison_refs, strict=True):
        tally = tallies.setdefault(anchor_kind_for(key), [0, 0, 0, 0])
        tally[2] += 1
        tally[3] += int(ref in mentioned and ref not in credited)
    by_anchor_kind = tuple(
        AnchorKindTally(kind=kind, gold_total=g_t, gold_hit=g_h, poison_total=p_t, poison_hit=p_h)
        for kind, (g_t, g_h, p_t, p_h) in sorted(tallies.items())
    )

    observed_status = turn_status(packet)
    status_match = observed_status == fixture.expected_status

    # Twin false activation (B1): a ref outside the twin's own gold, found via
    # a resolved anchor or any unit/pointer/current-state/ambiguity channel.
    own_gold = set(gold_refs)
    twin_false_activation = bool(
        fixture.case_id.startswith("T") and any(ref not in own_gold for ref in false_activation_candidates)
    )

    # Hedging (B3): a twin expected to resolve nothing may instead report
    # partial/ambiguous status without being punished for the status
    # mismatch itself -- bounded at the run level by HEDGED_TWINS_CEILING,
    # not blanket-permitted here.
    # `ambiguous` requires *naming* concrete candidate refs (`ambiguity` is
    # one of the unconditional false_activation_candidates channels), so for
    # a twin with no legitimate gold of its own (T1/T2/T6), naming any
    # candidate at all is already a false activation; `partial` (a status on
    # one anchor, asserting no specific alternative) is the hedge shape that
    # is actually achievable clean. Both remain in the vocabulary here so a
    # twin that *does* carry its own narrow gold (T4) can still hedge via
    # `ambiguous` between its own candidates without being blocked by this.
    is_unresolved_expected_twin = fixture.case_id.startswith("T") and fixture.expected_status == "unresolved"
    hedged = is_unresolved_expected_twin and observed_status in ("partial", "ambiguous") and not twin_false_activation

    # Precision (M1): gold hits over *all* resolved anchors, not just the
    # curated poison list -- a packet padded with dozens of irrelevant
    # resolved anchors that happen not to be on the poison list must still
    # fail, which a poison-only precision figure would miss entirely. A
    # credited superseded ancestor (task 2.3) is excluded from the
    # denominator: deliberately, transparently marking it is correct
    # compiler behaviour, not irrelevant padding, and must not be penalised
    # as if it were.
    gold_hit_resolved = sum(1 for ref in gold_refs if ref in resolved)
    total_resolved = len(resolved - credited)
    precision = (gold_hit_resolved / total_resolved) if total_resolved else None

    injected_text = _injected_text(packet)
    # C6's must_include fact ("9.5") is a response-reflection fact for the
    # reminder-turn test (`score_reminder_turn`, over the agent's answer),
    # never a packet-content fact: C6's packet is supposed to be empty by
    # design (the C6-specific check below), so checking must_include against
    # packet text here would make C6 permanently unpassable.
    if fixture.case_id == "C6":
        must_include_missing: tuple[str, ...] = ()
    else:
        must_include_missing = tuple(s for s in fixture.must_include if s not in injected_text)
    must_exclude_present = tuple(s for s in fixture.must_exclude if s in injected_text)

    failure_reasons: list[str] = []
    if not status_match and not hedged:
        failure_reasons.append(f"expected status {fixture.expected_status!r}, observed {observed_status!r}")
    if poison_hit:
        failure_reasons.append(f"{poison_hit} poison anchor(s) surfaced")
    if twin_false_activation:
        failure_reasons.append("twin surfaced a ref outside its own gold")
    if fixture.gold:
        recall = gold_hit / len(fixture.gold)
        if recall < GOLD_RECALL_FLOOR:
            failure_reasons.append(f"gold recall {recall:.2f} below the {GOLD_RECALL_FLOOR} floor")
    if precision is not None and precision < PRECISION_FLOOR:
        failure_reasons.append(f"precision {precision:.2f} below the {PRECISION_FLOOR} floor")
    if must_include_missing:
        failure_reasons.append(f"missing required fact(s): {list(must_include_missing)}")
    if must_exclude_present:
        failure_reasons.append(f"forbidden fact(s) present: {list(must_exclude_present)}")
    if fixture.case_id == "C6" and (mentioned or injected_char_count(packet)):
        failure_reasons.append("no-memory case injected an anchor, unit, pointer or characters")
    # B2: an ambiguous turn runs no role lane -- units/pointers must be
    # empty regardless of case or twin. (General contract property; see the
    # sibling add-context-activation spec's "no role lane runs" scenario.)
    if observed_status == "ambiguous" and (packet.units or packet.pointers):
        failure_reasons.append("ambiguous status packet must carry no units or pointers (no role lane runs)")

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
        hedged=hedged,
        precision=precision,
        must_include_missing=must_include_missing,
        must_exclude_present=must_exclude_present,
        packet_chars=injected_char_count(packet),
        packet_tokens=packet_token_count(packet),
        latency_ms=packet.latency_ms,
        working_set_ms=packet.working_set_ms,
        by_anchor_kind=by_anchor_kind,
        blocked=False,
        passed=not failure_reasons,
        failure_reasons=tuple(failure_reasons),
    )


def _blocked_score(fixture: FixtureCase) -> CaseScore:
    """The score for a fixture with no packet supplied at all (M2).

    Distinct from scoring :data:`DISABLED_PACKET` (which a caller may supply
    *explicitly* to exercise the documented kill-switch shape, and which
    scores normally through :func:`score_case`): an absent packet means the
    run never attempted this case, which must void the run's verdict, not
    merely fail one case.
    """

    return CaseScore(
        case_id=fixture.case_id,
        is_twin=fixture.case_id.startswith("T"),
        expected_status=fixture.expected_status,
        observed_status="blocked",
        status_match=False,
        gold_hit=0,
        gold_total=len(fixture.gold),
        poison_hit=0,
        poison_total=len(fixture.poison),
        twin_false_activation=False,
        hedged=False,
        precision=None,
        must_include_missing=fixture.must_include,
        must_exclude_present=(),
        packet_chars=0,
        packet_tokens=0,
        latency_ms=None,
        working_set_ms=None,
        by_anchor_kind=(),
        blocked=True,
        passed=False,
        failure_reasons=("no packet supplied: run blocked",),
    )


@dataclass(frozen=True)
class PaddingRobustnessResult:
    """C9 (padded tree) vs. T9 (unpadded tree) consistency check (N1).

    A twin carrying a different turn cannot show padding did anything to a
    query; two identical queries scored against two different corpus states
    can. C9 is meant to pass only when padding neither craters precision nor
    silently drops a gold hit T9 found on the unpadded tree.
    """

    padded_case_id: str
    base_case_id: str
    precision_padded: float | None
    recall_padded: float | None
    recall_base: float | None
    precision_delta: float | None
    passed: bool
    reasons: tuple[str, ...]


def _recall(score: CaseScore) -> float | None:
    return (score.gold_hit / score.gold_total) if score.gold_total else None


def score_padding_robustness(padded_score: CaseScore, base_score: CaseScore) -> PaddingRobustnessResult:
    """Compare C9's padded-tree score against T9's unpadded-tree score."""

    recall_padded = _recall(padded_score)
    recall_base = _recall(base_score)
    precision_delta = (
        padded_score.precision - base_score.precision
        if padded_score.precision is not None and base_score.precision is not None
        else None
    )
    reasons: list[str] = []
    if padded_score.precision is None or padded_score.precision < PADDING_PRECISION_FLOOR:
        reasons.append(f"padded-tree precision below the {PADDING_PRECISION_FLOOR} floor")
    if recall_padded != recall_base:
        reasons.append("padding changed recall relative to the unpadded tree")
    return PaddingRobustnessResult(
        padded_case_id=padded_score.case_id,
        base_case_id=base_score.case_id,
        precision_padded=padded_score.precision,
        recall_padded=recall_padded,
        recall_base=recall_base,
        precision_delta=precision_delta,
        passed=not reasons,
        reasons=tuple(reasons),
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
    blocked_count: int
    verdict: str  # "no_verdict" | "reviewed" -- never a pass/fail aggregate score


def build_report(
    manifest: RunManifest, scores: tuple[CaseScore, ...], *, fixtures_total: int = 18
) -> AuditReport:
    blocked_count = sum(1 for score in scores if score.blocked)
    # "reviewed" requires full coverage *and* zero blocked cases (M2): a run
    # that covered every fixture id but scored some of them "blocked" has
    # not actually reviewed anything for those cases.
    verdict = "reviewed" if (len(scores) >= fixtures_total and blocked_count == 0) else "no_verdict"
    return AuditReport(
        manifest=manifest,
        per_case=scores,
        fixtures_run=len(scores),
        fixtures_total=fixtures_total,
        blocked_count=blocked_count,
        verdict=verdict,
    )


def run_audit(
    packets: dict[str, ActivationPacket],
    *,
    manifest: RunManifest,
    key_to_ref: dict[str, str] | None = None,
    fixtures: tuple[FixtureCase, ...] = FIXTURES,
) -> AuditReport:
    """Score every fixture against its supplied packet, or mark it blocked.

    A ``case_id`` with no entry in ``packets`` is scored ``blocked`` (M2),
    never silently substituted with :data:`DISABLED_PACKET`: that
    substitution made "nobody ran this case" indistinguishable from "the
    compiler was deliberately disabled for this case", and voided the run's
    verdict only by accident (via the coverage count), not by design. A
    caller exercising the documented kill-switch shape passes
    ``DISABLED_PACKET`` explicitly instead.
    """

    scores = tuple(
        score_case(packets[fixture.case_id], fixture, key_to_ref=key_to_ref)
        if fixture.case_id in packets
        else _blocked_score(fixture)
        for fixture in fixtures
    )
    # `fixtures_total` is always the canonical full registered set (18), not
    # `len(fixtures)`: a caller scoring a deliberately partial subset (e.g. a
    # dropped twin) must still see "no_verdict", never "reviewed" just
    # because it covered everything it decided to attempt.
    return build_report(manifest, scores, fixtures_total=len(FIXTURES))


def _percentile(data: list[float], pct: float) -> float | None:
    """Ceil-rank percentile (``rank = ceil(pct * n)``, ``index = rank - 1``):
    pins p95 over n = 18 to the maximum value, matching this benchmark's own
    pre-registered reporting convention (minor fix; the prior nearest-rank
    approximation under-reported p95 for small n).
    """

    if not data:
        return None
    rank = max(1, math.ceil(pct * len(data)))
    index = min(len(data), rank) - 1
    return data[index]


def token_size_distribution(scores: Iterable[CaseScore]) -> dict[str, Any]:
    """The packet-size distribution across a run's cases.

    A percentile is the benchmark's own pre-registered way of reporting
    packet-size threshold compliance (spec: "packet size p50 at most 900
    tokens and p95 at most 1,500 ... hard refusal above 2,000"), not a
    weighted aggregate score: it is always reported beside the full per-case
    list it is derived from, never in place of it.
    """

    sizes = sorted(score.packet_tokens for score in scores)
    over_cap = [score.case_id for score in scores if score.packet_tokens > TOKEN_HARD_CAP]
    return {"p50": _percentile(sizes, 0.50), "p95": _percentile(sizes, 0.95), "over_hard_cap": over_cap}


def _latency_distribution(values: list[float]) -> dict[str, Any]:
    values = sorted(values)
    return {"p50": _percentile(values, 0.50), "p95": _percentile(values, 0.95)}


def working_set_latency_distribution(scores: Iterable[CaseScore]) -> dict[str, Any]:
    """The compiler-stage latency distribution, over only the cases that carry it."""

    return _latency_distribution([score.working_set_ms for score in scores if score.working_set_ms is not None])


def end_to_end_latency_distribution(scores: Iterable[CaseScore]) -> dict[str, Any]:
    """The end-to-end ``activate_context`` latency distribution, over only the
    cases that carry it -- compared against :data:`MEASURED_LATENCY_MS`'s
    naive-path baseline, never a placeholder figure (M8).
    """

    return _latency_distribution([score.latency_ms for score in scores if score.latency_ms is not None])


def audit_passed(report: AuditReport) -> bool:
    """Whether the audit is green: full coverage, every case passed, and every
    pre-registered run-level bound (token size, hedging ceiling, latency,
    C9 padding robustness) is met.
    """

    if report.verdict != "reviewed":
        return False
    if not all(score.passed for score in report.per_case):
        return False

    sizes = token_size_distribution(report.per_case)
    if sizes["over_hard_cap"]:
        return False
    if sizes["p50"] is not None and sizes["p50"] > TOKEN_P50_CEILING:
        return False
    if sizes["p95"] is not None and sizes["p95"] > TOKEN_P95_CEILING:
        return False

    hedged_count = sum(1 for score in report.per_case if score.hedged)
    if hedged_count > HEDGED_TWINS_CEILING:
        return False

    working_set = working_set_latency_distribution(report.per_case)
    if working_set["p50"] is not None and working_set["p50"] > WORKING_SET_P50_MS_CEILING:
        return False
    if working_set["p95"] is not None and working_set["p95"] > WORKING_SET_P95_MS_CEILING:
        return False

    end_to_end = end_to_end_latency_distribution(report.per_case)
    if end_to_end["p50"] is not None and end_to_end["p50"] > MEASURED_LATENCY_MS["p50_ms"]:
        return False
    if end_to_end["p95"] is not None and end_to_end["p95"] > MEASURED_LATENCY_MS["p95_ms"]:
        return False

    scores_by_id = {score.case_id: score for score in report.per_case}
    if "C9" in scores_by_id and "T9" in scores_by_id:
        if not score_padding_robustness(scores_by_id["C9"], scores_by_id["T9"]).passed:
            return False

    return True


def report_to_dict(report: AuditReport) -> dict[str, Any]:
    scores_by_id = {score.case_id: score for score in report.per_case}
    padding_robustness = None
    if "C9" in scores_by_id and "T9" in scores_by_id:
        result = score_padding_robustness(scores_by_id["C9"], scores_by_id["T9"])
        padding_robustness = {
            "padded_case_id": result.padded_case_id,
            "base_case_id": result.base_case_id,
            "precision_padded": result.precision_padded,
            "recall_padded": result.recall_padded,
            "recall_base": result.recall_base,
            "precision_delta": result.precision_delta,
            "passed": result.passed,
            "reasons": list(result.reasons),
        }
    hedged_total = sum(1 for score in report.per_case if score.is_twin and score.expected_status == "unresolved")
    hedged_count = sum(1 for score in report.per_case if score.hedged)
    return {
        "manifest": dataclasses.asdict(report.manifest),
        "verdict": report.verdict,
        "fixtures_run": report.fixtures_run,
        "fixtures_total": report.fixtures_total,
        "blocked": {"count": report.blocked_count, "total": report.fixtures_total},
        "hedged_twins": {"count": hedged_count, "total": hedged_total},
        "packet_size_tokens": token_size_distribution(report.per_case),
        "working_set_latency_ms": working_set_latency_distribution(report.per_case),
        "end_to_end_latency_ms": end_to_end_latency_distribution(report.per_case),
        "c9_padding_robustness": padding_robustness,
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
                "hedged": score.hedged,
                "precision": score.precision,
                "must_include_missing": list(score.must_include_missing),
                "must_exclude_present": list(score.must_exclude_present),
                "packet_chars": score.packet_chars,
                "packet_tokens": score.packet_tokens,
                "latency_ms": score.latency_ms,
                "working_set_ms": score.working_set_ms,
                "by_anchor_kind": [
                    {
                        "kind": tally.kind,
                        "gold": {"hit": tally.gold_hit, "total": tally.gold_total},
                        "poison": {"hit": tally.poison_hit, "total": tally.poison_total},
                    }
                    for tally in score.by_anchor_kind
                ],
                "blocked": score.blocked,
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
    "HEDGED_TWINS_CEILING",
    "PADDING_PRECISION_FLOOR",
    "PRECISION_FLOOR",
    "REQUIRED_DIGEST_FIELDS",
    "STATEMENT_MAX_CHARS",
    "TOKEN_HARD_CAP",
    "TOKEN_P50_CEILING",
    "TOKEN_P95_CEILING",
    "WORKING_SET_P50_MS_CEILING",
    "WORKING_SET_P95_MS_CEILING",
    "ActivationPacket",
    "Anchor",
    "AnchorKindTally",
    "AuditReport",
    "CaseScore",
    "CurrentStateEntry",
    "ManifestVoidError",
    "PacketError",
    "PaddingRobustnessResult",
    "Pointer",
    "RunManifest",
    "Unit",
    "audit_passed",
    "build_report",
    "end_to_end_latency_distribution",
    "injected_char_count",
    "load_packet",
    "packet_from_dict",
    "packet_token_count",
    "report_to_dict",
    "run_audit",
    "score_case",
    "score_padding_robustness",
    "token_size_distribution",
    "turn_status",
    "validate_manifest",
    "working_set_latency_distribution",
    "write_report",
]
