"""The classification rule decides which originals may leave the volume (D12)."""

from __future__ import annotations

import ast
from pathlib import Path

from exomem import media_capacity
from exomem.media_capacity import OFFLOADABLE, RESIDENT, Bounds, age_ciphertext_size

CELLCTL_MANIFESTS = Path(__file__).resolve().parents[1] / "infra" / "cellctl" / "src" / "cellctl" / "manifests.py"

GiB = 1024 ** 3
MiB = 1024 ** 2


def test_a_file_of_exactly_the_object_bound_is_resident():
    """Catches a rule that compares the plaintext, not the ciphertext, with the store's object bound."""
    bounds = Bounds(object_bytes=5 * GiB, hydration_bytes=6 * GiB, volume_bytes=40 * GiB)

    assert bounds.classify(5 * GiB) == RESIDENT
    assert age_ciphertext_size(bounds.largest_offloadable()) <= 5 * GiB
    assert bounds.classify(bounds.largest_offloadable()) == OFFLOADABLE


def test_a_hydration_capacity_below_the_ciphertext_makes_a_file_resident():
    """Catches a rule that lets offload reserve a ciphertext temporary file larger than the capacity."""
    bounds = Bounds(object_bytes=None, hydration_bytes=GiB, volume_bytes=40 * GiB)

    assert bounds.classify(GiB) == RESIDENT
    assert bounds.classify(GiB - 2 * MiB) == OFFLOADABLE


def test_on_a_12_gib_volume_a_4_gib_file_is_resident_and_3_gib_is_the_largest():
    """Catches a rule that ignores the volume fraction, which keeps fixed budget terms near half the volume."""
    bounds = Bounds(object_bytes=5 * GiB + 2 * MiB, hydration_bytes=5 * GiB + GiB // 4, volume_bytes=12 * GiB)

    assert bounds.classify(4 * GiB) == RESIDENT
    assert bounds.largest_offloadable() == 3 * GiB


def test_cellctl_and_the_cell_name_the_same_hydration_keys():
    """Catches a key added on one side only: cellctl renders a policy that the cell reads as no capacity, or the reverse."""
    # cellctl cannot import the cell and the cell cannot import cellctl, so this reads cellctl's constant from its source.
    assignments = [node for node in ast.parse(CELLCTL_MANIFESTS.read_text(encoding="utf-8")).body
                   if isinstance(node, ast.Assign) and [t.id for t in node.targets if isinstance(t, ast.Name)] == ["MEDIA_HYDRATION_KEYS"]]
    assert len(assignments) == 1

    assert ast.literal_eval(assignments[0].value) == media_capacity._HYDRATION_KEYS
