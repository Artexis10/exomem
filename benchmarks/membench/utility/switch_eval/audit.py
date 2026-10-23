"""The arm-guess audit: the guesses, the re-judge set and its standing verdicts.

Design, "Arm-guess audit": after judging, the recovery path and the
tie-break, a separate Opus lane reads exactly the judge view of every
original item, and never a verdict, a key or the arm key, and guesses the arm
through a structured-output schema. Every item guessed right on evidence that
describes tools or the retrieval process is re-judged with that span removed,
mixed in with as many controls, unchanged copies of original items drawn from
all of them, by every standing judge route with the judge it judged with.
Scoring then recomputes each rule with the re-judged verdicts on the flagged
items (``score.score_run``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from membench.judge.blinding import deterministic_permutation, shuffled

from . import protocol
from .judge import (
    JudgeError,
    Unblinded,
    UnblindingError,
    _opaque,
    _secret,
    judge_manifest,
    judge_prompt,
    load_answers,
    read_items,
    run_judge,
    run_structured,
    tie_break,
)
from .state import Layout

#: The audit lane is an Opus lane, so it runs on the Claude route.
AUDIT_ROUTE = "claude"
FLAGGED = "flagged"
CONTROL = "control"


def audit_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["item_id", "arm_guess", "confidence", "evidence_span", "evidence_kind"],
        "properties": {
            "item_id": {"type": "string"},
            "arm_guess": {"type": "string", "enum": list(protocol.ARM_IDS)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence_span": {"type": ["string", "null"]},
            "evidence_kind": {"type": "string", "enum": list(protocol.AUDIT_EVIDENCE_KINDS)},
        },
    }


def audit_prompt(item: Mapping) -> str:
    return judge_prompt(item, text="audit.md")


def validate_guess(item: Mapping, guess: object) -> dict:
    """The guess, checked against its item. A span must be quoted exactly from
    the answer, because the re-judge removes it from the answer."""

    if not isinstance(guess, dict) or guess.get("item_id") != item["item_id"]:
        raise JudgeError("the guess does not name its item")
    if guess.get("arm_guess") not in protocol.ARM_IDS:
        raise JudgeError("the arm guess is outside the schema")
    confidence = guess.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise JudgeError("the guess has no confidence")
    if not 0 <= confidence <= 1:
        raise JudgeError("the guess's confidence is outside 0..1")
    kind = guess.get("evidence_kind")
    if kind not in protocol.AUDIT_EVIDENCE_KINDS:
        raise JudgeError("the evidence kind is outside the schema")
    span = guess.get("evidence_span")
    if kind == protocol.AUDIT_NO_BASIS:
        span = None
    elif not (isinstance(span, str) and span.strip() and span in item["answer"]):
        raise JudgeError("a guess with a basis needs a span quoted exactly from the answer")
    return {
        "item_id": item["item_id"],
        "arm_guess": guess["arm_guess"],
        "confidence": float(confidence),
        "evidence_span": span,
        "evidence_kind": kind,
    }


def original_items(layout: Layout, unblinded: Unblinded) -> list[dict]:
    """The judge view of every original item, without the re-judged copies."""

    if not isinstance(unblinded, Unblinded):
        raise UnblindingError("the arm-guess audit runs only after unblinding")
    return [
        item for item in read_items(layout) if not unblinded.arm_key[item["item_id"]]["rejudge_of"]
    ]


def run_guesses(
    layout: Layout,
    unblinded: Unblinded,
    *,
    model: str,
    effort: str | None,
    claude: str,
    launcher: Sequence[str],
) -> dict:
    """The audit lane's guess on every original item; resumable item by item."""

    items = original_items(layout, unblinded)
    layout.audit.mkdir(parents=True, exist_ok=True)
    manifest = layout.audit / "guesser.json"
    record = {"route": AUDIT_ROUTE, "model": model, "effort": effort}
    if manifest.exists() and json.loads(manifest.read_text(encoding="utf-8")) != record:
        raise JudgeError("the audit was started with another lane; one lane guesses every item")
    manifest.write_text(json.dumps(record, indent=2), encoding="utf-8")
    guessed = run_structured(
        items,
        route=AUDIT_ROUTE,
        model=model,
        effort=effort,
        schema=audit_schema(),
        prompt_for=audit_prompt,
        validate=validate_guess,
        out_dir=layout.audit / "guesses",
        sandbox=layout.audit / "sandbox",
        claude=claude,
        launcher=launcher,
    )
    return {"guessed_now": guessed, "items": len(items)}


def load_guesses(layout: Layout, unblinded: Unblinded) -> dict[str, dict]:
    """Every original item's guess by item id; refused while any is missing."""

    expected = {item["item_id"] for item in original_items(layout, unblinded)}
    guesses = load_answers(layout.audit / "guesses")
    if set(guesses) != expected:
        raise UnblindingError(
            f"the arm-guess audit has guessed {len(set(guesses) & expected)} of {len(expected)} items"
        )
    return guesses


