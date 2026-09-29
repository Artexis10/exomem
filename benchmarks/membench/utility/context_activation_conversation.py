"""Scorer and report rows for the `conversation` benchmark group
(``context-activation-conversation-v1``, thread-aware compilation).

A sibling of :mod:`membench.utility.context_activation`, which scores the
digest-pinned English set: nothing here touches that module's scoring or its
report. Like the multilingual scorer it is a pure function of packets (the
product's own dict output), a case and the corpus key maps. No model, no I/O.

One packet is scored per (case, arm). The arms are (a) the turn alone, the
mechanism-removal control, (b) the turn with `recent` and `refs`, (c) the turn
with `focus`, (d) all three. Each row reports, for every anchor kind the case
names, the gold recall and its dual (poison served, other anchors served). There
is NO aggregate: no mean recall, no pooled pass rate. Verdicts are lists of the
cases and kinds that failed a floor, and one count for the control arm.

How a row fails (any one is enough):

* the observed packet status, ``generation.carried_by`` or
  ``generation.disambiguated_by`` differs from the arm's expectation;
* a gold anchor is not served the way the expectation says: at status
  ``resolved`` on a packet that did not abstain, or, for a conversation carry,
  as the single ``partial`` anchor with the expected ``carried_by``;
* a poison key is served (resolved, listed under ambiguity, carried, or the
  source of a unit, pointer or current-state entry), or a must-exclude fact is
  in the packet's text;
* a must-include fact is missing where the expectation serves gold;
* a twin resolves an anchor its case does not list as gold;
* a gold anchor's ``origin`` differs from the expectation. A product that emits
  no ``origin`` at all is reported ``not_emitted`` and does not fail the row:
  the label is a post-change field, and its absence is one recorded fact, not a
  failure per case;
* a drowning case serves ANY material from its conversation's earlier subject.
  That fails the case outright, whatever its recall.

The withheld-versus-absent pair is scored for byte identity on the whole
packet, ``generation`` included (:func:`score_withheld_pair`). The one
exception is the members of the continuity token that no two calls of one
request share (its mint time and its freshly minted thread): the token is
compared by its decoded content, never by its bytes.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from epistemic.corpora.context_activation_conversation import (
    KEY_KINDS,
    POSITIVE_KINDS,
    ConversationCase,
)

from membench.utility.context_activation import (
    TOKEN_HARD_CAP,
    TOKEN_P50_CEILING,
    TOKEN_P95_CEILING,
    _percentile,
)

#: Gold recall floor on every arm that passes a conversation (spec).
RECALL_FLOOR = 0.85
#: The arms that pass a conversation.
CONVERSATION_ARMS: tuple[str, ...] = ("b", "c", "d")
#: The multi-turn kinds the mechanism-removal verdict counts.
_STRIPPED_ARM = "a"
#: The mechanism-removal control is red when at least this share of the
#: multi-turn cases fail with the conversation stripped (spec: "at least half").
MECHANISM_REMOVAL_SHARE = 0.5
#: `generation.carried_by` values that serve a page the turn did not name.
_CARRY_STATUSES = frozenset({"partial", "retrieval_carried", "retrieval_named"})

_ANCHOR_KIND_OF = {"records_collection": "collection"}


@dataclass(frozen=True)
class KindMetrics:
    """One anchor kind's gold recall and its duals, for one case and arm."""

    gold: int
    gold_served: int
    recall: float | None
    poison: int
    poison_served: int
    other_served: int


@dataclass(frozen=True)
class ConversationRow:
    """One (case, arm) row: the arm's expectation, what was observed, why it failed."""

    case_id: str
    arm: str
    group: str
    kind: str
    is_twin: bool
    expected_status: str
    observed_status: str
    expected_carried_by: str | None
    observed_carried_by: str | None
    expected_disambiguated_by: str | None
    observed_disambiguated_by: str | None
    per_kind: dict[str, KindMetrics]
    served: tuple[str, ...]
    poison_served: tuple[str, ...]
    other_resolved: tuple[str, ...]
    origin_expected: dict[str, str]
    origin_observed: dict[str, str | None]
    origin_verdict: str
    generation_conversation: str | None
    drowned: bool
    packet_chars: int
    packet_tokens: int | None
    failure_reasons: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failure_reasons


