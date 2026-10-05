"""Mixed-media fixture/oracle checks, not ordinary-agent acceptance."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from epistemic.memory_loop import contract
from epistemic.memory_loop import mixed_media as fx
from epistemic.memory_loop.contract import (
    BuiltWorld,
    RecordView,
    VaultState,
    find_nudges,
    sha256_json,
)


def test_image_unavailability_cannot_be_reported_as_multimodal_input(monkeypatch) -> None:
    # A text fallback would turn a vision failure into an apparent lifecycle pass.
    def unavailable(*_args):
        raise fx.image.ImageUnavailable("fixture image dependency unavailable")

    monkeypatch.setattr(fx.image, "render", unavailable)
    with pytest.raises(fx.image.ImageUnavailable):
        fx.artifact_bytes()


def test_actor_binding_changes_when_the_image_changes_and_hides_the_oracle() -> None:
    # Artifact bytes are part of the frozen input, not just a filename beside a turn.
    original = {"north-gauge.png": b"first image", "vat-readings.csv": fx.CSV_BYTES}
    changed = {**original, "north-gauge.png": b"different image"}
    view = fx.actor_view(original)
    assert sha256_json(view) != sha256_json(fx.actor_view(changed))
    assert "calibration_basis" not in repr(view)
    assert "mixed-media/one-corrected-event" not in repr(view)
    assert all(not find_nudges(turn) for turn in (*fx.TURNS, fx.LATER_TURN))


def test_evaluator_binding_changes_when_unknown_cause_semantics_change(monkeypatch) -> None:
    # A changed abstention rule must void an old run instead of silently rescoring it.
    world = BuiltWorld(
        world_id=fx.FIXTURE_ID,
        spec_sha256=sha256_json(fx.WORLD),
        key_to_path={"readings": fx.WORLD.records[0].manifest_path},
        logical_sha256="a" * 64,
    )
    artifacts = {"north-gauge.png": b"image", "vat-readings.csv": fx.CSV_BYTES}
    original = fx.frozen(artifacts, world).evaluator_sha256
    monkeypatch.setattr(contract, "_EMPTY_VALUES", contract._EMPTY_VALUES | {"temperature caused it"})
    assert fx.frozen(artifacts, world).evaluator_sha256 != original


def test_correction_oracle_rejects_duplicate_events_and_rewritten_display_values() -> None:
    # These are evaluator sensitivity controls, not evidence that any actor captured.
    path = fx.WORLD.records[0].manifest_path
    world = BuiltWorld(
        world_id=fx.FIXTURE_ID,
        spec_sha256=sha256_json(fx.WORLD),
        key_to_path={"readings": path},
        logical_sha256="a" * 64,
    )
    fields = {
        "read_on": "2026-09-20",
        "read_at": "06:10",
        "vat": "north",
        "gauge_c": "41",
        "calibrated_c": "37",
        "calibration_basis": "User reported a four-degree calibration correction.",
        "outcome": "lighter batch",
        "cause": "unknown",
    }
    item = RecordView(collection=path, item_key="reading-1", fields=fields)
    before = VaultState(pages={}, records=())
    correct = VaultState(pages={}, records=(item,))
    # Correct-looking current fields alone prove neither preservation nor correction.
    assert not fx.check_capture(world, before, correct).accepted
    assert fx.check_state(world, before, correct).accepted
    wrong_display = replace(item, fields={**fields, "gauge_c": "37"})
    assert not fx.check_state(world, before, replace(correct, records=(wrong_display,))).accepted
    duplicate = replace(item, item_key="reading-2")
    assert not fx.check_state(world, before, replace(correct, records=(item, duplicate))).accepted


@pytest.fixture
def correction_case(tmp_path: Path):
    manifest = fx.WORLD.records[0].manifest_path
    world = BuiltWorld(fx.FIXTURE_ID, sha256_json(fx.WORLD), {"readings": manifest}, "a" * 64)
    artifacts = {"north-gauge.png": b"image bytes", "vat-readings.csv": fx.CSV_BYTES}
    preserved = {}
    for number, (name, content) in enumerate(artifacts.items(), 1):
        path = tmp_path / "Knowledge Base/Evidence/Test" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        preserved[name] = {"stored_path": path.relative_to(tmp_path).as_posix(),
            "hash": hashlib.sha256(content).hexdigest(),
            "ref": f"exomem://memory/70000000-0000-4000-8000-{number:012d}"}
    item = RecordView(manifest, "north-0610", {
        "read_on": "2026-09-20", "read_at": "06:10", "vat": "north", "gauge_c": "41",
        "calibrated_c": "37", "calibration_basis": "User reported four degrees high.",
        "outcome": "lighter", "cause": "unknown",
        "sources": " ".join(row["ref"] for row in preserved.values()),
    })
    initial = replace(item, fields={**item.fields, "calibrated_c": "", "calibration_basis": ""})
    empty = VaultState({}, ())
    current = VaultState({}, (item,))
    item_path = "Knowledge Base/Records/Vat Readings/Items/north-0610.md"
    history = {"status": "ok", "complete": True, "truncated": False, "events": [
        {"operation": "append", "item_key": item.item_key, "after_item_hash": "before"},
        {"operation": "update", "item_key": item.item_key, "before_item_hash": "before",
            "after_item_hash": "after", "transition_id": "correction", "canonical_path": item_path},
    ]}
    proof = fx.CaptureReadback(tmp_path, VaultState({}, (initial,)), "before", "after", history, preserved)
    return world, empty, current, proof, artifacts


def test_capture_requires_original_binding_and_same_event_update_history(correction_case) -> None:
    # A bare final row, detached originals, or a new append must not count as correction.
    world, empty, current, proof, artifacts = correction_case
    item, history, preserved = current.records[0], proof.history, proof.preserved
    assert fx.check_capture(world, empty, current, proof=proof, artifacts=artifacts).accepted
    detached = replace(item, fields={**item.fields, "sources": ""})
    assert not fx.check_capture(world, empty, VaultState({}, (detached,)), proof=proof, artifacts=artifacts).accepted
    second_append = {**history, "events": [history["events"][0],
        {**history["events"][1], "operation": "append"}]}
    assert not fx.check_capture(world, empty, current, proof=replace(proof, history=second_append), artifacts=artifacts).accepted
    (proof.root / preserved["north-gauge.png"]["stored_path"]).write_bytes(b"rewritten")
    assert not fx.check_capture(world, empty, current, proof=proof, artifacts=artifacts).accepted


def test_another_event_cannot_lend_its_sources_to_the_corrected_reading(correction_case) -> None:
    # Preserved files attached to yesterday's event do not ground this event.
    world, empty, current, proof, artifacts = correction_case
    item = current.records[0]
    other = replace(item, item_key="other-day", fields={**item.fields, "read_on": "2026-09-21"})
    target = replace(item, fields={**item.fields, "sources": ""})
    after = replace(current, records=(target, other))
    assert fx.check_state(world, empty, after).accepted
    assert not fx.check_capture(world, empty, after, proof=proof, artifacts=artifacts).accepted


def test_a_different_event_cannot_take_the_corrected_readings_place(correction_case) -> None:
    # Moving the original aside and inserting a new key is not a same-event correction.
    world, empty, current, proof, artifacts = correction_case
    item = current.records[0]
    moved = replace(item, fields={**item.fields, "vat": "south"})
    replacement = replace(item, item_key="replacement-event")
    after = replace(current, records=(moved, replacement))
    assert fx.check_state(world, empty, after).accepted
    assert not fx.check_capture(world, empty, after, proof=proof, artifacts=artifacts).accepted
