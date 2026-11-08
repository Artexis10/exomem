"""The names and sizes of an export's Kubernetes objects (add-cloud-media-store D8).

Rendering and the export decisions share these values. The module imports no
S3 client, so a tool that only renders cell objects loads it without botocore.
"""
from __future__ import annotations

import uuid

from .storage_config import GIB

# The cell command's own phases (`exomem cell-export --phase`), and the step
# that verifies the completed version. Each names one Job of the export.
PAUSE, PACK, UPLOAD, VERIFY = "pause", "pack", "upload", "verify"
# C6: the part URLs cover the planned archive plus 2% for what the plan cannot count.
SIZE_MARGIN_PERCENT = 102
# One presigned URL is one Job argument; this keeps a Job object far inside the
# API server's request size limit. A larger archive gets larger parts instead.
MAX_PARTS = 1000
# D8: the pack holds at most two parts on staging, so a part stays at most 1 GiB,
# and the archive bound is MAX_PARTS of them, near 1 TiB.
MAX_PART_BYTES = GIB
ARCHIVE_BOUND = MAX_PARTS * MAX_PART_BYTES * 100 // SIZE_MARGIN_PERCENT
# D8: staging holds the copied members (at most the cell's used bytes plus 10%
# for filesystem overhead) and the pack's two-part spool.
STAGING_MARGIN_PERCENT = 110
STAGING_SPOOL_BYTES = 2 * MAX_PART_BYTES
# ext4 keeps 5% of a new filesystem's blocks for root, so the Jobs can write to 95% of the claim.
STAGING_USABLE_PERCENT = 95


def job_name(export_id: str, step: str) -> str:
    return f"export-{uuid.UUID(export_id).hex}-{step}"


# The single Job an export ran before the three steps. cellctl still removes it.
def legacy_job_name(export_id: str) -> str:
    return f"export-{uuid.UUID(export_id).hex}"


def staging_claim_name(export_id: str) -> str:
    return f"export-staging-{uuid.UUID(export_id).hex}"


def verification_secret_name(export_id: str) -> str:
    return job_name(export_id, VERIFY) + "-get"


def staging_gib(used_bytes: int | None, storage_gib: int) -> int:
    """D8: the staging claim holds the copied members and the spool, in whole
    GiB. It never exceeds staging_bound_gib, which the cell quota admits."""

    bound = staging_bound_gib(storage_gib)
    if used_bytes is None:
        return bound
    return min(bound, _claim_gib(used_bytes))


def staging_bound_gib(storage_gib: int) -> int:
    """The largest staging claim of a cell of this size: a full volume plus the spool."""

    return _claim_gib(storage_gib * GIB)


def _claim_gib(member_bytes: int) -> int:
    usable = member_bytes * STAGING_MARGIN_PERCENT // 100 + STAGING_SPOOL_BYTES
    return -(-(usable * 100) // (STAGING_USABLE_PERCENT * GIB))