# --------------------------------------------------------------------------- #
# Reading one packet
# --------------------------------------------------------------------------- #


def observed_status(packet: Mapping[str, Any]) -> str:
    """``resolved``/``partial``/``ambiguous``/``unresolved`` for one packet.

    An abstained packet is ``ambiguous`` when it lists a competing group and
    ``unresolved`` otherwise, whatever partial candidates it names: a partial
    that is not served is not the packet's status.
    """

    if packet.get("ambiguity"):
        return "ambiguous"
    if packet.get("abstained"):
        reason = (packet.get("abstention") or {}).get("reason")
        return "ambiguous" if reason == "ambiguous" else "unresolved"
    statuses = {str(item.get("status")) for item in packet.get("anchors") or ()}
    if "resolved" in statuses:
        return "resolved"
    if statuses & _CARRY_STATUSES:
        return "partial"
    return "unresolved"


def _identity_map(key_to_path: Mapping[str, str], key_to_ref: Mapping[str, str]) -> dict[str, str]:
    """Every identifier a packet may use for a page (vault path or canonical ref) -> key."""

    identity: dict[str, str] = {}
    for key, path in key_to_path.items():
        identity[path] = key
    for key, ref in key_to_ref.items():
        identity[ref] = key
    return identity


def _key_of(identifier: str, identity: Mapping[str, str]) -> str | None:
    if not identifier:
        return None
    base = identifier.split("#", 1)[0]
    return identity.get(identifier) or identity.get(base)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).casefold()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def packet_text(packet: Mapping[str, Any]) -> str:
    """Every text a packet would inject: unit text, current-state statements,
    pointer titles and reasons, anchor titles, missing and ambiguity labels."""

    parts: list[str] = []
    for anchor in packet.get("anchors") or ():
        parts.append(str(anchor.get("title") or ""))
    for unit in packet.get("units") or ():
        parts.append(str(unit.get("text") or ""))
    for pointer in packet.get("pointers") or ():
        parts.append(f"{pointer.get('title') or ''} {pointer.get('why') or ''}")
    for entry in packet.get("current_state") or ():
        parts.append(str(entry.get("statement") or ""))
    for entry in packet.get("ambiguity") or ():
        parts.append(str(entry.get("title") or ""))
    return "\n".join(part for part in parts if part)


def packet_chars(packet: Mapping[str, Any]) -> int:
    return len(packet_text(packet))


def packet_tokens(packet: Mapping[str, Any]) -> int | None:
    """Tokens over the injected text with the audit's frozen encoding, or None
    where the encoding cannot be loaded (it is fetched on first use)."""

    text = packet_text(packet)
    if not text:
        return 0
    try:
        import tiktoken

        return len(tiktoken.get_encoding("o200k_base").encode_ordinary(text))
    except Exception:  # noqa: BLE001 - an offline host has no encoding; that is a fact, not a failure
        return None


def served_keys(packet: Mapping[str, Any], identity: Mapping[str, str]) -> set[str]:
    """Every logical key the packet serves as activated context: resolved
    anchors of a packet that did not abstain, ambiguity candidates, a carried
    partial anchor, and the source of any unit, pointer or current-state entry."""

    found: set[str] = set()
    abstained = bool(packet.get("abstained"))
    carried_by = (packet.get("generation") or {}).get("carried_by")
    for anchor in packet.get("anchors") or ():
        status = anchor.get("status")
        if (status == "resolved" and not abstained) or (
            status in _CARRY_STATUSES and not abstained and carried_by
        ):
            key = _key_of(str(anchor.get("path") or ""), identity) or _key_of(str(anchor.get("ref") or ""), identity)
            if key:
                found.add(key)
    for item in packet.get("ambiguity") or ():
        key = _key_of(str(item.get("ref") or ""), identity)
        if key:
            found.add(key)
    for unit in packet.get("units") or ():
        provenance = unit.get("provenance") or {}
        for identifier in (provenance.get("path"), provenance.get("anchor"), unit.get("ref")):
            key = _key_of(str(identifier or ""), identity)
            if key:
                found.add(key)
    for pointer in packet.get("pointers") or ():
        for identifier in (pointer.get("path"), pointer.get("ref"), pointer.get("anchor")):
            key = _key_of(str(identifier or ""), identity)
            if key:
                found.add(key)
    for entry in packet.get("current_state") or ():
        key = _key_of(str(entry.get("anchor") or ""), identity)
        if key:
            found.add(key)
    return found


