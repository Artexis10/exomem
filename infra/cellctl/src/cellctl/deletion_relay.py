"""automate-cell-storage-deletion D1: cellctl relays each deletion grant into the ConfigMap that the key
custodian and the media edge read.

cellctl holds no grant public key and needs none: each executor verifies a grant before it acts, so a
token that cellctl relays wrongly can only delay a deletion. cellctl reads two claims of each token without
verifying them: the cell ID, to list the cell's prefix, and `purge_after`, to keep the grant through the grace.

A grant leaves the ConfigMap only after cellctl's list-only views of both buckets show no `upload` and no
`start` entry under the cell's prefix. Until then an executor may still have work to do. A platform with
no media bucket has no media view, and that view counts as empty.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import jwt

from .db import CELL_ID_RE
from .deletion_grant import DELETIONS_CONFIGMAP, GRANTS_KEY, MAX_GRANT_CHARS
from .errors import describe_error
from .manifests import GATEWAY_NAMESPACE
from .storage.interface import cell_object_prefix, holds_stored_entry

logger = logging.getLogger("cellctl")

# A cell whose prefix still held an entry, or whose listing failed, is listed again after this long. The
# provider deletes the versions days after the hide, so a slower recheck only delays a drop, which costs nothing.
RELAY_RECHECK = timedelta(hours=1)
# The cells one pass lists. After a restart every finished deletion is listed once more, and that must not
# stall the pass that also serves the live cells.
RELAY_LISTINGS_PER_PASS = 4
_NEVER = datetime.min.replace(tzinfo=UTC)


@dataclass
class RelayMemory:
    """What this process learned from its listings. A restart forgets it and lists each cell once more.

    A cell in `empty` showed only hide markers in both views. No writer is left for a deleted cell, so that
    stays true. A cell in `remaining` showed an entry at its latest listing. `next_listing` says when a cell
    with an entry, or with a failed listing, is listed again."""

    empty: set[str] = field(default_factory=set)
    remaining: set[str] = field(default_factory=set)
    next_listing: dict[str, datetime] = field(default_factory=dict)


def relay_target(token: object) -> tuple[str, int] | None:
    """The cell ID and `purge_after` of a token, read without verification; None for a token that no executor
    could accept, because it has no cell ID of the right shape or no integer `purge_after`."""

    if not isinstance(token, str) or len(token) > MAX_GRANT_CHARS:
        return None
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    cell_id, purge_after = claims.get("cell_id"), claims.get("purge_after")
    if not isinstance(cell_id, str) or not CELL_ID_RE.fullmatch(cell_id) or type(purge_after) is not int:
        return None
    return cell_id, purge_after


def render_cell_deletions(tokens: list[str]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": DELETIONS_CONFIGMAP,
            "namespace": GATEWAY_NAMESPACE,
            "labels": {"app.kubernetes.io/name": "cellctl", "app.kubernetes.io/part-of": "exomem-cloud"},
        },
        "data": {GRANTS_KEY: json.dumps(tokens)},
    }


def _relayed(data: dict[str, str] | None) -> list[str]:
    if data is None:
        return []
    try:
        tokens = json.loads(data.get(GRANTS_KEY, "[]"))
    except ValueError:
        tokens = None
    if not isinstance(tokens, list):
        logger.error("cellctl: %s holds no JSON list; the relay rebuilds it from the database", DELETIONS_CONFIGMAP)
        return []
    return [token for token in tokens if isinstance(token, str)]


def _holds_entries(cell_id: str, views: Iterable) -> bool | None:
    """Whether a view shows an entry but a hide marker under the cell's prefix; None when a view is
    missing or its listing failed and no other view showed an entry."""

    unknown = False
    for view in views:
        if view is None:
            unknown = True
            continue
        try:
            if holds_stored_entry(view.iter_object_versions(cell_object_prefix(cell_id))):
                return True
        except Exception as error:  # noqa: BLE001 - a failed listing proves nothing; the grant stays
            logger.warning("cellctl could not list cell %s for its deletion grant: %s", cell_id, describe_error(error))
            unknown = True
    return None if unknown else False


def relay_grants(cluster, stored: list[str], now: datetime, memory: RelayMemory, *,
                 backup_listing, media_listing, media_bucket: bool) -> None:
    """One relay pass. `stored` holds the grants in the database, and `media_bucket` says whether the platform
    has a media bucket. The ConfigMap changes only on evidence: a cell is dropped only after a listing that
    shows no entry, and it is added again only after a listing that shows one."""

    try:
        current = cluster.read_cell_deletions()
    except Exception as error:  # noqa: BLE001 - the relay waits; the grants stay as they are
        logger.error("cellctl could not read %s: %s", DELETIONS_CONFIGMAP, describe_error(error))
        return
    relayed = _relayed(current)
    grants: dict[str, tuple[str, int]] = {}
    # The database's grant comes last, so it replaces a relayed one for the same cell.
    for token in [*relayed, *stored]:
        if (target := relay_target(token)) is not None:
            grants[target[0]] = (token, target[1])
    unusable = sum(relay_target(token) is None for token in stored)
    if unusable:
        logger.error("cellctl: %d stored deletion grant(s) carry no usable cell ID or purge_after", unusable)
    was_relayed = {target[0] for token in relayed if (target := relay_target(token)) is not None}

    epoch = now.timestamp()
    due = sorted((cell_id for cell_id, (_, purge_after) in grants.items()
                  if epoch >= purge_after and cell_id not in memory.empty
                  and memory.next_listing.get(cell_id, _NEVER) <= now),
                 key=lambda cell_id: (memory.next_listing.get(cell_id, _NEVER), cell_id))
    views = [backup_listing, media_listing] if media_bucket else [backup_listing]
    for cell_id in due[:RELAY_LISTINGS_PER_PASS]:
        holds = _holds_entries(cell_id, views)
        if holds is False:
            memory.empty.add(cell_id)
            memory.remaining.discard(cell_id)
            memory.next_listing.pop(cell_id, None)
            continue
        if holds:
            memory.remaining.add(cell_id)
        memory.next_listing[cell_id] = now + RELAY_RECHECK

    members = sorted(cell_id for cell_id, (_, purge_after) in grants.items()
                     if epoch < purge_after or cell_id in memory.remaining
                     or (cell_id in was_relayed and cell_id not in memory.empty))
    tokens = [grants[cell_id][0] for cell_id in members]
    if (current is None and not tokens) or (current is not None and current.get(GRANTS_KEY) == json.dumps(tokens)):
        return
    try:
        cluster.apply_cell_deletions(render_cell_deletions(tokens))
    except Exception as error:  # noqa: BLE001 - the next pass applies again
        logger.error("cellctl could not write %s: %s", DELETIONS_CONFIGMAP, describe_error(error))
        return
    added, dropped = sorted(set(members) - was_relayed), sorted(was_relayed - set(members))
    logger.info("cellctl: %s relays %d grant(s); added %s; dropped %s", DELETIONS_CONFIGMAP, len(tokens),
                added or "none", dropped or "none")
