"""Bring an obsolete projector namespace current with no manual step.

A release that bumps `projections.PROJECTOR_SCHEMA_VERSION` leaves an enrolled
vault's active tuple on the old projector. Projected reads and content
publication both refuse an obsolete projector, so until the representation is
refreshed the owner cannot write content. That refresh is mechanical: it is
the owner's representation-only `govern_memory` proposal (`documents={}`)
against the exact active policy, committed through the same policy-generation
publisher and tuple CAS. Nothing about it needs a person to decide.

`converge` runs that refresh from the owner runtime, under the writer lease,
at startup or takeover. It is idempotent: an already-current tuple answers
`current` without a proposal or a generation. It is crash-safe: a refresh
whose commit reserved its publication before the process died is completed
from its own receipt first, exactly as an owner retry would; a proposal that
never reserved simply expires. Every outcome is logged.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .. import reserved_paths
from . import projections, schema_v4, store
from .principal import owner_principal

log = logging.getLogger(__name__)

_INTENT = "Refresh the search representation for the running projector"


def _reserved_refreshes(vault_root: Path) -> list[str]:
    """Refresh proposals whose commit reserved a publication but never finished."""
    connection = store.open_authorization_session_connection(vault_root)
    try:
        rows = connection.execute(
            "SELECT proposal_id, proposal_json FROM governance_proposals "
            "WHERE status='pending' AND reserved_event_id IS NOT NULL ORDER BY created_at"
        ).fetchall()
    finally:
        connection.close()
    pending = []
    for proposal_id, proposal_json in rows:
        try:
            binding = json.loads(proposal_json).get("authority_binding") or {}
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(binding, dict) and binding.get("publication_mode") == "projector-refresh":
            pending.append(str(proposal_id))
    return pending


def converge(vault_root: Path, *, now: int | None = None) -> str:
    """Refresh an obsolete active projector; `not_enrolled`, `current` or `refreshed`."""
    from .tool import op_govern_memory

    root = Path(vault_root)
    if store.authorization_session_schema_version_if_readable(root) != schema_v4.SCHEMA_USER_VERSION:
        return "not_enrolled"
    owner = owner_principal()
    moment = int(time.time()) if now is None else now
    with reserved_paths._owner_authority_scope("govern_memory"):  # noqa: SLF001
        recovered = _reserved_refreshes(root)
        for proposal_id in recovered:
            op_govern_memory(
                root, operation="commit", principal=owner, proposal_id=proposal_id, now=moment
            )
            log.info("projector refresh %s recovered from its reserved publication", proposal_id)
        proposed = op_govern_memory(
            root, operation="propose", principal=owner, intent=_INTENT, documents={}, now=moment
        )
        if proposed.get("status") == "current":
            return "refreshed" if recovered else "current"
        op_govern_memory(
            root,
            operation="commit",
            principal=owner,
            proposal_id=proposed["proposal_id"],
            now=moment,
        )
    log.info(
        "projector namespace refreshed to version %s (proposal %s)",
        projections.PROJECTOR_SCHEMA_VERSION,
        proposed["proposal_id"],
    )
    return "refreshed"