def _anchor_for(packet: Mapping[str, Any], key: str, identity: Mapping[str, str]) -> Mapping[str, Any] | None:
    for anchor in packet.get("anchors") or ():
        found = _key_of(str(anchor.get("path") or ""), identity) or _key_of(str(anchor.get("ref") or ""), identity)
        if found == key:
            return anchor
    return None


def _gold_hit(
    packet: Mapping[str, Any], key: str, expectation: Mapping[str, Any], identity: Mapping[str, str]
) -> bool:
    """A gold anchor is served the way the expectation says it is."""

    anchor = _anchor_for(packet, key, identity)
    if anchor is None or packet.get("abstained") and not packet.get("ambiguity") and expectation["status"] != "unresolved":
        return False
    if packet.get("abstained"):
        return False
    if expectation["carried_by"]:
        return anchor.get("status") in _CARRY_STATUSES and (packet.get("generation") or {}).get(
            "carried_by"
        ) == expectation["carried_by"]
    return anchor.get("status") == "resolved"


# --------------------------------------------------------------------------- #
# Scoring one (case, arm)
# --------------------------------------------------------------------------- #


def score_conversation_case(
    case: ConversationCase,
    arm: str,
    packet: Mapping[str, Any],
    *,
    key_to_path: Mapping[str, str],
    key_to_ref: Mapping[str, str],
    origin_emitted: bool | None = None,
) -> ConversationRow:
    """Score one case's packet on one arm against the arm's expectation.

    ``origin_emitted``: whether the product labels anchors with ``origin`` at
    all (default: read off this packet). The runner passes one value for the
    whole run so a case with no anchors is not mistaken for a product without
    the label.
    """

    if arm not in case.arms:
        raise ValueError(f"{case.case_id} is not scored on arm {arm!r}")
    expectation = case.expectation(arm)
    identity = _identity_map(key_to_path, key_to_ref)
    generation = packet.get("generation") or {}
    reasons: list[str] = []

    status = observed_status(packet)
    if status != expectation["status"]:
        reasons.append(f"status {status} != {expectation['status']}")
    carried = generation.get("carried_by")
    if carried != expectation["carried_by"]:
        reasons.append(f"carried_by {carried!r} != {expectation['carried_by']!r}")
    disambiguated = generation.get("disambiguated_by")
    if disambiguated != expectation["disambiguated_by"]:
        reasons.append(f"disambiguated_by {disambiguated!r} != {expectation['disambiguated_by']!r}")

    served = served_keys(packet, identity)
    gold, poison = set(case.gold), set(case.poison)
    hits = {key for key in gold if _gold_hit(packet, key, expectation, identity)}
    expects_gold_served = expectation["status"] in {"resolved", "partial"}
    missing = sorted(gold - hits) if expects_gold_served else []
    if missing:
        reasons.append(f"gold not served: {missing}")
    served_poison = sorted(poison & served)
    text = _normalize(packet_text(packet))
    leaked = sorted(phrase for phrase in case.must_exclude if _normalize(phrase) in text)
    if served_poison:
        reasons.append(f"poison served: {served_poison}")
    if leaked:
        reasons.append(f"must-exclude fact present: {leaked}")
    if expects_gold_served:
        absent = sorted(phrase for phrase in case.must_include if _normalize(phrase) not in text)
        if absent:
            reasons.append(f"must-include fact missing: {absent}")

    other_resolved: list[str] = []
    for anchor in packet.get("anchors") or ():
        if anchor.get("status") != "resolved" or packet.get("abstained"):
            continue
        key = _key_of(str(anchor.get("path") or ""), identity) or _key_of(str(anchor.get("ref") or ""), identity)
        label = key or f"unmapped:{anchor.get('title')}"
        if label not in gold:
            other_resolved.append(label)
    is_twin = case.kind.endswith("_twin")
    if is_twin and other_resolved:
        reasons.append(f"twin false activation: {sorted(other_resolved)}")

    drowned = case.kind == "drowning" and bool(served_poison or leaked)
    if drowned:
        reasons.append("drowning: served material from the conversation's earlier subject")

    # Origin: compared only where the product emits the label.
    emitted = (
        origin_emitted
        if origin_emitted is not None
        else any("origin" in anchor for anchor in packet.get("anchors") or ())
    )
    expected_origin = dict(expectation["origin"])
    observed_origin: dict[str, str | None] = {}
    for key in expected_origin:
        anchor = _anchor_for(packet, key, identity)
        observed_origin[key] = None if anchor is None else anchor.get("origin")
    if not expected_origin:
        origin_verdict = "not_applicable"
    elif not emitted:
        origin_verdict = "not_emitted"
    else:
        wrong = sorted(key for key, want in expected_origin.items() if observed_origin.get(key) != want)
        origin_verdict = "mismatch" if wrong else "match"
        if wrong:
            reasons.append(f"origin: {{{', '.join(f'{k}: {observed_origin.get(k)!r} != {expected_origin[k]!r}' for k in wrong)}}}")

    return ConversationRow(
        case_id=case.case_id,
        arm=arm,
        group=case.group,
        kind=case.kind,
        is_twin=is_twin,
        expected_status=expectation["status"],
        observed_status=status,
        expected_carried_by=expectation["carried_by"],
        observed_carried_by=carried,
        expected_disambiguated_by=expectation["disambiguated_by"],
        observed_disambiguated_by=disambiguated,
        per_kind=_per_kind(case, hits, served, other_resolved),
        served=tuple(sorted(served)),
        poison_served=tuple(served_poison),
        other_resolved=tuple(sorted(other_resolved)),
        origin_expected=expected_origin,
        origin_observed=observed_origin,
        origin_verdict=origin_verdict,
        generation_conversation=generation.get("conversation"),
        drowned=drowned,
        packet_chars=packet_chars(packet),
        packet_tokens=packet_tokens(packet),
        failure_reasons=tuple(reasons),
    )


