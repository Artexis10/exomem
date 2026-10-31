"""The one classification rule for media originals: text, offloadable artifact or resident artifact (D12).

The rule is pure arithmetic over configured bounds, so the CLI, the adoption
inventory, offload and eviction all import it and no caller keeps a copy.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

# These three classes are the closed set that design D12 fixes; no caller extends them.
TEXT = "text"
OFFLOADABLE = "offloadable"
RESIDENT = "resident"

# age's STREAM format fixes the chunk and tag sizes (age v1 specification).
AGE_CHUNK_BYTES = 64 * 1024
AGE_TAG_BYTES = 16
# An upper bound on the header and nonce: one X25519 recipient measures 184 bytes.
# An underestimate would encrypt a file that the store then refuses by size.
AGE_HEADER_BYTES = 1024
# The store policy's default volume fraction (D12, choice 3); cellctl may render another.
DEFAULT_VOLUME_FRACTION = 0.25


def age_ciphertext_size(plaintext_bytes: int) -> int:
    """The largest `age` ciphertext that `plaintext_bytes` of plaintext can produce."""
    if plaintext_bytes < 0:
        raise ValueError("a plaintext size cannot be negative")
    chunks = max(1, -(-plaintext_bytes // AGE_CHUNK_BYTES))
    return AGE_HEADER_BYTES + plaintext_bytes + AGE_TAG_BYTES * chunks


def _largest_plaintext_within(ciphertext_bound: int) -> int:
    """The largest plaintext whose `age_ciphertext_size` is at most `ciphertext_bound`, or -1."""
    available = ciphertext_bound - AGE_HEADER_BYTES
    if available < AGE_TAG_BYTES:
        return -1
    full, rest = divmod(available, AGE_CHUNK_BYTES + AGE_TAG_BYTES)
    return full * AGE_CHUNK_BYTES + max(0, rest - AGE_TAG_BYTES)


@dataclass(frozen=True, slots=True)
class Bounds:
    """The rule's inputs. A missing object bound means the store sets none."""

    object_bytes: int | None
    hydration_bytes: int
    volume_bytes: int
    volume_fraction: float = DEFAULT_VOLUME_FRACTION

    def largest_offloadable(self) -> int:
        """The largest plaintext that classifies as offloadable, or -1 when none does."""
        ciphertext_bound = self.hydration_bytes if self.object_bytes is None else min(self.object_bytes, self.hydration_bytes)
        return min(_largest_plaintext_within(ciphertext_bound), int(self.volume_fraction * self.volume_bytes))

    def classify(self, plaintext_bytes: int, *, text: bool = False) -> str:
        """Classify one member; the caller applies the importer's text rule."""
        if text:
            return TEXT
        return OFFLOADABLE if plaintext_bytes <= self.largest_offloadable() else RESIDENT

    def hydration_fits(self, plaintext_bytes: int) -> bool:
        """Hydration reserves plaintext bytes only (D12, choice 3)."""
        return plaintext_bytes <= self.hydration_bytes


def bounds_for(config: dict[str, object], vault: Path) -> Bounds | None:
    """The rule's inputs for a store policy, or None when the policy sets no hydration capacity."""
    if config.get("max_temporary_bytes") is None:
        return None
    bound = config.get("max_object_bytes")
    fraction = config.get("volume_fraction")
    return Bounds(
        object_bytes=None if bound is None else int(bound),
        hydration_bytes=int(config["max_temporary_bytes"]),
        volume_bytes=shutil.disk_usage(vault).total,
        volume_fraction=DEFAULT_VOLUME_FRACTION if fraction is None else float(fraction),
    )
