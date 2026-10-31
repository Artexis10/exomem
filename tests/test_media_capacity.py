"""The classification rule decides which originals may leave the volume (D12)."""

from __future__ import annotations

from exomem.media_capacity import OFFLOADABLE, RESIDENT, Bounds, age_ciphertext_size

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