def _kind_of(key: str) -> str:
    kind = KEY_KINDS.get(key, "unknown")
    return _ANCHOR_KIND_OF.get(kind, kind)


def _per_kind(
    case: ConversationCase, hits: set[str], served: set[str], other_resolved: Sequence[str]
) -> dict[str, KindMetrics]:
    """Gold recall and its duals per anchor kind. No key is pooled across kinds."""

    kinds = sorted({_kind_of(key) for key in (*case.gold, *case.poison)})
    out: dict[str, KindMetrics] = {}
    for kind in kinds:
        gold = [key for key in case.gold if _kind_of(key) == kind]
        poison = [key for key in case.poison if _kind_of(key) == kind]
        served_gold = [key for key in gold if key in hits]
        out[kind] = KindMetrics(
            gold=len(gold),
            gold_served=len(served_gold),
            recall=(len(served_gold) / len(gold)) if gold else None,
            poison=len(poison),
            poison_served=len([key for key in poison if key in served]),
            other_served=len([key for key in other_resolved if _kind_of(key) == kind]),
        )
    return out


# --------------------------------------------------------------------------- #
# The withheld-versus-absent pair
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WithheldPairRow:
    """The pair's byte-identity verdict on one arm."""

    withheld_case: str
    absent_case: str
    arm: str
    identical: bool
    differs_in: tuple[str, ...]


