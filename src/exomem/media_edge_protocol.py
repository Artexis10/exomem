"""Wire contract between the media edge and the cell's edge store (design D6)."""

from __future__ import annotations

EDGE_PORT = 8769
AUDIENCE = "exomem-media-edge"
READY_PATH = "/health/ready"
GENERATION_PATH = "/v1/generation"
PENDING_PATH = "/v1/pending"
OBJECTS_PATH = "/v1/objects/"
ADMIT_ACTION = "admit"
SIZE_HEADER = "x-exomem-size"
SHA256_HEADER = "x-exomem-sha256"
MAX_CREDENTIAL_CHARS = 8192

# Edge answers that the cell store maps onto its own error codes. Every other
# answer is an outage from the cell's point of view.
STATUS_TOO_LARGE = 413
STATUS_MISMATCH = 422
STATUS_ABSENT = 404


def object_key(cell: str, name: str) -> str:
    """The provider name of a cell's object, which the credential alone selects.

    The backup bucket uses the same `cells/<cell>/` shape for its own prefixes.
    """
    return f"cells/{cell}/{name}"
