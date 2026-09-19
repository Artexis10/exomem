"""Deterministic, model-free scorer for the context-activation benchmark.

OpenSpec change ``add-context-activation-benchmark``, task 2. Consumes an
activation packet -- from a hand-written oracle-packet file today, or later
from ``activate_context``'s own output over MCP/CLI/REST -- and scores it
against the pre-registered fixture (``epistemic.corpora.context_activation``).
No model judge, no I/O beyond loading a packet file: every check here is a
pure function of a packet and a fixture, matching this package's sibling
``scoring.py`` ("Pure paired scorer over observed action outcomes. No model
judge, no I/O.").

This module's thresholds quote, rather than restate, ``openspec/changes/
add-context-activation-benchmark/specs/context-activation-benchmark/
spec.md`` -- the amended spec is the contract; a docstring that paraphrases
it is a second copy that can drift. In its own words (Requirement:
Pre-registered thresholds):

    activation precision at least 0.80, computed over every ref the packet
    surfaces as a resolved anchor, unit or pointer and excluding superseded
    ancestors the packet credits as marked [...] `resolved` false activation
    [...] is a `resolved` anchor, unit, pointer, current-state entry or
    ambiguity candidate outside the twin's own gold set (a twin designed to
    resolve on a narrow gold of its own is not a false activation); `partial`
    activation on twins whose expected status is `unresolved` limited to at
    most one twin per run [...] (a twin without gold of its own cannot hedge
    as `ambiguous`, because naming any ambiguity candidate is itself a false
    activation) [...] a current-state statement of at most 200 characters, a
    longer one failing the case as a packet-contract violation [...]
    percentiles taken ceil-rank so that over the eighteen packets of one run
    the p95 bound is the run's maximum and the hard refusal is reached only
    by larger runs.

See :func:`score_case` (false activation, hedging, precision, the
statement-length check) and :func:`_percentile` (ceil-rank) for where each
clause above is implemented.

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
    BASE_DISTRACTOR_COUNT,
    DEFAULT_DISTRACTOR_COUNT,
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
    #: The corpus tree (distractor count) this packet was compiled against
    #: (round-two N1 consequence 1, spec: "Every fixture and every packet
    #: SHALL record the corpus tree ... it belongs to"). Set by whatever
    #: compiled the packet -- the harness for a product run, the test/oracle
    #: author for a hand-written one -- never inferred here.
    distractor_count: int | None = None


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
        distractor_count=data.get("distractor_count"),
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
    #: The corpus tree (distractor count) the scored packet was compiled
    #: against, propagated from :attr:`ActivationPacket.distractor_count`
    #: (round-two N1 consequence 1); ``None`` when the packet didn't record it.
    distractor_count: int | None
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

    # Hedging (B3, spec scenario "Hedged twin activation is reported, not
    # punished as resolution"): a twin expected to resolve nothing may
    # instead yield only `partial` anchors and *nothing else* -- no units,
    # pointers, current state or ambiguity candidates -- without being
    # punished for the status mismatch itself, bounded at the run level by
    # HEDGED_TWINS_CEILING, not blanket-permitted here. `ambiguous` is not a
    # hedge shape at all (spec: "a twin without gold of its own cannot hedge
    # as ambiguous, because naming any ambiguity candidate is itself a false
    # activation"; a twin with its own narrow gold, e.g. T4, is scored on its
    # `ambiguous` expectation directly, never credited as "hedging").
    is_unresolved_expected_twin = fixture.case_id.startswith("T") and fixture.expected_status == "unresolved"
    hedged = (
        is_unresolved_expected_twin
        and observed_status == "partial"
        and not packet.units
        and not packet.pointers
        and not packet.current_state
        and not packet.ambiguity
    )

    # Precision (M1, spec: "computed over every ref the packet surfaces as a
    # resolved anchor, unit or pointer and excluding superseded ancestors the
    # packet credits as marked"): gold hits over the union of resolved
    # anchors, unit refs and pointer refs -- not resolved anchors alone, or
    # padding with dozens of junk *units* (never touching an anchor at all)
    # would be invisible to precision entirely (round-two review: "60 junk
    # units with 2 gold anchors must fail"). A credited superseded ancestor
    # (task 2.3) is excluded from the denominator: deliberately,
    # transparently marking it is correct compiler behaviour, not irrelevant
    # padding, and must not be penalised as if it were.
    precision_denominator_refs = resolved | {unit.ref for unit in packet.units} | {p.ref for p in packet.pointers}
    precision_denominator_refs -= credited
    gold_hit_for_precision = sum(1 for ref in gold_refs if ref in precision_denominator_refs)
    total_precision_denominator = len(precision_denominator_refs)
    precision = (gold_hit_for_precision / total_precision_denominator) if total_precision_denominator else None

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

    # STATEMENT_MAX_CHARS (spec: "a current-state statement of at most 200
    # characters, a longer one failing the case as a packet-contract
    # violation"). Enforced here, over the packet's own field, never by
    # truncating it in `packet_from_dict`: a silent truncation would hide
    # exactly the violation this is meant to catch, and scoring (not
    # loading) is where "fails the case" -- rather than aborting the whole
    # run -- makes contractual sense.
    overlong_statements = tuple(
        entry.anchor
        for entry in packet.current_state
        if entry.statement is not None and len(entry.statement) > STATEMENT_MAX_CHARS
    )

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
    if overlong_statements:
        failure_reasons.append(
            f"packet-contract violation: current_state statement exceeds {STATEMENT_MAX_CHARS} chars "
            f"for {list(overlong_statements)}"
        )

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
        distractor_count=packet.distractor_count,
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
        distractor_count=fixture.distractor_count,
        by_anchor_kind=(),
        blocked=True,
        passed=False,
        failure_reasons=("no packet supplied: run blocked",),
    )


@dataclass(frozen=True)
class PaddingRobustnessResult:
    """C9 (padded tree) vs. C2's own score (unpadded tree) consistency check.

    Round-two N1 revision: compares C9 against C2's *own* unpadded-tree
    score, never T9 (T9 is C2's twin's own turn, restored as an ordinary
    ninth negative twin -- see the fixtures module docstring). A twin
    carrying a different turn cannot show what padding did to the grill
    query's own precision or recall; only the identical query scored on two
    different corpus states can, and C9-vs-C2 is that comparison. C9 is
    meant to pass only when padding neither craters precision nor silently
    drops a gold hit C2 found on the unpadded tree, and only when the two
    scores actually came from different trees in the first place.
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
    """Compare C9's padded-tree score against C2's own unpadded-tree score.

    Spec scenario "Padding comparison refuses packets from one tree", and
    the spec's own "every fixture and every packet SHALL record the corpus
    tree it belongs to": this refuses (naming what is wrong) whenever the
    two packets record the same tree -- ``None`` included, since a missing
    tree is not a third valid state, it is the violation the "every packet"
    requirement exists to catch -- and whenever either side is not exactly
    the tree it is supposed to be (the base side ``BASE_DISTRACTOR_COUNT``,
    the padded side ``DEFAULT_DISTRACTOR_COUNT``). Micro-round finding: a
    prior ``is not None and ...`` guard let ``None`` vs. ``None`` and ``200``
    vs. ``None`` both pass vacuously.
    """

    reasons: list[str] = []
    if padded_score.distractor_count == base_score.distractor_count:
        reasons.append(
            f"padded and base packets record the same corpus tree (distractor_count="
            f"{padded_score.distractor_count!r}); a padding comparison requires two different trees"
        )
    if padded_score.distractor_count != DEFAULT_DISTRACTOR_COUNT:
        reasons.append(
            f"padded packet must record distractor_count={DEFAULT_DISTRACTOR_COUNT!r}, "
            f"got {padded_score.distractor_count!r}"
        )
    if base_score.distractor_count != BASE_DISTRACTOR_COUNT:
        reasons.append(
            f"base packet must record distractor_count={BASE_DISTRACTOR_COUNT!r}, got {base_score.distractor_count!r}"
        )
    if reasons:
        return PaddingRobustnessResult(
            padded_case_id=padded_score.case_id,
            base_case_id=base_score.case_id,
            precision_padded=padded_score.precision,
            recall_padded=_recall(padded_score),
            recall_base=_recall(base_score),
            precision_delta=None,
            passed=False,
            reasons=tuple(reasons),
        )

    recall_padded = _recall(padded_score)
    recall_base = _recall(base_score)
    precision_delta = (
        padded_score.precision - base_score.precision
        if padded_score.precision is not None and base_score.precision is not None
        else None
    )
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