#: The members of a decoded continuity token that differ between two calls of
#: one request: the mint time, the keyless thread minted anew per call, its
#: time, and the MAC that signs them. Everything else in the token (the refs it
#: names, the roles, the generation it was minted against) is the packet's own
#: content and is compared.
VOLATILE_TOKEN_MEMBERS: frozenset[str] = frozenset({"minted_ns", "thread", "thread_ns", "mac"})


def _decode_token(token: str) -> dict[str, Any] | None:
    try:
        payload = token.split(".", 1)[0]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _canonical(packet: Mapping[str, Any]) -> str:
    """The packet as JSON, with the continuity token replaced by its decoded
    content minus :data:`VOLATILE_TOKEN_MEMBERS`. Two calls of one request never
    return the same token bytes (the token carries its own mint time and a fresh
    thread), so byte identity is over everything the token means."""

    data = dict(packet)
    token = data.get("continuity")
    if isinstance(token, str) and token:
        decoded = _decode_token(token)
        if decoded is not None:
            data["continuity"] = {
                key: value for key, value in decoded.items() if key not in VOLATILE_TOKEN_MEMBERS
            }
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def score_withheld_pair(
    withheld_case: ConversationCase,
    absent_case: ConversationCase,
    arm: str,
    withheld_packet: Mapping[str, Any],
    absent_packet: Mapping[str, Any],
) -> WithheldPairRow:
    """Byte identity of the two packets: the JSON of each, ``generation``
    included, must be equal. ``differs_in`` names the top-level fields (and
    ``generation`` members) that differ when it is not."""

    identical = _canonical(withheld_packet) == _canonical(absent_packet)
    differs: list[str] = []
    if not identical:
        for key in sorted(set(withheld_packet) | set(absent_packet)):
            left, right = withheld_packet.get(key), absent_packet.get(key)
            if _canonical({key: left}) == _canonical({key: right}):
                continue
            if key == "generation" and isinstance(left, Mapping) and isinstance(right, Mapping):
                differs.extend(
                    f"generation.{member}"
                    for member in sorted(set(left) | set(right))
                    if json.dumps(left.get(member), sort_keys=True) != json.dumps(right.get(member), sort_keys=True)
                )
            else:
                differs.append(key)
    return WithheldPairRow(
        withheld_case=withheld_case.case_id,
        absent_case=absent_case.case_id,
        arm=arm,
        identical=identical,
        differs_in=tuple(differs),
    )


# --------------------------------------------------------------------------- #
# Verdicts (no aggregate)
# --------------------------------------------------------------------------- #


def arm_floors(rows: Iterable[ConversationRow], arm: str) -> dict[str, Any]:
    """The pre-registered floors for one arm that passes a conversation.

    Every entry is a list of the cases (and kinds) that failed; nothing is
    averaged. ``passed`` is True when every list is empty and the size bounds
    hold.
    """

    arm_rows = [row for row in rows if row.arm == arm]
    recall = sorted(
        (row.case_id, kind)
        for row in arm_rows
        if row.expected_status in {"resolved", "partial"}
        for kind, metrics in row.per_kind.items()
        if metrics.recall is not None and metrics.recall < RECALL_FLOOR
    )
    poison = sorted(row.case_id for row in arm_rows if row.poison_served)
    twins = sorted(row.case_id for row in arm_rows if row.is_twin and row.other_resolved)
    drowning = sorted(row.case_id for row in arm_rows if row.drowned)
    tokens = sorted(value for value in (row.packet_tokens for row in arm_rows) if value is not None)
    p50, p95 = _percentile([float(v) for v in tokens], 0.50), _percentile([float(v) for v in tokens], 0.95)
    over_cap = sorted(row.case_id for row in arm_rows if (row.packet_tokens or 0) > TOKEN_HARD_CAP)
    size_ok = (
        not over_cap
        and (p50 is None or p50 <= TOKEN_P50_CEILING)
        and (p95 is None or p95 <= TOKEN_P95_CEILING)
    )
    return {
        "arm": arm,
        "cases": len(arm_rows),
        "recall_below_floor": [{"case_id": case, "kind": kind} for case, kind in recall],
        "poison_served": poison,
        "twin_false_activation": twins,
        "drowning_failures": drowning,
        "packet_size": {
            "tokens_measured": len(tokens),
            "p50": p50,
            "p95": p95,
            "over_hard_cap": over_cap,
            "within_bounds": size_ok,
        },
        "passed": not (recall or poison or twins or drowning) and size_ok,
    }