def rejudge_set(
    layout: Layout,
    unblinded: Unblinded,
    guesses: Mapping[str, Mapping],
    *,
    seed: int,
    inputs_digest: str,
) -> tuple[list[dict], dict[str, dict]]:
    """The flagged items with their span removed and as many controls as they
    are, shuffled together under fresh ids, with their key. Each id carries a
    digest of the item's content, so a changed span gives a new id. Controls are
    unchanged copies drawn from every original item, flagged ones included, so
    a run whose every item is flagged still has its noise baseline."""

    secret = _secret(seed, inputs_digest)
    originals = {item["item_id"]: item for item in original_items(layout, unblinded)}
    flagged = sorted(
        item_id
        for item_id, guess in guesses.items()
        if guess["evidence_kind"] == protocol.AUDIT_FLAGGED_KIND
        and guess["arm_guess"] == unblinded.arm_key[item_id]["arm"]
    )
    pool = sorted(originals)
    chosen = deterministic_permutation(len(pool), f"{secret}:audit-controls")[: len(flagged)]
    controls = [pool[index] for index in chosen]
    items: list[dict] = []
    key: dict[str, dict] = {}
    for role, item_ids in ((FLAGGED, flagged), (CONTROL, controls)):
        for item_id in item_ids:
            entry = unblinded.arm_key[item_id]
            view = {k: v for k, v in originals[item_id].items() if k != "item_id"}
            if role == FLAGGED:
                view["answer"] = view["answer"].replace(guesses[item_id]["evidence_span"], "")
            content = hashlib.sha256(_canonical(view).encode("utf-8")).hexdigest()
            fresh = _opaque(secret, "audit", role, entry["session_id"], content)
            items.append({**view, "item_id": fresh})
            key[fresh] = {**entry, "rejudge_of": item_id, "role": role}
    return list(shuffled(items, f"{secret}:audit-order")), key


def _canonical(item: Mapping) -> str:
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def _items_text(items: Sequence[Mapping]) -> str:
    return "".join(_canonical(item) + "\n" for item in items)


def write_rejudge_set(
    layout: Layout, unblinded: Unblinded, *, seed: int, inputs_digest: str
) -> list[dict]:
    """Write the re-judge set once; a stored set must match the one the guesses rebuild."""

    items, key = rejudge_set(
        layout, unblinded, load_guesses(layout, unblinded), seed=seed, inputs_digest=inputs_digest
    )
    items_path = layout.audit / "items.jsonl"
    key_path = layout.unblinding / "audit-key.json"
    text = _items_text(items)
    if items_path.exists() or key_path.exists():
        stored_key = json.loads(key_path.read_text(encoding="utf-8")) if key_path.exists() else None
        if items_path.read_text(encoding="utf-8") != text or stored_key != key:
            raise UnblindingError("the stored re-judge set differs from the one the guesses build")
        return items
    items_path.write_text(text, encoding="utf-8")
    key_path.write_text(json.dumps(key, indent=2, sort_keys=True), encoding="utf-8")
    return items


def run_rejudge(
    layout: Layout,
    unblinded: Unblinded,
    items: Sequence[Mapping],
    *,
    claude: str,
    launcher: Sequence[str],
) -> dict:
    """Each standing route re-judges the set with the judge of its first pass."""

    done = {}
    for route in unblinded.routes:
        judge = judge_manifest(layout, route)
        done[route] = run_judge(
            layout,
            route=route,
            model=judge["model"],
            effort=judge["effort"],
            claude=claude,
            launcher=launcher,
            items=items,
            out_dir=layout.audit / "verdicts" / route,
        )["judged_now"]
    return done


def load_audit(
    layout: Layout, unblinded: Unblinded, *, seed: int, inputs_digest: str
) -> dict | None:
    """What scoring reads from the audit, by session id: each guess, and the
    standing re-judged verdict of each flagged and control item. ``None`` when
    the audit has not run; refused when it is partial or does not rebuild.
    The stored re-judge items must be the ones the guesses rebuild, removed
    spans included, before any cached verdict on them is read."""

    if not layout.audit.exists():
        return None
    guesses = load_guesses(layout, unblinded)
    items, key = rejudge_set(layout, unblinded, guesses, seed=seed, inputs_digest=inputs_digest)
    items_path = layout.audit / "items.jsonl"
    if not items_path.exists():
        raise UnblindingError("the audit has no re-judge items: its verdicts judged nothing known")
    if items_path.read_text(encoding="utf-8") != _items_text(items):
        raise UnblindingError("the stored re-judge items differ from the ones the guesses build")
    key_path = layout.unblinding / "audit-key.json"
    if not key_path.exists() or json.loads(key_path.read_text(encoding="utf-8")) != key:
        raise UnblindingError("the stored audit key differs from the one the guesses build")
    by_route = [load_answers(layout.audit / "verdicts" / route) for route in unblinded.routes]
    for route, verdicts in zip(unblinded.routes, by_route, strict=True):
        missing = set(key) - set(verdicts)
        if missing:
            raise UnblindingError(f"route {route} has not re-judged {len(missing)} audit items")
    # The audit key passed the same rebuild check as the arm key, so the
    # tie-break may read its arms.
    audit_unblinded = Unblinded(arm_key=key, routes=unblinded.routes)
    rejudged: dict[str, dict[str, dict]] = {FLAGGED: {}, CONTROL: {}}
    for item_id, entry in key.items():
        verdict = by_route[0][item_id]
        if len(by_route) == 2:
            second = {**by_route[1][item_id], "item_id": item_id}
            verdict = tie_break({**verdict, "item_id": item_id}, second, unblinded=audit_unblinded)
        rejudged[entry["role"]][entry["session_id"]] = verdict
    return {
        "guesses": {
            unblinded.arm_key[item_id]["session_id"]: guess for item_id, guess in guesses.items()
        },
        "flagged": rejudged[FLAGGED],
        "controls": rejudged[CONTROL],
    }