REQUIRED_DIGEST_FIELDS: tuple[str, ...] = (
    "fixture_set_digest",
    "corpus_digest",
    "logical_corpus_digest",
    "threshold_digest",
)


@dataclass(frozen=True)
class RunManifest:
    fixture_set_digest: str
    corpus_digest: str
    logical_corpus_digest: str
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
        logical_corpus_digest=data["logical_corpus_digest"],
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
    """Ceil-rank percentile (``rank = ceil(pct * n)``, ``index = rank - 1``).

    Spec: "percentiles taken ceil-rank so that over the eighteen packets of
    one run the p95 bound is the run's maximum and the hard refusal is
    reached only by larger runs." At the pre-registered run size (eighteen
    fixtures, one packet each), ``rank = ceil(0.95 * 18) = 18``, i.e. p95
    *is* ``max(data)`` -- the p95 ceiling and the hard-refusal cap
    (:data:`TOKEN_HARD_CAP`) are deliberately two separate bounds at n = 18:
    the p95 bound covers every packet in a normal run, while the hard cap is
    a backstop that only starts distinguishing outliers once a run has more
    than eighteen packets (e.g. the n = 5 repeats of task 4). A prior
    nearest-rank approximation under-reported p95 for this small n (minor
    fix).
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
    if "C9" in scores_by_id and "C2" in scores_by_id:
        if not score_padding_robustness(scores_by_id["C9"], scores_by_id["C2"]).passed:
            return False

    return True


def report_to_dict(report: AuditReport) -> dict[str, Any]:
    scores_by_id = {score.case_id: score for score in report.per_case}
    padding_robustness = None
    if "C9" in scores_by_id and "C2" in scores_by_id:
        result = score_padding_robustness(scores_by_id["C9"], scores_by_id["C2"])
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
