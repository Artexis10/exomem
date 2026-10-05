"""First image/dataset/correction journey; actor input is not its oracle.

The gauge and export retain the original display reading. A later user turn
corrects its interpretation, not the measurement event or original bytes.
Rendering a real image is mandatory: a missing image dependency must not
silently turn this into a text-only run. Native initiation and useful later
answers still require observations; generating this fixture proves neither.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from membench.artifacts import image
from membench.schema import ArtifactKind
from membench.templates.base import SourceContent

from . import observation as obs
from .contract import (
    BuiltWorld,
    CaptureCheck,
    FieldIs,
    LaterUse,
    NoNewPlanning,
    PreCapture,
    RecordItem,
    RecordsSeed,
    Result,
    VaultState,
    build_world,
    check_expectations,
    semantics_fingerprint,
    sha256_json,
)

FIXTURE_ID = "memory-loop-mixed-media-correction-v1"
TURNS = (
    "The north vat reading was taken at 06:10 on 20 September 2026. Here's its "
    "gauge photo and the readings export. The batch came out lighter than usual, "
    "but I don't yet know why.",
    "That north vat temperature needs a correction: the gauge reads four degrees "
    "high, so the calibrated temperature was 37 C. It's the same reading, not a "
    "second measurement. I might check the gauge again next week.",
)
LATER_TURN = (
    "What was the calibrated north vat temperature on 20 September, what did the "
    "gauge show, and do we know why the batch came out lighter?"
)
CSV_BYTES = (
    b"read_on,read_at,vat,batch_result\n"
    b"2026-09-20,06:10,north,lighter\n"
    b"2026-09-20,06:15,south,usual\n"
)
GAUGE = SourceContent(
    kind=ArtifactKind.PNG,
    title="North vat gauge",
    lines=["2026-09-20 06:10", "DISPLAY: 41 C"],
)
WORLD = PreCapture(
    records=(
        RecordsSeed(
            key="readings",
            manifest_path="Knowledge Base/Records/Vat Readings/_collection.md",
            exomem_id="50000000-0000-4000-8000-000000000001",
            title="Vat readings",
            claims=("vat readings", "vat temperature", "gauge", "calibration"),
            natural_key=("read_on", "read_at", "vat"),
            fields=(
                ("read_on", "date", True, ()),
                ("read_at", "string", True, ()),
                ("vat", "string", True, ()),
                ("gauge_c", "integer", True, ()),
                ("calibrated_c", "integer", True, ()),
                ("calibration_basis", "string", False, ()),
                ("outcome", "string", False, ()),
                ("cause", "string", False, ()),
                ("sources", "string", False, ()),
            ),
        ),
    ),
)
EXPECTATIONS = (
    RecordItem(
        key="mixed-media/one-corrected-event",
        polarity="positive",
        collection="readings",
        where=(
            ("read_on", FieldIs(equals=("2026-09-20",))),
            ("read_at", FieldIs(equals=("06:10",))),
            ("vat", FieldIs(equals=("north",))),
        ),
        expect=(
            ("gauge_c", FieldIs(equals=("41",))),
            ("calibrated_c", FieldIs(equals=("37",))),
            ("calibration_basis", FieldIs(any_of=("user", "operator", "reported"))),
            ("outcome", FieldIs(tokens=("lighter",))),
            ("cause", FieldIs(empty=True)),
        ),
        reason="One event keeps the display reading and the user-attributed calibration; no cause is established.",
    ),
    NoNewPlanning(
        key="mixed-media/possibility-is-not-a-commitment",
        polarity="negative",
        markers=("gauge",),
        reason="Might check next week is not a Planning commitment.",
    ),
)
LATER_USE = LaterUse(
    useful="The gauge showed 41 C; the user's four-degree correction gives 37 C for the same reading. The lighter batch's cause remains unknown.",
    wrong=("The gauge showed 37 C.", "There were two north vat measurements.", "The lower temperature caused the lighter batch."),
)


def artifact_bytes() -> dict[str, bytes]:
    """Exact real artifacts; no transcript or markdown fallback for the image."""
    return {
        "north-gauge.png": image.render(GAUGE, "synthetic-gauge-input-v1"),
        "vat-readings.csv": CSV_BYTES,
    }


def actor_view(artifacts: dict[str, bytes]) -> dict[str, Any]:
    """The actor sees only user turns and the delivered artifact identities."""
    return {
        "fixture_id": FIXTURE_ID,
        "turns": list(TURNS),
        "later_turn": LATER_TURN,
        "attachments": [
            {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for name, data in sorted(artifacts.items())
        ],
    }


def frozen(artifacts: dict[str, bytes], world: BuiltWorld) -> obs.Frozen:
    """Bind actual artifact bytes and the writer-built world before capture."""
    return obs.Frozen(
        fixture_id=FIXTURE_ID,
        actor_sha256=sha256_json(actor_view(artifacts)),
        pre_capture_sha256=world.logical_sha256,
        evaluator_sha256=sha256_json({
            "semantics": semantics_fingerprint(),
            "expectations": EXPECTATIONS,
            "later_use": LATER_USE,
            "journey_checks": {
                fn.__name__: inspect.getsource(fn)
                for fn in (CaptureReadback, check_state, check_capture, _reading, _correction, _originals)
            },
        }),
        turns_sha256=obs.turns_sha256(TURNS),
        later_turn_sha256=obs.text_sha256(LATER_TURN),
        shipped_prompts=obs.shipped_prompts(),
    )


def build_pre_capture(root: Path) -> BuiltWorld:
    return build_world(root, WORLD, world_id=FIXTURE_ID)


def check_state(world: BuiltWorld, before: VaultState, after: VaultState) -> CaptureCheck:
    """Current fields only; never sufficient for the mixed-media lifecycle."""
    return check_expectations(EXPECTATIONS, world=world.key_to_path, before=before, after=after)


@dataclass(frozen=True)
class CaptureReadback:
    """Actual between-turn/public audit evidence, not actor-authored verdicts.

    The runner retains the earlier row itself: product audit history records
    hashes, not historical field values. ``origin_coverage`` and ``origin_path``
    are optional for a direct row evidence link; when used they must be the
    observed episode coverage and its retained input, not a guessed association.
    """

    root: Path
    between: VaultState
    before_item_hash: str
    after_item_hash: str
    history: Mapping[str, Any]
    preserved: Mapping[str, Mapping[str, Any]]
    origin_path: str | None = None
    origin_coverage: Mapping[str, Any] | None = None


def _reading(world: BuiltWorld, state: VaultState) -> list:
    expectation = EXPECTATIONS[0]
    return [item for item in state.records if item.collection == world.key_to_path["readings"]
        and all(matcher.matches(item.fields.get(field, "")) for field, matcher in expectation.where)]


def _correction(world: BuiltWorld, after: VaultState, proof: CaptureReadback) -> tuple[bool, str]:
    original, current = _reading(world, proof.between), _reading(world, after)
    if (len(original) != 1 or len(current) != 1 or original[0].item_key != current[0].item_key
        or original[0].fields.get("gauge_c") != "41"
        or original[0].fields.get("calibrated_c") == current[0].fields.get("calibrated_c")):
        return False, "The between-turn north reading and its same-item correction were not observed."
    history = proof.history
    if history.get("status") != "ok" or not history.get("complete") or history.get("truncated"):
        return False, "The public agent history does not establish the complete transition chain."
    events = [event for event in history.get("events", ()) if event.get("item_key") == original[0].item_key]
    if (sum(event.get("operation") == "append" for event in events) != 1
        or not proof.before_item_hash or proof.before_item_hash == proof.after_item_hash):
        return False, "No single initial event and changed interpretation were observed."
    cursor, seen, paths = proof.before_item_hash, set(), set()
    while cursor != proof.after_item_hash:
        updates = [event for event in events if event.get("operation") == "update"
            and event.get("before_item_hash") == cursor]
        if len(updates) != 1 or cursor in seen or not updates[0].get("transition_id"):
            return False, "No unique guarded update chain binds the retained earlier row to the corrected row."
        seen.add(cursor)
        paths.add(updates[0].get("canonical_path"))
        cursor = updates[0].get("after_item_hash")
    if len(paths) != 1 or not next(iter(paths)):
        return False, "The correction changed the canonical event identity."
    return True, str(next(iter(paths)))


def _originals(
    world: BuiltWorld, after: VaultState, proof: CaptureReadback,
    artifacts: Mapping[str, bytes], item_path: str,
) -> tuple[bool, str]:
    root = proof.root.resolve()
    if set(artifacts) != {"north-gauge.png", "vat-readings.csv"}:
        return False, "Both declared original artifacts are required."
    source_text = _reading(world, after)[0].fields.get("sources", "")
    if proof.origin_path is not None:
        page = after.pages.get(proof.origin_path)
        coverage = proof.origin_coverage or {}
        bound_ref = f"exomem://memory/{page.frontmatter.get('exomem_id')}" if page else None
        if (page is None or not proof.origin_path.startswith("Knowledge Base/Sources/")
            or coverage.get("input", {}).get("ref") != bound_ref
            or not any(row.get("path") == item_path for row in coverage.get("receipts", ()))):
            return False, "The retained episode input is not bound to this row's effect."
        source_text += " " + page.body
    for name, original in artifacts.items():
        receipt = proof.preserved.get(name)
        if receipt is None:
            return False, f"Original preservation was not observed: {name}."
        path = root / str(receipt.get("stored_path", ""))
        if (not path.resolve().is_relative_to(root / "Knowledge Base/Evidence")
            and not path.resolve().is_relative_to(root / "Knowledge Base/Sources")):
            return False, f"The original has no governed retained path: {name}."
        try:
            retained = path.read_bytes()
        except OSError:
            return False, f"The original cannot be read back: {name}."
        if (retained != original or receipt.get("hash") != hashlib.sha256(original).hexdigest()):
            return False, f"Original bytes or version identity differ: {name}."
        # Direct Evidence references or exact held-input identities in a bound
        # episode Source are valid. Merely retaining two unattached files is not.
        references = (receipt.get("ref"), receipt.get("stored_path"))
        if proof.origin_path is not None:
            references += (receipt.get("file_id"),)
        if not any(ref and str(ref) in source_text for ref in references):
            return False, f"This row has no observed input binding to {name}."
    return True, "Both original artifacts are unchanged and bound to this event."


def check_capture(
    world: BuiltWorld, before: VaultState, after: VaultState, *,
    proof: CaptureReadback | None = None, artifacts: Mapping[str, bytes] | None = None,
) -> CaptureCheck:
    """State, same-event correction history and retained original provenance.

    Missing proof means this lifecycle was not measured, not that a writer
    failed. The existing observation gate separately checks ordinary initiation,
    publication and the fresh answer; use this result, never ``check_state``.
    """
    state = check_state(world, before, after)
    if proof is None or artifacts is None:
        return CaptureCheck((*state.results, Result(
            "mixed-media/lifecycle-evidence", "positive", "fail",
            "Unmeasured: between-turn, correction and original-preservation readbacks are required.",
        )))
    corrected, detail = _correction(world, after, proof)
    retained, provenance = _originals(world, after, proof, artifacts, detail) if corrected else (False, "Correction binding unavailable.")
    return CaptureCheck((*state.results,
        Result("mixed-media/correction-history", "positive", "pass" if corrected else "fail", detail),
        Result("mixed-media/original-provenance", "positive", "pass" if retained else "fail", provenance),
    ))