def mechanism_removal(rows: Iterable[ConversationRow]) -> dict[str, Any]:
    """The arm-(a) verdict: the mechanism is needed when at least half of the
    multi-turn cases fail with the conversation stripped from every request."""

    multi = [
        row
        for row in rows
        if row.arm == _STRIPPED_ARM and row.group == "multi_turn" and row.kind in POSITIVE_KINDS
    ]
    failed = sorted(row.case_id for row in multi if not row.passed)
    return {
        "arm": _STRIPPED_ARM,
        "multi_turn_cases": len(multi),
        "failed": failed,
        "failed_count": len(failed),
        "red": bool(multi) and len(failed) >= MECHANISM_REMOVAL_SHARE * len(multi),
    }


def incident_classes(rows: Iterable[ConversationRow]) -> dict[str, list[str]]:
    """Which cases fail on the control arm, by the incident class they encode."""

    failing: dict[str, list[str]] = {}
    for row in rows:
        if row.arm != _STRIPPED_ARM or row.passed or row.is_twin:
            continue
        failing.setdefault(row.kind, []).append(row.case_id)
    return {kind: sorted(ids) for kind, ids in sorted(failing.items())}


def row_to_dict(row: ConversationRow, *, with_tokens: bool = False) -> dict[str, Any]:
    """A deterministic dict of one row. Token counts depend on a downloaded
    encoding, so they are opt-in; character counts are always present."""

    data: dict[str, Any] = {
        "case_id": row.case_id,
        "arm": row.arm,
        "kind": row.kind,
        "passed": row.passed,
        "expected": {
            "status": row.expected_status,
            "carried_by": row.expected_carried_by,
            "disambiguated_by": row.expected_disambiguated_by,
            "origin": row.origin_expected,
        },
        "observed": {
            "status": row.observed_status,
            "carried_by": row.observed_carried_by,
            "disambiguated_by": row.observed_disambiguated_by,
            "origin": row.origin_observed,
            "generation_conversation": row.generation_conversation,
        },
        "origin_verdict": row.origin_verdict,
        "served": list(row.served),
        "poison_served": list(row.poison_served),
        "other_resolved": list(row.other_resolved),
        "per_anchor_kind": {
            kind: {
                "gold": metrics.gold,
                "gold_served": metrics.gold_served,
                "recall": metrics.recall,
                "poison": metrics.poison,
                "poison_served": metrics.poison_served,
                "other_served": metrics.other_served,
            }
            for kind, metrics in sorted(row.per_kind.items())
        },
        "drowned": row.drowned,
        "packet_chars": row.packet_chars,
        "failure_reasons": list(row.failure_reasons),
    }
    if with_tokens:
        data["packet_tokens"] = row.packet_tokens
    return data


__all__ = [
    "CONVERSATION_ARMS",
    "MECHANISM_REMOVAL_SHARE",
    "RECALL_FLOOR",
    "ConversationRow",
    "KindMetrics",
    "VOLATILE_TOKEN_MEMBERS",
    "WithheldPairRow",
    "arm_floors",
    "incident_classes",
    "mechanism_removal",
    "observed_status",
    "packet_chars",
    "packet_text",
    "packet_tokens",
    "row_to_dict",
    "score_conversation_case",
    "score_withheld_pair",
    "served_keys",
]
