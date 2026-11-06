"""The operator's physical meter of a cell's prefix in the media bucket (add-cloud-media-store D10).

cellctl lists `cells/<cell>/` with its list-only key and sums what the provider reports. The figure is
ciphertext bytes on disk for the operator, not the owner's allowance: the cell meters that from its own
sidecars, and nothing here changes it. A listing that fails is unknown, never zero, because a zero would
read as an empty prefix to the alert that watches for abuse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .storage.interface import ObjectStorage, cell_object_prefix

logger = logging.getLogger("cellctl")

# The provider's documented file-version actions. They are a closed protocol set that the provider fixes
# and versions, not a vocabulary that a customer or the data defines.
UPLOAD, START, HIDE = "upload", "start", "hide"


@dataclass(frozen=True)
class PhysicalFigure:
    """What one listing of a cell's prefix showed.

    `bytes` sums the content lengths of upload versions. `unfinished` counts large files that were
    started and never finished. `hides` counts hide markers: the edge cannot hide, so any one means a
    credential beyond the edge's touched the bucket. A field is None when the listing could not show it.
    """

    bytes: int | None
    unfinished: int | None
    hides: int | None


UNKNOWN = PhysicalFigure(bytes=None, unfinished=None, hides=None)


def meter_prefix(listing: ObjectStorage, cell_id: str) -> PhysicalFigure:
    """The figure for `cell_id`, or UNKNOWN when any page fails. The listing is all or nothing: it
    raises on a malformed or missing page, and a partial sum would understate the bucket."""
    try:
        versions = listing.list_object_versions(cell_object_prefix(cell_id))
    except Exception as error:  # noqa: BLE001 - a failed page makes the whole figure unknown
        # The type only: a provider message could carry an identifier.
        logger.error("cellctl: the media listing of cell %s failed (%s); its physical figure is unknown",
                     cell_id, type(error).__name__)
        return UNKNOWN
    uploads = [version for version in versions if version.action == UPLOAD]
    # An upload without a reported size makes the sum unknown; counting it as 0 would understate the bucket.
    sized = all(version.size is not None for version in uploads)
    return PhysicalFigure(
        bytes=sum(version.size for version in uploads if version.size is not None) if sized else None,
        unfinished=sum(1 for version in versions if version.action == START),
        hides=sum(1 for version in versions if version.action == HIDE),
    )


def over_allowance(figure: PhysicalFigure, allowance_bytes: int | None, headroom: float | None) -> bool | None:
    """Whether the prefix holds more than the cell's allowance times the configured headroom factor.
    None when the bytes, the allowance or the factor is unknown: an unknown figure is not "within"."""
    if figure.bytes is None or allowance_bytes is None or headroom is None:
        return None
    return figure.bytes > allowance_bytes * headroom
